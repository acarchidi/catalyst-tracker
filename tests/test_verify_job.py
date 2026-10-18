"""The check job runs every rule on every extractor claim and keeps each claim's latest outcome."""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import insert, select, update
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
    EntityMatch,
    SourceDocument,
    SourceTier,
    Trial,
    TrialPhase,
)
from catalyst.verify.run import CheckReport, run_checks

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


def _claim(
    session_factory: sessionmaker[Session],
    document: UUID,
    quote: str,
    *,
    extracted_at: datetime = PUBLISHED,
    **value: Any,
) -> UUID:
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
        extracted_at=extracted_at,
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
    assert outcomes["trial_named_in_quote"][0] == "pass"
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


def test_a_trial_match_swapped_for_another_of_the_companys_trials_is_caught(
    session_factory: sessionmaker[Session], release: dict[str, UUID]
) -> None:
    """The planted-error test's "wrong trial": the match names a trial the quote does not."""
    _claim(session_factory, release["document"], "AZURE-1 topline data are expected in Q4 2026.")
    resolve_claims(session_factory, lambda: _now(date(2026, 10, 17)))
    with session_factory.begin() as session:
        record_trial(session, Trial(nct_id="NCT06444444", study_name="AZURE-4", phase=TrialPhase.PHASE_2))
        trial = TABLES[Trial]
        other = session.execute(select(trial.c.id).where(trial.c.nct_id == "NCT06444444")).scalar_one()
        match = TABLES[EntityMatch]
        session.execute(update(match).where(match.c.entity_type == "trial").values(matched_id=other))
    report = run_checks(session_factory, lambda: _now(date(2026, 10, 17)))
    assert _outcomes(session_factory)["trial_named_in_quote"] == (
        "fail",
        "the quote does not name the matched trial NCT06444444 (AZURE-4)",
    )
    assert report.claims_passing == 0


def test_a_failed_check_must_say_why(
    session_factory: sessionmaker[Session], release: dict[str, UUID]
) -> None:
    from uuid import uuid4

    claim = _claim(session_factory, release["document"], "Topline data are expected in Q4 2026.")
    row = {"id": uuid4(), "claim_id": claim, "check_name": "has_window", "outcome": "fail", "reason": None}
    with pytest.raises(IntegrityError, match="fail_has_reason"), session_factory.begin() as session:
        session.execute(insert(TABLES[ClaimCheck]).values(**row, checked_at=PUBLISHED))


def _company_news(
    session_factory: sessionmaker[Session], key: str, fill: str, tier: SourceTier, *, page: str | None = None
) -> UUID:
    """Store a company document that is not an SEC filing, linked to Spyre.

    Any link to the company counts; how the collector made the link is not this check's concern.
    """
    with session_factory.begin() as session:
        company = upsert_company(session, 1636282, "Spyre Therapeutics, Inc.", "2834").id
        document = _document(key, fill).model_copy(
            update={
                "url": f"https://www.example-wire.com/{page or fill}.html",
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


DEAL = {"catalyst_type": "merger_acquisition", "drug": None}
WIRE_QUOTE = "The merger is expected to close in Q4 2026."
SITE_QUOTE = "We expect the acquisition to close in the fourth quarter of 2026."


def _deal(
    session_factory: sessionmaker[Session],
    document: UUID,
    quote: str,
    *,
    date_text: str | None = None,
    extracted_at: datetime = PUBLISHED,
) -> UUID:
    written = date_text or ("Q4 2026" if "Q4 2026" in quote else "fourth quarter of 2026")
    return _claim(
        session_factory,
        document,
        quote,
        extracted_at=extracted_at,
        catalyst_type=DEAL["catalyst_type"],
        drug=None,
        date_text=written,
    )


def _run(session_factory: sessionmaker[Session]) -> CheckReport:
    resolve_claims(session_factory, lambda: _now(date(2026, 10, 17)))
    return run_checks(session_factory, lambda: _now(date(2026, 10, 17)))


def test_a_wire_release_needs_a_second_source_from_the_same_company(
    session_factory: sessionmaker[Session], release: dict[str, UUID]
) -> None:
    wire = _company_news(session_factory, "wire:spyre-1", "c", SourceTier.WIRE)
    alone = _deal(session_factory, wire, WIRE_QUOTE)
    _run(session_factory)
    assert _checks(session_factory, alone)["independent_source"] == (
        "fail",
        "no second document states this event",
    )
    site = _company_news(session_factory, "site:spyre-1", "d", SourceTier.COMPANY_SITE)
    second = _deal(session_factory, site, SITE_QUOTE)
    report = _run(session_factory)
    assert {n: o for n, (o, _) in _checks(session_factory, second).items() if o == "fail"} == {}
    assert _checks(session_factory, alone)["independent_source"] == ("pass", None)
    assert _checks(session_factory, second)["independent_source"] == ("pass", None)
    assert report.outcomes["independent_source:pass"] == 2


def test_a_claim_that_fails_a_rule_check_does_not_vouch(
    session_factory: sessionmaker[Session], release: dict[str, UUID]
) -> None:
    """The second document's quote says 2028, so its stored Q4 2026 window cannot vouch (Opus review)."""
    wire = _company_news(session_factory, "wire:spyre-1", "c", SourceTier.WIRE)
    claim = _deal(session_factory, wire, WIRE_QUOTE)
    site = _company_news(session_factory, "site:spyre-1", "d", SourceTier.COMPANY_SITE)
    _deal(session_factory, site, "We expect the acquisition to close in 2028.", date_text="2028")
    _run(session_factory)
    assert _checks(session_factory, claim)["independent_source"][0] == "fail"


def test_a_company_page_alone_is_not_enough(
    session_factory: sessionmaker[Session], release: dict[str, UUID]
) -> None:
    site = _company_news(session_factory, "site:spyre-1", "d", SourceTier.COMPANY_SITE)
    claim = _deal(session_factory, site, SITE_QUOTE)
    _run(session_factory)
    assert _checks(session_factory, claim)["independent_source"][0] == "fail"


def test_two_versions_at_one_address_are_one_source(
    session_factory: sessionmaker[Session], release: dict[str, UUID]
) -> None:
    first = _company_news(session_factory, "site:spyre-1:v1", "d", SourceTier.COMPANY_SITE)
    second = _company_news(session_factory, "site:spyre-1:v2", "e", SourceTier.COMPANY_SITE, page="d")
    claim = _deal(session_factory, first, SITE_QUOTE)
    _deal(session_factory, second, "The acquisition should close in Q4 2026.", date_text="Q4 2026")
    _run(session_factory)
    assert _checks(session_factory, claim)["independent_source"][0] == "fail"


def test_an_older_reading_of_a_document_does_not_vouch(
    session_factory: sessionmaker[Session], release: dict[str, UUID]
) -> None:
    wire = _company_news(session_factory, "wire:spyre-1", "c", SourceTier.WIRE)
    claim = _deal(session_factory, wire, WIRE_QUOTE)
    site = _company_news(session_factory, "site:spyre-1", "d", SourceTier.COMPANY_SITE)
    _deal(session_factory, site, SITE_QUOTE, extracted_at=PUBLISHED - timedelta(days=1))
    _deal(session_factory, site, "We now expect the acquisition to close in 2027.", date_text="2027")
    _run(session_factory)
    assert _checks(session_factory, claim)["independent_source"][0] == "fail"
