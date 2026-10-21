"""A person's decisions on held claims, and how an approval reaches live data (ADR 0042, part 1).

Spec, Agent architecture: the review queue's approvals go through the publish gate, and only
the plain-code writer writes live data. So recording a decision writes no catalyst; an approval
excuses only the reasons the person saw, never the quote match; a rejection never publishes.
Each test stores real-shaped rows with the writer tests' `World` and runs the real jobs.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.publish import gate
from catalyst.publish.run import decide_claims
from catalyst.publish.sanity import PASSED, ROLE_UNCLEAR, TIED, check_safe_to_write, person_may_approve
from catalyst.publish.write import publish_claims
from catalyst.review import ReviewError, record_decision
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CheckOutcome,
    ClaimCheck,
    ClaimDecision,
    GateDecision,
    ReviewAction,
    ReviewDecision,
)
from catalyst.snapshots import LocalSnapshotStore
from catalyst.verify.rules import CheckResult
from catalyst.verify.run import store_check
from test_publish_writer import NOW, OCT_16, VERIFIER_MODEL, World

OWNER = "acarchidi"
LATER = NOW + timedelta(hours=1)


@pytest.fixture
def world(session_factory: sessionmaker[Session], tmp_path: Path) -> World:
    return World(session_factory, LocalSnapshotStore(tmp_path))


def _publish(world: World, at: datetime = NOW) -> dict[str, Any]:
    return publish_claims(world.factory, world.store, lambda: at, verifier_model=VERIFIER_MODEL).summary()


def _check(world: World, claim: UUID, name: str) -> tuple[str, str | None]:
    (row,) = [r for r in world.rows(ClaimCheck) if r.claim_id == claim and r.check_name == name]
    return row.outcome, row.reason


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
        assert _decide(world)[claim] == ("review", [*reasons, "rejected in the review queue"])
        assert _publish(world, NOW + timedelta(hours=hours))["claims_marked_publish"] == 0
    assert world.counts() == (0, 0, 0, 0)
    (row,) = world.rows(ReviewDecision)
    assert (row.action, row.reviewer, row.decided_at) == ("reject", OWNER, LATER)


def test_a_rejection_after_an_approval_withdraws_it_before_the_writer_runs(world: World) -> None:
    claim, reasons = _held(world)
    _record(world, claim, ReviewAction.APPROVE, reasons)
    record_decision(world.factory, claim, ReviewAction.REJECT, OWNER, reasons, lambda: LATER + timedelta(1))
    assert _decide(world)[claim] == (
        "review",
        [*reasons, "rejected in the review queue"],
    )  # the latest counts
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
    approved = gate.approve(decision, seen, _no_writer_hold)
    assert approved.decision is GateDecision.REVIEW
    assert approved.holds == (*holds, unseen_hold)  # only the seen, excusable hold is lifted
    assert {
        gate.QUOTE_CHECK,
        "window_not_past",
        "has_window",
        "extractor_claim",
        "extractor_prompt",
    } <= gate.PERSON_MAY_NOT_EXCUSE
    assert gate.approve(gate.Decision(GateDecision.PUBLISH), frozenset(), _no_writer_hold).decision is (
        GateDecision.PUBLISH
    )
    writer = gate.Hold(gate.WRITER_CHECK, f"{gate.WRITER_CHECK}: a guess")
    only_writer = gate.Decision(GateDecision.REVIEW, (writer,))
    assert gate.approve(only_writer, frozenset({writer.reason}), _no_writer_hold).holds == (writer,)
    lifted = gate.approve(only_writer, frozenset({writer.reason}), lambda reason: reason == "a guess")
    assert lifted.holds == ()


def test_a_check_that_has_not_run_is_never_lifted_even_when_seen() -> None:
    """Opus review of PR #84: a person cannot vouch for a check nobody ran."""
    missing = gate.Hold("second_reading_agrees", "check second_reading_agrees has not run", missing=True)
    decision = gate.Decision(GateDecision.REVIEW, (missing,))
    assert gate.approve(decision, frozenset({missing.reason}), _no_writer_hold).holds == (missing,)


def _no_writer_hold(_reason: str) -> bool:
    return False


def test_an_approval_excuses_a_date_move_the_writer_holds_for_a_person(world: World) -> None:
    """Spec, Change sanity: a move of more than 6 months goes to review; a person may approve it."""
    world.claim("October 2027")
    _publish(world)
    moved = world.claim("late 2027 or early 2028", published=OCT_16)
    assert _publish(world)["held_by_writer"] == 1
    decision, reasons = _decide(world)[moved]
    moved_reason = (
        "safe_to_write: the date would move more than 6 months "
        "(from 2027-10-01..2027-10-31 to 2027-07-01..2028-06-30)"
    )
    assert (decision, reasons) == ("review", [moved_reason])
    _record(world, moved, ReviewAction.APPROVE, reasons)
    _decide(world)
    summary = _publish(world)
    assert (summary["catalysts_updated"], summary["published_by_approval"]) == (1, 1)
    moves = [c for c in world.rows(CatalystChange) if c.old_value is not None]
    assert moves
    assert {c.claim_id for c in moves} == {moved}


def test_b1_an_approved_slip_whose_window_has_since_passed_is_never_written(world: World) -> None:
    """Opus review of PR #84, B1: lifting "does not overlap" must not skip the passed-window check."""
    world.claim("first half of 2027")
    _publish(world)
    slip = world.claim("fourth quarter of 2026", published=OCT_16, decision=GateDecision.REVIEW)
    check_safe_to_write(world.factory, lambda: NOW)
    _, reasons = _decide(world)[slip]
    assert reasons == [
        "safe_to_write: the window does not overlap the published one "
        "(from 2027-01-01..2027-06-30 to 2026-10-01..2026-12-31)"
    ]
    _record(world, slip, ReviewAction.APPROVE, reasons)
    later = datetime(2027, 1, 5, 17, tzinfo=UTC)
    decide_claims(world.factory, world.store, lambda: later)
    summary = _publish(world, later)
    assert (summary["catalysts_updated"], summary["published_by_approval"]) == (0, 0)
    (row,) = world.rows(Catalyst)
    assert (row.window_start.isoformat(), row.window_end.isoformat()) == ("2027-01-01", "2027-06-30")
    stored = (
        "the window does not overlap the published one; the new window has already passed "
        "(from 2027-01-01..2027-06-30 to 2026-10-01..2026-12-31)"
    )
    assert _check(world, slip, "safe_to_write") == ("fail", stored)
    decide_claims(world.factory, world.store, lambda: later)
    (decision,) = [d for d in world.rows(ClaimDecision) if d.claim_id == slip]
    assert (decision.decision, decision.reasons) == (
        "review",
        [f"safe_to_write: {stored}"],
    )  # back in the queue


def test_b1_every_failing_date_reason_is_listed_and_all_must_be_approvable() -> None:
    assert person_may_approve("the window does not overlap the published one (from a to b)")
    assert person_may_approve(
        "the window does not overlap the published one; the date would move more than 6 months (from a to b)"
    )
    assert not person_may_approve(f"the window does not overlap the published one; {PASSED} (from a to b)")
    assert not person_may_approve(f"{PASSED} (from a to b)")
    assert not person_may_approve(TIED)
    assert not person_may_approve(ROLE_UNCLEAR)


def test_b2_an_approval_does_not_lift_a_move_from_a_published_window_that_changed_since(world: World) -> None:
    """Opus review of PR #84, B2: the reason names both windows, so a new published window voids it."""
    first = world.claim("October 2027")
    _publish(world)
    moved = world.claim("late 2027 or early 2028", published=OCT_16)
    _publish(world)
    _, reasons = _decide(world)[moved]
    _record(world, moved, ReviewAction.APPROVE, reasons)
    catalyst = TABLES[Catalyst]
    with world.factory.begin() as session:  # the published window changes before the writer runs
        session.execute(
            update(catalyst).values(window_start=date(2027, 11, 1), window_end=date(2027, 11, 30))
        )
    _decide(world)
    summary = _publish(world)
    assert (summary["catalysts_updated"], summary["published_by_approval"]) == (0, 0)
    assert _check(world, moved, "safe_to_write") == (
        "fail",
        "the date would move more than 6 months (from 2027-11-01..2027-11-30 to 2027-07-01..2028-06-30)",
    )
    assert {c.claim_id for c in world.rows(CatalystChange)} == {first}


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


# --- A rejection holds for good, whatever clears later (Opus review of PR #84, round 2, B1) ---

REJECTED = "rejected in the review queue"


def _rerun(world: World, claim: UUID, outcome: CheckOutcome, at: datetime) -> None:
    """Run the second-reading check again for one claim, as its own job stores it."""
    reason = "stored as failed" if outcome is CheckOutcome.FAIL else None
    with world.factory.begin() as session:
        store_check(session, claim, CheckResult("second_reading_agrees", outcome, reason), at)


def test_a_rejected_claim_stays_held_when_every_check_passes_later(world: World) -> None:
    claim, reasons = _held(world)
    _record(world, claim, ReviewAction.REJECT, reasons)
    _rerun(world, claim, CheckOutcome.PASS, LATER + timedelta(hours=1))  # say a second source arrives
    report = decide_claims(world.factory, world.store, lambda: NOW)
    assert report.reasons == {"review_rejected failed": 1}
    (row,) = [d for d in world.rows(ClaimDecision) if d.claim_id == claim]
    assert (row.decision, row.reasons) == ("review", [REJECTED])  # no claim text in the reason
    assert _publish(world)["claims_marked_publish"] == 0
    assert world.counts() == (0, 0, 0, 0)


def test_the_writer_holds_a_rejected_claim_even_when_its_decision_says_publish(world: World) -> None:
    claim, reasons = _held(world)
    _record(world, claim, ReviewAction.REJECT, reasons)
    _rerun(world, claim, CheckOutcome.PASS, LATER + timedelta(hours=1))
    decision = TABLES[ClaimDecision]
    with world.factory.begin() as session:  # a publish decision stored before the rejection counted
        session.execute(
            update(decision).where(decision.c.claim_id == claim).values(decision="publish", reasons=[])
        )
    summary = _publish(world)
    assert (summary["claims_marked_publish"], summary["held_on_recheck"]) == (1, 1)
    assert summary["top_reasons"] == {"review_rejected failed": 1}
    assert world.counts() == (0, 0, 0, 0)


def test_the_rejection_hold_is_one_no_approval_of_it_excuses() -> None:
    assert "review_rejected" in gate.PERSON_MAY_NOT_EXCUSE
    rejected = gate.Decision(GateDecision.REVIEW, (gate.REVIEW_REJECTED,))
    assert gate.approve(rejected, frozenset({REJECTED}), _no_writer_hold).holds == (gate.REVIEW_REJECTED,)
    assert gate.Hold("review_rejected", REJECTED) == gate.REVIEW_REJECTED


def test_a_later_approval_overrides_a_rejection(world: World) -> None:
    claim, reasons = _held(world)
    _record(world, claim, ReviewAction.REJECT, reasons)
    assert _decide(world)[claim] == ("review", [*reasons, REJECTED])
    approved_at = LATER + timedelta(hours=1)
    record_decision(
        world.factory, claim, ReviewAction.APPROVE, OWNER, [*reasons, REJECTED], lambda: approved_at
    )
    assert _decide(world)[claim] == ("publish", [])
    summary = _publish(world)
    assert (summary["catalysts_created"], summary["published_by_approval"]) == (1, 1)


# --- An approval does not excuse a failure that came back after it (round 2, optional 1) ---


def test_an_approval_does_not_excuse_a_failure_that_cleared_and_came_back(world: World) -> None:
    claim, reasons = _held(world)
    _record(world, claim, ReviewAction.APPROVE, reasons)
    _rerun(world, claim, CheckOutcome.PASS, LATER + timedelta(hours=1))
    _rerun(world, claim, CheckOutcome.FAIL, LATER + timedelta(hours=2))  # the same words, a new failure
    assert _decide(world)[claim] == ("review", reasons)
    assert _publish(world)["catalysts_created"] == 0


def test_an_approval_still_counts_when_its_check_runs_again_and_fails_the_same_way(world: World) -> None:
    claim, reasons = _held(world)
    _record(world, claim, ReviewAction.APPROVE, reasons)
    _rerun(world, claim, CheckOutcome.FAIL, LATER + timedelta(hours=1))  # the scheduled run, unchanged
    (row,) = [
        r for r in world.rows(ClaimCheck) if r.claim_id == claim and r.check_name == "second_reading_agrees"
    ]
    assert row.checked_at < LATER  # kept: the outcome has not changed since it was first found
    assert _decide(world)[claim] == ("publish", [])
    assert _publish(world)["published_by_approval"] == 1


def test_the_writer_does_not_excuse_a_failure_that_came_back_after_the_decision_job(world: World) -> None:
    claim, reasons = _held(world)
    _record(world, claim, ReviewAction.APPROVE, reasons)
    assert _decide(world)[claim] == ("publish", [])
    _rerun(world, claim, CheckOutcome.PASS, LATER + timedelta(hours=1))
    _rerun(world, claim, CheckOutcome.FAIL, LATER + timedelta(hours=2))
    summary = _publish(world)
    assert (summary["held_on_recheck"], summary["catalysts_created"]) == (1, 0)
    assert world.counts() == (0, 0, 0, 0)
