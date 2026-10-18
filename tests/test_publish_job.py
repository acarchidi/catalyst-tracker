"""The decision job on a disposable database with a local snapshot store (ADR 0018, part 5a).

Every extractor claim gets one stored decision, publish or review, with every reason. A re-run
updates it in place. The job writes no catalyst rows: that is part 5b.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.documents import FetchedDocument, store_fetched
from catalyst.db.tables import TABLES
from catalyst.extract.agent import EXTRACTOR_PROMPT
from catalyst.extract.text import TEXT_VERSION, document_text
from catalyst.publish.gate import ADVISORY_CHECK, REQUIRED_CHECKS
from catalyst.publish.run import decide_claims
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CheckOutcome,
    Claim,
    ClaimCheck,
    ClaimDecision,
    ClaimSubject,
    GateDecision,
    SourceTier,
)
from catalyst.snapshots import LocalSnapshotStore
from catalyst.verify import second

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
SENTENCE = "Topline data from the Phase 3 RAINIER trial are expected in the second half of 2027."
RELEASE = f"<html><body><p>Acme Bio news</p><p>{SENTENCE}</p></body></html>".encode()


class _CountingStore(LocalSnapshotStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.reads: list[str] = []

    def get(self, key: str) -> bytes:
        self.reads.append(key)
        return super().get(key)


@pytest.fixture
def store(tmp_path: Path) -> _CountingStore:
    return _CountingStore(tmp_path)


def _release(factory: sessionmaker[Session], store: LocalSnapshotStore, key: str = "a") -> UUID:
    fetched = FetchedDocument(
        source_key=f"sec:{key}",
        url=f"https://www.sec.gov/Archives/edgar/data/1/{key}.htm",
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type="8-K EX-99.1",
        published_at=datetime(2026, 10, 14, 20, tzinfo=UTC),
        content=RELEASE.replace(b"Acme", key.encode()),
        content_type="text/html",
    )
    document, _ = store_fetched(fetched, store, factory, now=lambda: NOW)
    return document.id


def _claim(
    factory: sessionmaker[Session],
    document: UUID,
    *,
    agent: str = "extractor",
    start: int | None = None,
    field: str | None = None,
    prompt_version: str = EXTRACTOR_PROMPT[1],
    **value: Any,
) -> UUID:
    text = document_text(RELEASE.replace(b"Acme", b"a"), "text/html")
    start = text.index(SENTENCE) if start is None else start
    stored: dict[str, Any] = {
        "catalyst_type": "topline_readout",
        "problems": [],
        "text_version": TEXT_VERSION,
    }
    claim = Claim(
        source_document_id=document,
        subject_type=ClaimSubject.CATALYST,
        field=field or f"catalyst:{agent}:{start}",
        value=stored | value,
        quote=SENTENCE,
        quote_start=start,
        quote_end=start + len(SENTENCE),
        agent=agent,
        prompt_id=EXTRACTOR_PROMPT[0],
        prompt_version=prompt_version,
        provider="anthropic",
        model="claude-sonnet-5-5",
        extracted_at=NOW,
    )
    with factory.begin() as session:
        session.execute(insert(TABLES[Claim]).values(**claim.model_dump()))
    return claim.id


def _checks(
    factory: sessionmaker[Session],
    claim: UUID,
    *,
    skip: tuple[str, ...] = (),
    fail: dict[str, str] | None = None,
) -> None:
    rows: list[dict[str, Any]] = []
    for name in REQUIRED_CHECKS:
        if name in skip:
            continue
        reason = (fail or {}).get(name)
        outcome = CheckOutcome.FAIL if reason else CheckOutcome.PASS
        check = ClaimCheck(claim_id=claim, check_name=name, outcome=outcome, reason=reason, checked_at=NOW)
        rows.append(check.model_dump())
    with factory.begin() as session:
        session.execute(insert(TABLES[ClaimCheck]).values(rows))


def _decisions(factory: sessionmaker[Session]) -> dict[UUID, tuple[str, list[str], datetime]]:
    table = TABLES[ClaimDecision]
    with factory() as session:
        rows = session.execute(
            select(table.c.claim_id, table.c.decision, table.c.reasons, table.c.decided_at)
        )
        return {row.claim_id: (row.decision, row.reasons, row.decided_at) for row in rows}


def test_a_claim_with_every_check_passing_and_its_quote_found_is_marked_to_publish(
    session_factory: sessionmaker[Session], store: _CountingStore
) -> None:
    claim = _claim(session_factory, _release(session_factory, store))
    _checks(session_factory, claim)
    report = decide_claims(session_factory, store, lambda: NOW)
    assert _decisions(session_factory) == {claim: ("publish", [], NOW)}
    assert report.summary() == {
        "claims": 1,
        "to_publish": 1,
        "to_review": 0,
        "copies_unreadable": 0,
        "top_reasons": {},
    }


def test_the_job_writes_no_catalyst_or_change_rows(
    session_factory: sessionmaker[Session], store: _CountingStore
) -> None:
    claim = _claim(session_factory, _release(session_factory, store))
    _checks(session_factory, claim)
    decide_claims(session_factory, store, lambda: NOW)
    with session_factory() as session:
        for model in (Catalyst, CatalystChange):
            assert session.execute(select(func.count()).select_from(TABLES[model])).scalar_one() == 0


def test_every_reason_is_stored_and_counted(
    session_factory: sessionmaker[Session], store: _CountingStore
) -> None:
    document = _release(session_factory, store)
    first, other = _claim(session_factory, document), _claim(session_factory, document, start=0)
    _checks(
        session_factory, first, skip=(ADVISORY_CHECK,), fail={"has_window": "the date words name no window"}
    )
    _checks(session_factory, other, skip=(ADVISORY_CHECK,))
    report = decide_claims(session_factory, store, lambda: NOW)
    decisions = _decisions(session_factory)
    assert decisions[first][:2] == (
        "review",
        ["has_window: the date words name no window", f"check {ADVISORY_CHECK} has not run"],
    )
    assert decisions[other][:2] == (
        "review",
        ["the quote is not at its stored place in the stored copy", f"check {ADVISORY_CHECK} has not run"],
    )
    summary = report.summary()
    assert (summary["claims"], summary["to_publish"], summary["to_review"]) == (2, 0, 2)
    assert summary["top_reasons"] == {
        f"{ADVISORY_CHECK} has not run": 2,
        "has_window failed": 1,
        "quote_matches_stored_copy failed": 1,
    }
    assert store.reads == [store.reads[0]]  # each copy is read once per run


def test_a_rerun_updates_each_decision_in_place(
    session_factory: sessionmaker[Session], store: _CountingStore
) -> None:
    claim = _claim(session_factory, _release(session_factory, store))
    _checks(session_factory, claim)
    decide_claims(session_factory, store, lambda: NOW)
    later = NOW + timedelta(hours=5)
    check = TABLES[ClaimCheck]
    with session_factory.begin() as session:
        session.execute(
            update(check)
            .where(check.c.claim_id == claim, check.c.check_name == second.CHECK)
            .values(outcome="fail", reason="the second reading did not find this event")
        )
    decide_claims(session_factory, store, lambda: later)
    decide_claims(session_factory, store, lambda: later)
    assert _decisions(session_factory) == {
        claim: ("review", [f"{second.CHECK}: the second reading did not find this event"], later)
    }


def test_a_missing_copy_means_review(
    session_factory: sessionmaker[Session], store: _CountingStore, tmp_path: Path
) -> None:
    claim = _claim(session_factory, _release(session_factory, store))
    _checks(session_factory, claim)
    report = decide_claims(session_factory, LocalSnapshotStore(tmp_path / "empty"), lambda: NOW)
    assert _decisions(session_factory)[claim][:2] == ("review", ["the stored copy is missing"])
    assert report.summary()["copies_unreadable"] == 1


def test_a_changed_copy_means_review(
    session_factory: sessionmaker[Session], store: _CountingStore, tmp_path: Path
) -> None:
    claim = _claim(session_factory, _release(session_factory, store))
    _checks(session_factory, claim)
    (key,) = [p for p in tmp_path.rglob("*") if p.is_file()]
    key.write_bytes(b"<p>something else</p>")
    decide_claims(session_factory, store, lambda: NOW)
    assert _decisions(session_factory)[claim][:2] == (
        "review",
        ["the stored copy no longer matches its hash"],
    )


def test_an_old_text_version_means_review(
    session_factory: sessionmaker[Session], store: _CountingStore
) -> None:
    claim = _claim(session_factory, _release(session_factory, store), text_version="0")
    _checks(session_factory, claim)
    decide_claims(session_factory, store, lambda: NOW)
    decision, reasons, _ = _decisions(session_factory)[claim]
    assert decision == "review"
    assert reasons == [
        f"the quote's place was found with text version 0, not the current {TEXT_VERSION}; "
        "it must be found again"
    ]


def test_only_extractor_claims_get_a_decision(
    session_factory: sessionmaker[Session], store: _CountingStore
) -> None:
    document = _release(session_factory, store)
    claim = _claim(session_factory, document)
    _claim(session_factory, document, agent="verifier")
    decide_claims(session_factory, store, lambda: NOW)
    assert list(_decisions(session_factory)) == [claim]


def test_an_extractor_claim_that_is_not_a_catalyst_gets_no_decision(
    session_factory: sessionmaker[Session], store: _CountingStore
) -> None:
    document = _release(session_factory, store)
    claim = _claim(session_factory, document)
    _claim(session_factory, document, field="window", start=0)
    assert decide_claims(session_factory, store, lambda: NOW).summary()["claims"] == 1
    assert list(_decisions(session_factory)) == [claim]


def test_a_claim_from_an_older_prompt_version_waits_for_review(
    session_factory: sessionmaker[Session], store: _CountingStore
) -> None:
    claim = _claim(session_factory, _release(session_factory, store), prompt_version="0.9.0")
    _checks(session_factory, claim)
    report = decide_claims(session_factory, store, lambda: NOW)
    current = "/".join(EXTRACTOR_PROMPT)
    assert _decisions(session_factory)[claim][:2] == (
        "review",
        [f"the claim came from prompt {EXTRACTOR_PROMPT[0]}/0.9.0, not the current {current}"],
    )
    assert report.summary()["top_reasons"] == {"extractor_prompt failed": 1}


def test_no_claims_is_an_empty_report(session_factory: sessionmaker[Session], store: _CountingStore) -> None:
    assert decide_claims(session_factory, store, lambda: NOW).summary()["claims"] == 0
    assert _decisions(session_factory) == {}


# --- The table itself refuses a decision that does not explain itself ---


@pytest.mark.parametrize(
    ("decision", "reasons"),
    [("review", []), ("publish", ["a reason"]), ("review", {"a": 1}), ("review", "a reason"), ("maybe", [])],
    ids=[
        "review without a reason",
        "publish with a reason",
        "reasons not a list",
        "reasons a string",
        "unknown",
    ],
)
def test_the_database_refuses_an_unexplained_decision(
    session_factory: sessionmaker[Session], store: _CountingStore, decision: str, reasons: object
) -> None:
    claim = _claim(session_factory, _release(session_factory, store))
    row = {"id": claim, "claim_id": claim, "decision": decision, "reasons": reasons, "decided_at": NOW}
    with pytest.raises(IntegrityError), session_factory.begin() as session:
        session.execute(insert(TABLES[ClaimDecision]).values(**row))


def test_one_decision_per_claim(session_factory: sessionmaker[Session], store: _CountingStore) -> None:
    claim = _claim(session_factory, _release(session_factory, store))
    first = ClaimDecision(claim_id=claim, decision=GateDecision.PUBLISH, reasons=[], decided_at=NOW)
    again = ClaimDecision(claim_id=claim, decision=GateDecision.REVIEW, reasons=["x"], decided_at=NOW)
    with session_factory.begin() as session:
        session.execute(insert(TABLES[ClaimDecision]).values(**first.model_dump()))
    with pytest.raises(IntegrityError), session_factory.begin() as session:
        session.execute(insert(TABLES[ClaimDecision]).values(**again.model_dump()))


@pytest.mark.parametrize(
    ("decision", "reasons"),
    [(GateDecision.REVIEW, []), (GateDecision.PUBLISH, ["x"]), (GateDecision.REVIEW, [""])],
    ids=["review without a reason", "publish with a reason", "a blank reason"],
)
def test_the_model_refuses_an_unexplained_decision(decision: GateDecision, reasons: list[str]) -> None:
    with pytest.raises(ValueError, match=r"reason|at least 1 character"):
        ClaimDecision(claim_id=UUID(int=1), decision=decision, reasons=reasons, decided_at=NOW)
