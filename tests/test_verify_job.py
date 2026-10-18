"""The check job runs every rule on every extractor claim and keeps each claim's latest outcome."""

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.repository import link_company_document, save_source_document, upsert_company
from catalyst.db.tables import TABLES
from catalyst.references import record_trial
from catalyst.resolve_claims import resolve_claims
from catalyst.schemas import (
    Claim,
    ClaimCheck,
    ClaimSubject,
    DocumentMatch,
    SourceDocument,
    SourceTier,
    Trial,
    TrialPhase,
)
from catalyst.verify.run import run_checks

PUBLISHED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _now(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 18, 0, tzinfo=UTC)


@pytest.fixture
def release(session_factory: sessionmaker[Session]) -> dict[str, UUID]:
    """Store a company release, with one registered Phase 2 trial named AZURE-1."""
    with session_factory.begin() as session:
        company = upsert_company(session, 1636282, "Spyre Therapeutics, Inc.", "2834").id
        document, _ = save_source_document(session, _document("sec:spyre-release-a", "a"))
        link_company_document(session, company, document.id, DocumentMatch.SEC_CIK)
        record_trial(session, Trial(nct_id="NCT06111111", study_name="AZURE-1", phase=TrialPhase.PHASE_2))
        registry, _ = save_source_document(session, _document("ctgov:NCT06111111:2026-10-01", "b"))
        link_company_document(session, company, registry.id, DocumentMatch.CTGOV_LEAD_SPONSOR)
    return {"document": document.id}


def _document(key: str, fill: str) -> SourceDocument:
    return SourceDocument(
        source_key=key,
        url=f"https://www.sec.gov/{fill}.htm",
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type="8-K EX-99.1",
        published_at=PUBLISHED,
        retrieved_at=PUBLISHED,
        sha256=fill * 64,
        snapshot_key=f"sha256/{fill * 2}/{fill * 64}",
        content_type="text/html",
    )


def _claim(session_factory: sessionmaker[Session], document: UUID, quote: str, **value: Any) -> UUID:
    stored: dict[str, Any] = {
        "catalyst_type": "topline_readout",
        "drug": None,
        "date_text": "Q4 2026",
        "window_start": "2026-10-01",
        "window_end": "2026-12-31",
        "window_precision": "quarter",
        "problems": list[str](),
    } | value
    claim = Claim(
        source_document_id=document,
        subject_type=ClaimSubject.CATALYST,
        field="catalyst:0000000000000001",
        value=stored,
        quote=quote,
        quote_start=0,
        quote_end=len(quote),
        agent="extractor",
        prompt_id="extract_catalysts",
        prompt_version="1.0.0",
        provider="anthropic",
        model="m",
        extracted_at=PUBLISHED,
    )
    with session_factory.begin() as session:
        session.execute(insert(TABLES[Claim]).values(**claim.model_dump()))
    return claim.id


def _outcomes(session_factory: sessionmaker[Session]) -> dict[str, tuple[str, str | None]]:
    with session_factory() as session:
        rows = session.execute(select(TABLES[ClaimCheck])).mappings()
        return {r["check_name"]: (r["outcome"], r["reason"]) for r in rows}


def test_every_check_is_stored_for_a_claim(
    session_factory: sessionmaker[Session], release: dict[str, UUID]
) -> None:
    _claim(session_factory, release["document"], "Phase 2 AZURE-1 topline data are expected in Q4 2026.")
    resolve_claims(session_factory, lambda: _now(date(2026, 10, 17)))
    report = run_checks(session_factory, lambda: _now(date(2026, 10, 17)))
    outcomes = _outcomes(session_factory)
    assert {name for name, (outcome, _) in outcomes.items() if outcome == "fail"} == set()
    assert outcomes["phase_matches_registry"][0] == "pass"
    assert (report.claims, report.claims_passing) == (1, 1)


def test_a_wrong_phase_is_caught_against_the_registry(
    session_factory: sessionmaker[Session], release: dict[str, UUID]
) -> None:
    _claim(session_factory, release["document"], "Phase 3 AZURE-1 topline data are expected in Q4 2026.")
    resolve_claims(session_factory, lambda: _now(date(2026, 10, 17)))
    report = run_checks(session_factory, lambda: _now(date(2026, 10, 17)))
    assert _outcomes(session_factory)["phase_matches_registry"] == (
        "fail",
        "the quote says Phase 3; the registry says Phase 2",
    )
    assert report.claims_passing == 0


def test_a_re_run_keeps_one_row_per_check_and_updates_it(
    session_factory: sessionmaker[Session], release: dict[str, UUID]
) -> None:
    _claim(session_factory, release["document"], "AZURE-1 topline data are expected in Q4 2026.")
    resolve_claims(session_factory, lambda: _now(date(2026, 10, 17)))
    run_checks(session_factory, lambda: _now(date(2026, 10, 17)))
    first = _outcomes(session_factory)
    run_checks(session_factory, lambda: _now(date(2027, 1, 5)))
    later = _outcomes(session_factory)
    assert set(first) == set(later)
    assert (first["window_not_past"][0], later["window_not_past"][0]) == ("pass", "fail")


def test_today_is_the_new_york_date(session_factory: sessionmaker[Session], release: dict[str, UUID]) -> None:
    _claim(session_factory, release["document"], "AZURE-1 topline data are expected in Q4 2026.")
    # 10pm on December 31 in New York is already January 1 in UTC; the Q4 window has not ended.
    run_checks(session_factory, lambda: datetime(2027, 1, 1, 3, 0, tzinfo=UTC))
    assert _outcomes(session_factory)["window_not_past"][0] == "pass"


def test_a_claim_whose_names_were_never_matched_fails_that_check(
    session_factory: sessionmaker[Session], release: dict[str, UUID]
) -> None:
    _claim(session_factory, release["document"], "Topline data are expected in Q4 2026.")
    run_checks(session_factory, lambda: _now(date(2026, 10, 17)))
    assert _outcomes(session_factory)["names_matched"][0] == "fail"


def test_a_failed_check_must_say_why(
    session_factory: sessionmaker[Session], release: dict[str, UUID]
) -> None:
    from uuid import uuid4

    claim = _claim(session_factory, release["document"], "Topline data are expected in Q4 2026.")
    row = {"id": uuid4(), "claim_id": claim, "check_name": "has_window", "outcome": "fail", "reason": None}
    with pytest.raises(IntegrityError, match="fail_has_reason"), session_factory.begin() as session:
        session.execute(insert(TABLES[ClaimCheck]).values(**row, checked_at=PUBLISHED))


def _company_news(session_factory: sessionmaker[Session], key: str, fill: str, tier: SourceTier) -> UUID:
    """Store a company document that is not an SEC filing, linked to Spyre.

    Any link to the company counts; how the collector made the link is not this check's concern.
    """
    with session_factory.begin() as session:
        company = upsert_company(session, 1636282, "Spyre Therapeutics, Inc.", "2834").id
        document = _document(key, fill).model_copy(
            update={
                "url": f"https://www.example-wire.com/{fill}.html",
                "source_tier": tier,
                "publisher": "Wire",
            }
        )
        stored, _ = save_source_document(session, document)
        link_company_document(session, company, stored.id, DocumentMatch.SEC_CIK)
    return stored.id


def _checks(session_factory: sessionmaker[Session], claim: UUID) -> dict[str, tuple[str, str | None]]:
    with session_factory() as session:
        rows = session.execute(select(TABLES[ClaimCheck]).where(TABLES[ClaimCheck].c.claim_id == claim))
        return {r.check_name: (r.outcome, r.reason) for r in rows}


def test_an_sec_filing_is_its_own_independent_source(
    session_factory: sessionmaker[Session], release: dict[str, UUID]
) -> None:
    claim = _claim(
        session_factory, release["document"], "Phase 2 AZURE-1 topline data are expected in Q4 2026."
    )
    run_checks(session_factory, lambda: _now(date(2026, 10, 17)))
    assert _checks(session_factory, claim)["independent_source"] == ("pass", None)


def test_a_wire_release_needs_a_second_document_from_the_same_company(
    session_factory: sessionmaker[Session], release: dict[str, UUID]
) -> None:
    quote = "Zentocel topline data are expected in Q4 2026."
    wire = _company_news(session_factory, "wire:spyre-1", "c", SourceTier.WIRE)
    alone = _claim(session_factory, wire, quote, drug="zentocel")
    run_checks(session_factory, lambda: _now(date(2026, 10, 17)))
    assert _checks(session_factory, alone)["independent_source"] == (
        "fail",
        "no second document states this event",
    )
    site = _company_news(session_factory, "site:spyre-1", "d", SourceTier.COMPANY_SITE)
    second = _claim(session_factory, site, quote, drug="Zentocel")
    report = run_checks(session_factory, lambda: _now(date(2026, 10, 17)))
    assert _checks(session_factory, alone)["independent_source"] == ("pass", None)
    assert _checks(session_factory, second)["independent_source"] == ("pass", None)
    assert report.outcomes["independent_source:pass"] == 2
