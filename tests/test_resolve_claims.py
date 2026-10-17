"""Names in extracted claims are matched to companies, drugs, and trials (ADR 0015, part 4).

Every name gets one stored match: resolved, ambiguous (with every candidate), or unknown. Only
a company's own unknown code name, quoted in its own release, creates a drug; other unknown
names wait for review. Re-running updates matches as the reference tables grow.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.repository import link_company_document, save_source_document, upsert_company
from catalyst.db.tables import TABLES
from catalyst.references import record_trial
from catalyst.resolve_claims import resolve_claims
from catalyst.schemas import (
    Claim,
    ClaimSubject,
    DocumentMatch,
    Drug,
    DrugName,
    DrugNameKind,
    EntityMatch,
    MatchedEntity,
    SourceDocument,
    SourceTier,
    Trial,
    TrialPhase,
)

NOW = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)


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


@pytest.fixture
def spyre(session_factory: sessionmaker[Session]) -> dict[str, UUID]:
    """Spyre with one release, one known generic-name drug, and two named trials."""
    with session_factory.begin() as session:
        company = upsert_company(session, 1636282, "Spyre Therapeutics, Inc.", "2834").id
        release = _document(session, "sec:spyre-release-a")
        link_company_document(session, company, release, DocumentMatch.SEC_CIK)
        for nct, name in (("NCT06111111", "AZURE-1"), ("NCT06444444", "AZURE-4")):
            record_trial(session, Trial(nct_id=nct, study_name=name, phase=TrialPhase.PHASE_3))
            registry = _document(session, f"ctgov:{nct}:2026-10-01" + nct[-1])
            link_company_document(session, company, registry, DocumentMatch.CTGOV_LEAD_SPONSOR)
        known = Drug(company_id=None, preferred_name="maralixibat")
        session.execute(insert(TABLES[Drug]).values(**known.model_dump()))
        name = DrugName(drug_id=known.id, name="maralixibat", kind=DrugNameKind.INN)
        session.execute(insert(TABLES[DrugName]).values(**name.model_dump()))
    return {"company": company, "release": release, "maralixibat": known.id}


def _matches(session_factory: sessionmaker[Session]) -> list[dict[str, Any]]:
    with session_factory() as session:
        return [dict(r) for r in session.execute(select(TABLES[EntityMatch])).mappings()]


def _by(rows: list[dict[str, Any]], entity: MatchedEntity) -> dict[str, dict[str, Any]]:
    return {r["name"]: r for r in rows if r["entity_type"] == entity.value}


def test_company_drug_and_trials_are_matched(
    session_factory: sessionmaker[Session], spyre: dict[str, UUID]
) -> None:
    quote = "Topline results from AZURE-1 of maralixibat are expected in Q3 2026."
    with session_factory.begin() as session:
        _claim(session, spyre["release"], quote, "Maralixibat", 1)
    report = resolve_claims(session_factory, lambda: NOW)
    rows = _matches(session_factory)
    company = _by(rows, MatchedEntity.COMPANY)
    drug = _by(rows, MatchedEntity.DRUG)["Maralixibat"]
    trial = _by(rows, MatchedEntity.TRIAL)["AZURE-1"]
    assert [r["matched_id"] for r in company.values()] == [spyre["company"]]
    assert (drug["status"], drug["matched_id"], drug["matched_by"]) == (
        "resolved",
        spyre["maralixibat"],
        "inn",
    )
    assert (trial["status"], trial["matched_by"]) == ("resolved", "study_name")
    assert report.claims == 1


def test_a_company_s_new_code_name_in_its_own_release_creates_its_drug(
    session_factory: sessionmaker[Session], spyre: dict[str, UUID]
) -> None:
    with session_factory.begin() as session:
        _claim(session, spyre["release"], "SPY-001 induction data are expected in 2027.", "SPY-001", 1)
        _claim(session, spyre["release"], "SPY001 maintenance data follow in 2028.", "SPY001", 2)
    report = resolve_claims(session_factory, lambda: NOW)
    drugs = _by(_matches(session_factory), MatchedEntity.DRUG)
    # Whichever claim comes first creates the drug; the other finds it by the same code name.
    assert sorted(d["matched_by"] for d in drugs.values()) == ["code_name", "new_code_name"]
    assert drugs["SPY-001"]["matched_id"] == drugs["SPY001"]["matched_id"]
    with session_factory() as session:
        created = (
            session.execute(select(TABLES[Drug]).where(TABLES[Drug].c.company_id.is_not(None)))
            .mappings()
            .one()
        )
    assert created["company_id"] == spyre["company"]
    assert report.drugs_created == 1


@pytest.mark.parametrize(
    ("drug", "quote"),
    [
        ("povetacicept", "povetacicept data are expected in 2027."),
        ("SPY001 and SPY002", "SPY001 and SPY002 data are expected in 2027."),
        ("SPY-009", "Data for the lead program are expected in 2027."),
    ],
    ids=["unknown generic name", "two drugs in one name", "code name not in the quote"],
)
def test_other_unknown_drug_names_wait_for_review(
    session_factory: sessionmaker[Session], spyre: dict[str, UUID], drug: str, quote: str
) -> None:
    with session_factory.begin() as session:
        _claim(session, spyre["release"], quote, drug, 1)
    report = resolve_claims(session_factory, lambda: NOW)
    match = _by(_matches(session_factory), MatchedEntity.DRUG)[drug]
    assert (match["status"], match["matched_id"]) == ("unknown", None)
    assert report.drugs_created == 0


def test_two_registry_numbers_name_two_trials_and_an_unknown_one_is_kept(
    session_factory: sessionmaker[Session], spyre: dict[str, UUID]
) -> None:
    quote = "Results from NCT06444444 and its extension NCT09999999 are expected in Q4 2026."
    with session_factory.begin() as session:
        _claim(session, spyre["release"], quote, None, 1)
    resolve_claims(session_factory, lambda: NOW)
    trials = _by(_matches(session_factory), MatchedEntity.TRIAL)
    assert trials["NCT06444444"]["status"] == "resolved"
    assert trials["NCT09999999"]["status"] == "unknown"
    assert MatchedEntity.DRUG.value not in {r["entity_type"] for r in _matches(session_factory)}


def test_a_document_linked_to_no_company_leaves_the_company_unknown(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        release = _document(session, "sec:orphan-release-b")
        _claim(session, release, "SPY-001 data are expected in 2027.", "SPY-001", 1)
    report = resolve_claims(session_factory, lambda: NOW)
    rows = _matches(session_factory)
    assert [r["status"] for r in rows if r["entity_type"] == "company"] == ["unknown"]
    assert _by(rows, MatchedEntity.DRUG)["SPY-001"]["status"] == "unknown"
    assert report.drugs_created == 0


def test_a_re_run_adds_nothing_and_picks_up_new_names(
    session_factory: sessionmaker[Session], spyre: dict[str, UUID]
) -> None:
    with session_factory.begin() as session:
        _claim(session, spyre["release"], "Povetacicept data are expected in 2027.", "Povetacicept", 1)
    resolve_claims(session_factory, lambda: NOW)
    count = len(_matches(session_factory))
    with session_factory.begin() as session:
        drug = Drug(company_id=None, preferred_name="povetacicept")
        session.execute(insert(TABLES[Drug]).values(**drug.model_dump()))
        name = DrugName(drug_id=drug.id, name="povetacicept", kind=DrugNameKind.INN)
        session.execute(insert(TABLES[DrugName]).values(**name.model_dump()))
    resolve_claims(session_factory, lambda: NOW)
    rows = _matches(session_factory)
    assert len(rows) == count
    assert _by(rows, MatchedEntity.DRUG)["Povetacicept"]["matched_id"] == drug.id


def test_a_resolved_match_must_name_its_record(
    session_factory: sessionmaker[Session], spyre: dict[str, UUID]
) -> None:
    with session_factory.begin() as session:
        claim = _claim(session, spyre["release"], "SPY-001 data are expected in 2027.", "SPY-001", 1)
    row: dict[str, Any] = {
        "claim_id": claim,
        "entity_type": "drug",
        "name": "SPY-001",
        "status": "resolved",
        "candidates": [],
    }
    row |= {"matched_id": None, "matched_by": "code_name", "resolved_at": NOW}
    with pytest.raises(IntegrityError, match="resolved_has_record"), session_factory.begin() as session:
        session.execute(insert(TABLES[EntityMatch]).values(id=uuid4(), **row))
    with session_factory() as session:
        assert session.execute(select(func.count()).select_from(TABLES[EntityMatch])).scalar_one() == 0


# From the Opus review of PR #28: each case once produced a wrong match or a stuck run.


def test_a_release_linked_to_no_company_matches_no_study_name(
    session_factory: sessionmaker[Session], spyre: dict[str, UUID]
) -> None:
    with session_factory.begin() as session:
        wire = _document(session, "wire:other-bio-c")
        _claim(session, wire, "Other Bio expects AZURE-1 results in 2026.", None, 1)
    resolve_claims(session_factory, lambda: NOW)
    assert MatchedEntity.TRIAL.value not in {r["entity_type"] for r in _matches(session_factory)}


def test_another_company_s_code_name_is_ambiguous_not_a_new_drug(
    session_factory: sessionmaker[Session], spyre: dict[str, UUID]
) -> None:
    with session_factory.begin() as session:
        _claim(session, spyre["release"], "SPY-001 data are expected in 2027.", "SPY-001", 1)
        partner = upsert_company(session, 1000005, "Partner Pharma Inc", "2834").id
        release = _document(session, "sec:partner-release-d")
        link_company_document(session, partner, release, DocumentMatch.SEC_CIK)
        _claim(session, release, "Our partner's SPY-001 reads out in 2027.", "SPY-001", 2)
    report = resolve_claims(session_factory, lambda: NOW)
    rows = [r for r in _matches(session_factory) if r["entity_type"] == "drug"]
    assert sorted(r["status"] for r in rows) == ["ambiguous", "resolved"]
    assert report.drugs_created == 1


@pytest.mark.parametrize(
    ("drug", "quote"),
    [
        ("AB-12", "LAB-123 data are expected in 2027."),
        ("COVID-19", "COVID-19 booster data are expected in 2027."),
        ("IL-23", "IL-23 program data are expected in 2027."),
        ("FY2026", "FY2026 guidance is unchanged."),
    ],
    ids=["inside a longer code", "a disease", "a target", "a fiscal year"],
)
def test_names_that_only_look_like_code_names_create_nothing(
    session_factory: sessionmaker[Session], spyre: dict[str, UUID], drug: str, quote: str
) -> None:
    with session_factory.begin() as session:
        _claim(session, spyre["release"], quote, drug, 1)
    assert resolve_claims(session_factory, lambda: NOW).drugs_created == 0


def test_an_over_long_name_is_stored_shortened_as_unknown(
    session_factory: sessionmaker[Session], spyre: dict[str, UUID]
) -> None:
    with session_factory.begin() as session:
        _claim(session, spyre["release"], "Data are expected in 2027.", "X" * 301, 1)
    report = resolve_claims(session_factory, lambda: NOW)
    (drug,) = [r for r in _matches(session_factory) if r["entity_type"] == "drug"]
    assert (len(drug["name"]), drug["status"], report.failed) == (300, "unknown", 0)


def test_one_failing_claim_is_skipped_and_the_rest_are_matched(
    session_factory: sessionmaker[Session],
    spyre: dict[str, UUID],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import catalyst.resolve_claims as job

    with session_factory.begin() as session:
        broken = _claim(session, spyre["release"], "Data are expected in 2027.", "povetacicept", 1)
        _claim(session, spyre["release"], "Maralixibat data are expected in 2027.", "maralixibat", 2)
    original = job._store  # pyright: ignore[reportPrivateUsage]

    def store(session: Session, claim_id: UUID, *rest: Any) -> None:
        if claim_id == broken:
            raise IntegrityError("insert", {}, Exception("forced"))
        original(session, claim_id, *rest)

    monkeypatch.setattr(job, "_store", store)
    report = resolve_claims(session_factory, lambda: NOW)
    assert report.failed == 1
    assert broken not in {r["claim_id"] for r in _matches(session_factory)}
    assert _by(_matches(session_factory), MatchedEntity.DRUG)["maralixibat"]["status"] == "resolved"
    assert any(r.getMessage() == "claim_resolution_failed" for r in caplog.records)


def test_the_new_code_name_marker_survives_a_re_run(
    session_factory: sessionmaker[Session], spyre: dict[str, UUID]
) -> None:
    with session_factory.begin() as session:
        _claim(session, spyre["release"], "SPY-001 data are expected in 2027.", "SPY-001", 1)
    resolve_claims(session_factory, lambda: NOW)
    resolve_claims(session_factory, lambda: NOW)
    assert _by(_matches(session_factory), MatchedEntity.DRUG)["SPY-001"]["matched_by"] == "new_code_name"


def test_a_name_no_longer_found_loses_its_old_match(
    session_factory: sessionmaker[Session], spyre: dict[str, UUID]
) -> None:
    with session_factory.begin() as session:
        _claim(session, spyre["release"], "AZURE-1 results are expected in 2026.", None, 1)
    resolve_claims(session_factory, lambda: NOW)
    with session_factory.begin() as session:
        session.execute(TABLES[Trial].update().values(study_name="AZURE-ONE"))
    resolve_claims(session_factory, lambda: NOW)
    assert MatchedEntity.TRIAL.value not in {r["entity_type"] for r in _matches(session_factory)}


def test_a_company_found_through_a_registry_link_says_so(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        company = upsert_company(session, 1000006, "Registry Bio Inc", "2834").id
        record = _document(session, "ctgov:NCT07777777:2026-10-01e")
        link_company_document(session, company, record, DocumentMatch.CTGOV_LEAD_SPONSOR)
        _claim(session, record, "Topline data are expected in 2027.", None, 1)
    resolve_claims(session_factory, lambda: NOW)
    (company_match,) = [r for r in _matches(session_factory) if r["entity_type"] == "company"]
    assert (company_match["status"], company_match["matched_by"]) == ("resolved", "document_link")
