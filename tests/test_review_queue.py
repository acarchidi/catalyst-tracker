"""A person's decisions on held claims, and how an approval reaches live data (ADR 0042, part 1).

Spec, Agent architecture: the review queue's approvals go through the publish gate, and only
the plain-code writer writes live data. So recording a decision writes no catalyst; an approval
excuses only the reasons the person saw, never the quote match; a rejection never publishes.
Each test stores real-shaped rows with the writer tests' `World` and runs the real jobs.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.publish import gate
from catalyst.publish.run import decide_claims
from catalyst.publish.sanity import ROLE_UNCLEAR, check_safe_to_write
from catalyst.publish.write import publish_claims
from catalyst.review import ReviewError, record_decision
from catalyst.schemas import (
    CatalystChange,
    CheckOutcome,
    ClaimCheck,
    ClaimDecision,
    GateDecision,
    ReviewAction,
    ReviewDecision,
)
from catalyst.snapshots import LocalSnapshotStore
from test_publish_writer import NOW, OCT_16, VERIFIER_MODEL, World

OWNER = "acarchidi"
LATER = NOW + timedelta(hours=1)


@pytest.fixture
def world(session_factory: sessionmaker[Session], tmp_path: Path) -> World:
    return World(session_factory, LocalSnapshotStore(tmp_path))


def _publish(world: World, at: datetime = NOW) -> dict[str, Any]:
    return publish_claims(world.factory, world.store, lambda: at, verifier_model=VERIFIER_MODEL).summary()


def _decide(world: World) -> dict[UUID, tuple[str, list[str]]]:
    decide_claims(world.factory, world.store, lambda: NOW)
    return {row.claim_id: (row.decision, list(row.reasons)) for row in world.rows(ClaimDecision)}


def _held(world: World, **kwargs: Any) -> tuple[UUID, list[str]]:
    """Store a claim the gate holds because its second reading failed, and return its reasons."""
    stored = {"second_reading_agrees": CheckOutcome.FAIL} | kwargs.pop("stored", {})
    claim = world.claim("second half of 2027", decision=GateDecision.REVIEW, stored=stored, **kwargs)
    decision, reasons = _decide(world)[claim]
    assert decision == "review"
    return claim, reasons


def _record(world: World, claim: UUID, action: ReviewAction, seen: list[str]) -> ReviewDecision:
    return record_decision(world.factory, claim, action, OWNER, seen, lambda: LATER)


# --- An approval publishes only through the gate's writer, and records who and when ---


def test_an_approval_publishes_only_through_the_writer_and_records_who_and_when(world: World) -> None:
    claim, reasons = _held(world)
    assert reasons == ["second_reading_agrees: stored as failed"]
    assert _publish(world)["catalysts_created"] == 0  # held: the writer does not see it
    stored = _record(world, claim, ReviewAction.APPROVE, reasons)
    assert world.counts() == (0, 0, 0, 0)  # recording the decision writes no live data
    (row,) = world.rows(ReviewDecision)
    assert (row.claim_id, row.action, row.reviewer, row.decided_at) == (claim, "approve", OWNER, LATER)
    assert (row.reasons_seen, row.id) == (reasons, stored.id)
    assert _decide(world)[claim] == ("publish", [])  # the gate now lets it through
    assert world.counts() == (0, 0, 0, 0)  # and still nothing is written until the writer runs
    summary = _publish(world)
    assert (summary["catalysts_created"], summary["published_by_approval"]) == (1, 1)
    changes = world.rows(CatalystChange)
    assert changes
    assert {change.claim_id for change in changes} == {claim}  # full provenance: the claim
    assert world.rows(ReviewDecision) == [row]  # the audit record stands as written


def test_a_rejection_never_publishes(world: World) -> None:
    claim, reasons = _held(world)
    _record(world, claim, ReviewAction.REJECT, reasons)
    for hours in (0, 6):
        assert _decide(world)[claim] == ("review", reasons)
        assert _publish(world, NOW + timedelta(hours=hours))["claims_marked_publish"] == 0
    assert world.counts() == (0, 0, 0, 0)
    (row,) = world.rows(ReviewDecision)
    assert (row.action, row.reviewer, row.decided_at) == ("reject", OWNER, LATER)


def test_a_rejection_after_an_approval_withdraws_it_before_the_writer_runs(world: World) -> None:
    claim, reasons = _held(world)
    _record(world, claim, ReviewAction.APPROVE, reasons)
    record_decision(world.factory, claim, ReviewAction.REJECT, OWNER, reasons, lambda: LATER + timedelta(1))
    assert _decide(world)[claim] == ("review", reasons)  # the latest decision counts
    assert _publish(world)["catalysts_created"] == 0
    assert [row.action for row in world.rows(ReviewDecision)].count("approve") == 1  # nothing is erased


# --- An approval excuses only what the person saw, and never the quote ---


def test_a_reason_the_person_did_not_see_still_holds_the_claim(world: World) -> None:
    claim, reasons = _held(world)
    _record(world, claim, ReviewAction.APPROVE, reasons)
    check = TABLES[ClaimCheck]
    with world.factory.begin() as session:
        session.execute(
            update(check)
            .where(check.c.claim_id == claim, check.c.check_name == "company_named_in_source")
            .values(outcome="fail", reason="the company is not named")
        )
    decision, now_reasons = _decide(world)[claim]
    assert (decision, now_reasons) == ("review", ["company_named_in_source: the company is not named"])
    assert _publish(world)["catalysts_created"] == 0


def test_an_approval_never_excuses_a_quote_that_is_not_in_the_stored_copy(
    world: World, tmp_path: Path
) -> None:
    claim, reasons = _held(world)
    _record(world, claim, ReviewAction.APPROVE, reasons)
    (copy,) = [p for p in tmp_path.rglob("*") if p.is_file()]
    copy.write_bytes(b"<p>something else</p>")
    decision, now_reasons = _decide(world)[claim]
    assert decision == "review"
    assert "the stored copy no longer matches its hash" in now_reasons
    assert _publish(world)["catalysts_created"] == 0


def test_the_gate_never_lets_a_person_excuse_the_quote_or_a_passed_window() -> None:
    holds = tuple(gate.Hold(name, f"{name}: failed") for name in sorted(gate.PERSON_MAY_NOT_EXCUSE))
    seen_hold, unseen_hold = (
        gate.Hold("second_reading_agrees", "seen"),
        gate.Hold("independent_source", "new"),
    )
    seen = frozenset(hold.reason for hold in (*holds, seen_hold))
    decision = gate.Decision(GateDecision.REVIEW, (*holds, seen_hold, unseen_hold))
    approved = gate.approve(decision, seen, frozenset())
    assert approved.decision is GateDecision.REVIEW
    assert approved.holds == (*holds, unseen_hold)  # only the seen, excusable hold is lifted
    assert {
        gate.QUOTE_CHECK,
        "window_not_past",
        "has_window",
        "extractor_claim",
    } <= gate.PERSON_MAY_NOT_EXCUSE
    assert gate.approve(gate.Decision(GateDecision.PUBLISH), frozenset(), frozenset()).decision is (
        GateDecision.PUBLISH
    )
    writer = gate.Hold(gate.WRITER_CHECK, f"{gate.WRITER_CHECK}: a guess")
    only_writer = gate.Decision(GateDecision.REVIEW, (writer,))
    assert gate.approve(only_writer, frozenset({writer.reason}), frozenset()).holds == (writer,)
    assert gate.approve(only_writer, frozenset({writer.reason}), frozenset({"a guess"})).holds == ()


def test_an_approval_excuses_a_date_move_the_writer_holds_for_a_person(world: World) -> None:
    """Spec, Change sanity: a move of more than 6 months goes to review; a person may approve it."""
    world.claim("October 2027")
    _publish(world)
    moved = world.claim("late 2027 or early 2028", published=OCT_16)
    assert _publish(world)["held_by_writer"] == 1
    decision, reasons = _decide(world)[moved]
    assert (decision, reasons) == ("review", ["safe_to_write: the date would move more than 6 months"])
    _record(world, moved, ReviewAction.APPROVE, reasons)
    _decide(world)
    summary = _publish(world)
    assert (summary["catalysts_updated"], summary["published_by_approval"]) == (1, 1)
    moves = [c for c in world.rows(CatalystChange) if c.old_value is not None]
    assert moves
    assert {c.claim_id for c in moves} == {moved}


def test_an_approval_does_not_let_the_writer_guess_a_drug_role(world: World) -> None:
    sentence = (
        "Topline data from the Phase 3 RAINIER trial of pegozafermin as part of a doublet regimen "
        "are expected in the second half of 2027."
    )
    claim = world.claim("second half of 2027", sentence=sentence, decision=GateDecision.REVIEW)
    check_safe_to_write(world.factory, lambda: NOW)
    _, reasons = _decide(world)[claim]
    assert reasons == [f"safe_to_write: {ROLE_UNCLEAR}"]
    _record(world, claim, ReviewAction.APPROVE, reasons)
    assert _decide(world)[claim] == ("review", reasons)  # a person approves values, not a guess
    assert _publish(world)["claims_marked_publish"] == 0
    assert world.counts() == (0, 0, 0, 0)


# --- What a decision must say, and when it is refused ---


def test_a_decision_must_match_the_reasons_the_gate_holds_now(world: World) -> None:
    claim, _ = _held(world)
    with pytest.raises(ReviewError, match="changed since"):
        _record(world, claim, ReviewAction.APPROVE, ["something else"])
    assert world.rows(ReviewDecision) == []


def test_only_a_held_claim_can_be_decided(world: World) -> None:
    published = world.claim("second half of 2027")
    _decide(world)
    with pytest.raises(ReviewError, match="not waiting for review"):
        _record(world, published, ReviewAction.APPROVE, ["anything"])
    with pytest.raises(ReviewError, match="not waiting for review"):
        _record(world, uuid4(), ReviewAction.REJECT, ["anything"])
    assert world.rows(ReviewDecision) == []


@pytest.mark.parametrize("reviewer", ["", "   "])
def test_a_decision_names_who_made_it(world: World, reviewer: str) -> None:
    claim, reasons = _held(world)
    with pytest.raises(ReviewError, match="who"):
        record_decision(world.factory, claim, ReviewAction.APPROVE, reviewer, reasons, lambda: LATER)
    assert world.rows(ReviewDecision) == []


def test_the_record_refuses_an_approval_without_the_reasons_seen() -> None:
    with pytest.raises(ValueError, match="reasons"):
        ReviewDecision(
            claim_id=uuid4(), action=ReviewAction.APPROVE, reviewer=OWNER, reasons_seen=[], decided_at=NOW
        )
