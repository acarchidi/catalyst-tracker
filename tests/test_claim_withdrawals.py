"""The reviewed withdrawal list (ADR 0070): a listed claim is held, and its catalyst unpublished."""

from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from catalyst.alerts import _extract_problems  # pyright: ignore[reportPrivateUsage]
from catalyst.api.access import Access
from catalyst.api.read import catalyst as read_catalyst
from catalyst.api.read import published
from catalyst.cli import decide_after_checks
from catalyst.export import build_export
from catalyst.publish.gate import Decision
from catalyst.publish.run import decide_claims
from catalyst.publish.withdrawals import (
    DEFAULT_PATH,
    Withdrawal,
    WithdrawalError,
    load_withdrawals,
    resolve,
    withdraw,
)
from catalyst.publish.write import publish_claims
from catalyst.schemas import Catalyst, CatalystChange, ClaimDecision, GateDecision
from catalyst.snapshots import LocalSnapshotStore
from test_publish_writer import NOW, VERIFIER_MODEL, World


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
        "04157d4b", "7d2dd75b", "6adfe759", "c6b6be58", "dbe1d11b", "274e2311", "44f4eb8e", "b5e4fea1"
    ]  # fmt: skip
    assert all(e.reason and e.added == date(2026, 11, 24) for e in entries)


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
