"""The reviewed withdrawal list (ADR 0070): a listed claim is held, and its catalyst unpublished."""

from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from catalyst.alerts import _extract_problems  # pyright: ignore[reportPrivateUsage]
from catalyst.api.access import Access
from catalyst.api.read import catalyst as read_catalyst
from catalyst.api.read import published
from catalyst.cli import decide_after_checks, withdrawals_waiting
from catalyst.export import build_export
from catalyst.publish.gate import Decision
from catalyst.publish.indication import write_indications
from catalyst.publish.run import decide_claims
from catalyst.publish.withdrawals import (
    DEFAULT_PATH,
    Withdrawal,
    WithdrawalError,
    load_withdrawals,
    resolve,
    withdraw,
    withdrawals_pending,
)
from catalyst.publish.write import WITHDRAWN_EVENT, publish_claims
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    ClaimCheck,
    ClaimDecision,
    GateDecision,
    IndicationAttempt,
    IndicationResult,
    SourceTier,
)
from catalyst.snapshots import LocalSnapshotStore
from conftest import truncate_all
from test_publish_writer import NOW, OCT_14, OCT_16, VERIFIER_MODEL, World


def _entry(claim: UUID | str, reason: str = "the date has passed") -> Withdrawal:
    text = claim.hex[:8] if isinstance(claim, UUID) else claim
    return Withdrawal(claim=text, reason=reason, evidence="own source", added=date(2026, 11, 24))


@pytest.fixture
def world(session_factory: sessionmaker[Session], tmp_path: Path) -> World:
    return World(session_factory, LocalSnapshotStore(tmp_path))


def _publish(world: World, *entries: Withdrawal, days: int = 0) -> dict[str, Any]:
    at = NOW + timedelta(days=days)
    report = publish_claims(
        world.factory, world.store, lambda: at, verifier_model=VERIFIER_MODEL, withdrawals=entries
    )
    return report.summary()


# --- The list itself ---


def test_the_shipped_list_reads_and_names_each_reviewed_claim() -> None:
    entries = load_withdrawals(DEFAULT_PATH)
    assert [e.claim for e in entries] == [
        "04157d4b", "7d2dd75b", "6adfe759", "c6b6be58", "dbe1d11b", "274e2311", "44f4eb8e", "b5e4fea1",
        "0ab19edd-b118-45c3-8072-5ba268b82479", "eb4d7ce8-57b8-4df9-87de-d10fe82c010f",
        "96f28b6e-2ea2-4afc-8958-0d2e563b59cf", "c3ddf620-b734-4171-afcb-f3f41fdd6b29",
        "cf727e73-4231-435a-990a-fef9994db3b2", "e7bd09fa-eaef-4034-9eba-2609852ff512",
        "2ccc4ee2-846e-45fe-8886-2932c7becb41",
        "9fdf4f20-b3cf-4a2f-9000-974edb5898ec", "06dcbad8-1f08-45c3-ba2d-92f5c0b92de4",
        "e82cd120-a56a-4da9-a5bd-ea58cb778f41", "47e55a67-b917-45c6-9b4a-4db0752fe4a0",
        "15410cfa-c5e5-49aa-af52-6823fefcf73a", "8aa0095b-6759-4ce8-86af-20b868fb8593",
    ]  # fmt: skip
    assert all(e.reason and e.added in (date(2026, 11, 24), date(2026, 11, 25)) for e in entries)


def test_a_prefix_shorter_than_eight_or_a_malformed_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pattern"):
        _entry("04157d4")
    bad = tmp_path / "bad.toml"
    bad.write_text('[[withdrawal]]\nclaim = "04157d4b"\n')
    with pytest.raises(WithdrawalError):
        load_withdrawals(bad)
    with pytest.raises(WithdrawalError):
        load_withdrawals(tmp_path / "missing.toml")


def test_a_full_id_or_a_unique_prefix_names_its_claim() -> None:
    one, two = UUID("04157d4b-0000-4000-8000-000000000001"), UUID("04157d4c-0000-4000-8000-000000000002")
    assert resolve([_entry(one)], [one, two]) == {one: _entry(one)}
    full = Withdrawal(claim=str(two), reason="r", evidence="own source", added=date(2026, 11, 24))
    assert resolve([full], [one, two]) == {two: full}


def test_a_prefix_naming_no_claim_or_several_fails_loudly() -> None:
    one, two = UUID("04157d4b-0000-4000-8000-000000000001"), UUID("04157d4b-0000-4000-8000-000000000002")
    with pytest.raises(WithdrawalError, match="names 2 claims"):
        resolve([_entry("04157d4b")], [one, two])
    with pytest.raises(WithdrawalError, match="names 0 claims"):
        resolve([_entry("ffffffff")], [one, two])


def test_a_withdrawal_only_ever_holds() -> None:
    held = withdraw(Decision(GateDecision.PUBLISH), _entry(uuid4(), "stale"))
    assert (held.decision, held.reasons) == (GateDecision.REVIEW, ("withdrawn after review: stale",))
    assert withdraw(Decision(GateDecision.REVIEW), None) == Decision(GateDecision.REVIEW)


# --- The gate and the writer ---


def test_a_withdrawn_claim_is_held_by_the_gate_and_never_published(world: World) -> None:
    claim = world.claim("second half of 2027")
    decide_claims(world.factory, world.store, lambda: NOW, [_entry(claim, "superseded")])
    (row,) = world.rows(ClaimDecision)
    assert (row.decision, row.reasons) == (GateDecision.REVIEW, ["withdrawn after review: superseded"])


def test_a_stale_publish_decision_is_still_held_by_the_writer(world: World) -> None:
    claim = world.claim("second half of 2027")  # marked to publish before the entry was added
    summary = _publish(world, _entry(claim))
    assert (summary["catalysts_created"], summary["held_on_recheck"]) == (0, 1)
    assert world.rows(Catalyst) == []


def test_a_published_claim_is_unpublished_once_with_the_change_in_its_history(world: World) -> None:
    claim = world.claim("second half of 2027")
    assert _publish(world)["catalysts_created"] == 1
    (live,) = world.rows(Catalyst)
    assert _publish(world, _entry(claim), days=1)["catalysts_withdrawn"] == 1
    (row,) = world.rows(Catalyst)
    assert row.status == "withdrawn"
    (change,) = [c for c in world.rows(CatalystChange) if c.field == "status" and c.old_value is not None]
    assert (change.old_value, change.new_value, change.claim_id, change.is_correction) == (
        "upcoming", "withdrawn", claim, True
    )  # fmt: skip
    before = len(world.rows(CatalystChange))
    assert _publish(world, _entry(claim), days=2)["catalysts_withdrawn"] == 0  # safe to re-run
    assert len(world.rows(CatalystChange)) == before
    with world.factory() as session:
        assert read_catalyst(session, live.id, Access.PAID) is None
        assert published(session) == []  # the digest is built from these (catalyst.outreach.digest)
    decide_claims(world.factory, world.store, lambda: NOW, [_entry(claim)])
    with world.factory() as session:
        export = build_export(session, NOW, include_held=True)
    assert (export["catalysts"], export["held"], export["counts"]["published"]) == ([], [], 0)


def test_a_bad_entry_stops_the_run_and_raises_an_alert(world: World) -> None:
    world.claim("second half of 2027")
    with pytest.raises(WithdrawalError):
        _publish(world, _entry("ffffffff"))
    assert world.rows(Catalyst) == []
    # The shipped list names claims this test database lacks: the pipeline reports an error.
    decisions = decide_after_checks(world.factory, world.store, {})
    assert "error" in decisions
    assert "The publish decisions did not finish." in _extract_problems({"decisions": decisions})


# --- A withdrawn event does not come back (Opus review of PR #151) ---


def test_an_older_claim_about_the_same_event_does_not_bring_it_back(world: World) -> None:
    first = world.claim("second half of 2027", published=OCT_14)
    older = world.claim("second half of 2027", published=OCT_14 - timedelta(days=1))
    assert _publish(world)["catalysts_created"] == 1  # the older one stays marked, never cited
    summary = _publish(world, _entry(first), days=1)
    assert (summary["catalysts_withdrawn"], summary["catalysts_created"]) == (1, 0)
    assert summary["top_reasons"] == {WITHDRAWN_EVENT: 1}
    held = [r for r in world.rows(ClaimCheck) if r.claim_id == older and r.check_name == "safe_to_write"]
    assert [(r.outcome, r.reason) for r in held] == [("fail", WITHDRAWN_EVENT)]
    assert _publish(world, _entry(first), days=2)["catalysts_created"] == 0
    assert [row.status for row in world.rows(Catalyst)] == ["withdrawn"]


def test_a_later_release_repeating_the_stale_date_does_not_bring_it_back(world: World) -> None:
    first = world.claim("second half of 2027", published=OCT_14)
    assert _publish(world)["catalysts_created"] == 1
    world.claim("second half of 2027", published=OCT_16)  # same dates: never recorded against it
    assert _publish(world)["not_written_same_dates"] == 1
    summary = _publish(world, _entry(first), days=1)
    assert (summary["catalysts_withdrawn"], summary["catalysts_created"]) == (1, 0)
    assert summary["top_reasons"] == {WITHDRAWN_EVENT: 1}
    assert _publish(world, _entry(first), days=2)["catalysts_created"] == 0
    assert [row.status for row in world.rows(Catalyst)] == ["withdrawn"]


def test_only_a_release_published_after_the_withdrawal_may_publish_the_event_again(world: World) -> None:
    first = world.claim("second half of 2027", published=OCT_14)
    _publish(world)
    _publish(world, _entry(first), days=1)  # withdrawn on Oct 19
    world.claim("first half of 2028", published=NOW + timedelta(days=3))
    assert _publish(world, _entry(first), days=4)["catalysts_created"] == 1
    assert sorted(row.status for row in world.rows(Catalyst)) == ["upcoming", "withdrawn"]


_RELEASES = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=20),  # days before NOW
        st.sampled_from([SourceTier.REGULATOR_OR_SEC, SourceTier.COMPANY_SITE, SourceTier.WIRE]),
        st.sampled_from(["second half of 2027", "fourth quarter of 2027", "December 2027"]),
    ),
    min_size=1,
    max_size=5,
)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(releases=_RELEASES, pick=st.integers(min_value=0, max_value=10))
def test_a_withdrawn_event_never_reappears_from_releases_before_the_withdrawal(
    session_factory: sessionmaker[Session], engine: Engine, tmp_path: Path, releases: list[Any], pick: int
) -> None:
    """Any same-event releases published before the withdrawal: two runs later, nothing is live."""
    truncate_all(engine)
    world = World(session_factory, LocalSnapshotStore(tmp_path))
    for days, tier, text in releases:
        world.claim(text, published=NOW - timedelta(days=days), tier=tier)
    _publish(world)
    cited = sorted({c.claim_id for c in world.rows(CatalystChange)}, key=str)
    if not cited:
        return  # nothing was published, so nothing can come back
    entry = _entry(cited[pick % len(cited)])
    for day in (1, 2):
        _publish(world, entry, days=day)
        assert {row.status for row in world.rows(Catalyst)} == {"withdrawn"}


def test_no_indication_is_written_to_a_withdrawn_catalyst(world: World) -> None:
    claim = world.claim("second half of 2027")
    _publish(world)
    (live,) = world.rows(Catalyst)
    _publish(world, _entry(claim), days=1)
    attempt = IndicationAttempt(
        catalyst_id=live.id,
        claim_id=claim,
        outcome=IndicationResult.FOUND,
        indication_claim_id=claim,
        reader_prompt_version="v1",
        checker_prompt_version="v1",
        reader_model="m",
        checker_model="m",
        attempted_at=NOW,
    )
    world.add(attempt)
    with world.factory.begin() as session:
        report = write_indications(session, world.store, NOW)
    assert (report.written, report.held) == (0, {})
    assert world.rows(Catalyst)[0].indication_id is None


def test_a_skipped_decision_with_withdrawals_waiting_raises_an_alert(world: World) -> None:
    claim = world.claim("second half of 2027")
    with world.factory() as session:
        assert withdrawals_pending(session, [_entry(claim)]) == 1  # still marked to publish
    _publish(world, _entry(claim))
    decide_claims(world.factory, world.store, lambda: NOW, [_entry(claim)])
    with world.factory() as session:
        assert withdrawals_pending(session, [_entry(claim)]) == 0
    decisions = {"skipped": "a check ran out of time", "withdrawals_waiting": 2}
    (problem,) = _extract_problems({"decisions": decisions})
    assert "2 claims on the withdrawal list" in problem
    stopped = decide_after_checks(world.factory, world.store, {"drug": {"stopped_for_time": True}})
    assert "withdrawals_waiting" not in stopped  # counted by the caller, from the shipped list:
    # it names claims this test database lacks, so the count cannot be read, and the alert says so.
    counted = withdrawals_waiting(world.factory, stopped)
    assert counted == stopped | {"withdrawals_unchecked": True}
    assert "could not be checked" in _extract_problems({"decisions": counted})[0]
    assert withdrawals_waiting(world.factory, {"error": "x"}) == {"error": "x"}
