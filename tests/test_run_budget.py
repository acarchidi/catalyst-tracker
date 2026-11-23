"""A run fits its time (ADR 0064).

Matching writes only what changed, each copy is read once, and a time budget stops the run
cleanly so the next one picks up where it stopped.
"""

import argparse
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, event, insert, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst import cli
from catalyst.budget import TimeBudget
from catalyst.copy_cache import CachedStore, claim_copy_keys
from catalyst.db.repository import link_company_document, save_source_document, upsert_company
from catalyst.db.tables import TABLES
from catalyst.resolve_claims import resolve_claims
from catalyst.schemas import (
    Claim,
    ClaimSubject,
    DocumentMatch,
    Drug,
    DrugName,
    DrugNameKind,
    EntityMatch,
    SourceDocument,
    SourceTier,
)
from catalyst.snapshots import Snapshot, SnapshotNotFoundError

NOW = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)


@pytest.fixture
def writes(engine: Engine) -> Iterator[list[str]]:
    """Every INSERT, UPDATE, DELETE, or SAVEPOINT statement sent while the test runs."""
    seen: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "SAVEPOINT"}:
            seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine, "before_cursor_execute", record)


# --- Matching writes only what changed -----------------------------------------------------------


def _document(session: Session, key: str) -> UUID:
    document, _ = save_source_document(
        session,
        SourceDocument(
            source_key=key,
            url=f"https://www.sec.gov/{key}.htm",
            publisher="SEC EDGAR",
            source_tier=SourceTier.REGULATOR_OR_SEC,
            document_type="8-K EX-99.1",
            published_at=NOW,
            retrieved_at=NOW,
            sha256=(key[-1] * 64),
            snapshot_key="sha256/" + key[-1] * 2 + "/" + key[-1] * 64,
            content_type="text/html",
        ),
    )
    return document.id


def _claim(session: Session, document: UUID, quote: str, drug: str | None, n: int) -> UUID:
    claim = Claim(
        source_document_id=document,
        subject_type=ClaimSubject.CATALYST,
        field=f"catalyst:{n:016x}",
        value={"catalyst_type": "topline_readout", "drug": drug, "title": "t", "problems": []},
        quote=quote,
        quote_start=0,
        quote_end=len(quote),
        agent="extractor",
        prompt_id="extract_catalysts",
        prompt_version="1.0.0",
        provider="anthropic",
        model="m",
        extracted_at=NOW,
    )
    session.execute(insert(TABLES[Claim]).values(**claim.model_dump()))
    return claim.id


def _drug(session: Session, company: UUID | None, name: str) -> UUID:
    drug = Drug(company_id=company, preferred_name=name)
    session.execute(insert(TABLES[Drug]).values(**drug.model_dump()))
    row = DrugName(drug_id=drug.id, name=name, kind=DrugNameKind.INN)
    session.execute(insert(TABLES[DrugName]).values(**row.model_dump()))
    return drug.id


def _matches(session_factory: sessionmaker[Session]) -> dict[tuple[UUID, str], dict[str, Any]]:
    with session_factory() as session:
        rows = session.execute(select(TABLES[EntityMatch])).mappings()
        return {(r["claim_id"], r["entity_type"]): dict(r) for r in rows}


@pytest.fixture
def release(session_factory: sessionmaker[Session]) -> dict[str, UUID]:
    with session_factory.begin() as session:
        company = upsert_company(session, 1636282, "Spyre Therapeutics, Inc.", "2834").id
        document = _document(session, "sec:spyre-release-a")
        link_company_document(session, company, document, DocumentMatch.SEC_CIK)
        _drug(session, None, "maralixibat")
    return {"company": company, "release": document}


def test_a_matching_run_that_changes_nothing_only_marks_the_matches_as_made_again(
    session_factory: sessionmaker[Session], release: dict[str, UUID], writes: list[str]
) -> None:
    with session_factory.begin() as session:
        known = _claim(session, release["release"], "Maralixibat data in Q3 2026.", "maralixibat", 1)
        unknown = _claim(session, release["release"], "Data for brelovitug in Q3 2026.", "brelovitug", 2)
        none = _claim(session, release["release"], "Data in Q3 2026.", None, 3)
    first = resolve_claims(session_factory, lambda: NOW)
    before = _matches(session_factory)
    writes.clear()
    later = NOW + timedelta(days=1)
    again = resolve_claims(session_factory, lambda: later)
    assert again.summary() == first.summary()
    assert [w.split()[0].upper() for w in writes] == ["UPDATE"]  # one statement marks them all
    after = _matches(session_factory)
    assert after.keys() == before.keys()
    for key, row in after.items():
        assert row["resolved_at"] == later
        assert {k: v for k, v in row.items() if k != "resolved_at"} == {
            k: v for k, v in before[key].items() if k != "resolved_at"
        }
    assert {claim for claim, _ in after} == {known, unknown, none}


def test_a_name_that_now_matches_a_record_is_written_on_the_next_run(
    session_factory: sessionmaker[Session], release: dict[str, UUID]
) -> None:
    with session_factory.begin() as session:
        claim = _claim(session, release["release"], "Data for brelovitug in Q3 2026.", "brelovitug", 1)
    resolve_claims(session_factory, lambda: NOW)
    assert _matches(session_factory)[(claim, "drug")]["status"] == "unknown"
    with session_factory.begin() as session:
        drug = _drug(session, release["company"], "brelovitug")
    resolve_claims(session_factory, lambda: NOW)
    row = _matches(session_factory)[(claim, "drug")]
    assert (row["status"], row["matched_id"]) == ("resolved", drug)


# --- Each copy is read once a run ----------------------------------------------------------------


class _Copies:
    def __init__(self, copies: dict[str, bytes]) -> None:
        self.copies = copies
        self.reads: list[str] = []

    def put(self, content: bytes) -> Snapshot:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        self.reads.append(key)
        if key not in self.copies:
            raise SnapshotNotFoundError(key)
        return self.copies[key]


def test_a_cached_store_reads_each_copy_once_and_prefetches_side_by_side() -> None:
    copies = _Copies({"a": b"one", "b": b"two"})
    cached = CachedStore(copies)
    assert cached.prefetch(["a", "b", "a", "missing"], workers=4) == 2
    assert sorted(copies.reads) == ["a", "b", "missing"]
    copies.reads.clear()
    assert (cached.get("a"), cached.get("b")) == (b"one", b"two")
    assert copies.reads == []


def test_a_copy_that_could_not_be_read_is_not_kept_and_its_error_comes_back() -> None:
    copies = _Copies({})
    cached = CachedStore(copies)
    cached.prefetch(["missing"])
    with pytest.raises(SnapshotNotFoundError):
        cached.get("missing")
    assert copies.reads == ["missing", "missing"]


def test_a_cached_store_keeps_no_more_than_its_limit() -> None:
    copies = _Copies({"a": b"12345", "b": b"67890"})
    cached = CachedStore(copies, max_bytes=7)
    cached.get("a")
    cached.get("b")
    copies.reads.clear()
    cached.get("a")
    cached.get("b")
    assert copies.reads == ["b"]  # only the first fitted


def test_the_copies_to_prefetch_are_those_of_extractor_claims(
    session_factory: sessionmaker[Session], release: dict[str, UUID]
) -> None:
    with session_factory.begin() as session:
        _claim(session, release["release"], "Data in Q3 2026.", None, 1)
        _claim(session, release["release"], "More data in Q3 2026.", None, 2)
        other = _document(session, "sec:no-claims-b")
        assert other
        assert claim_copy_keys(session) == ["sha256/aa/" + "a" * 64]


# --- The time budget ----------------------------------------------------------------------------


def test_a_time_budget_gives_each_share_a_deadline() -> None:
    now = [100.0]
    budget = TimeBudget(minutes=10, clock=lambda: now[0])
    assert budget.by(0.5) == 400.0
    assert not budget.past(0.5)
    now[0] = 401.0
    assert budget.past(0.5)
    assert not budget.past()
    assert budget.minutes_used() == pytest.approx(5.0167, abs=1e-3)


def test_no_decision_rests_on_a_check_that_ran_out_of_time() -> None:
    checks: dict[str, Any] = {
        "advisory": {},
        "company": {"stopped_for_time": True},
        "drug": {},
        "type": {},
        "safe_to_write": {},
    }
    nothing: Any = None  # never reached: the decisions stop before reading anything
    decided = cli.decide_after_checks(nothing, nothing, checks)
    assert decided == {"skipped": "a check ran out of time, so nothing was decided; the next run goes on"}


def _plan(max_minutes: int | None = None) -> cli.RunPlan:
    args = argparse.Namespace(time_budget_minutes=cli.TIME_BUDGET_MINUTES, max_minutes=max_minutes)
    return cli.RunPlan.of(args)


def test_a_nightly_run_gives_each_step_its_share_of_the_budget() -> None:
    plan = _plan()
    assert plan.reading == pytest.approx(35 * 0.45)
    assert plan.rest(0.7) == pytest.approx(35 * 0.7)
    assert plan.rest(1.0) == pytest.approx(35.0)


def test_a_backfill_reads_for_its_max_minutes_and_the_later_steps_keep_their_time() -> None:
    """ADR 0065's --max-minutes sets when reading stops; the checks and the gate still get theirs."""
    plan = _plan(max_minutes=170)
    assert plan.reading == 170
    assert plan.rest(0.7) == pytest.approx(170 + 35 * 0.25)
    assert plan.rest(1.0) == pytest.approx(170 + 35 * 0.55)
    assert plan.by(1.0) > plan.budget.at(170)
