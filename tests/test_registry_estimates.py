"""Estimated dates from the registry (owner decision 2026-11-23; spec: Date precision; ADR 0067).

Judged against the spec, not the code: a covered company's trial with no company-stated date
gets the registry's estimated primary completion date as its own catalyst, labelled "Estimated".
Its claim quotes the field exactly as the stored registry record writes it, and plain code checks
the field at its path (spec: Provenance, rule 9). A company-stated date always wins and replaces
the estimate, with the change kept. A past estimate is never shown as upcoming.

The registry records are the recorded ARISE record (NCT04677543, Insmed) with its status fields
set as an earlier version of that record, posted while the trial ran, would have them. The
fixtures hold no recorded record of a trial still running.
"""

import copy
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import event, insert, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from catalyst import cli
from catalyst.api.access import Access
from catalyst.api.read import Query, catalyst, read_session, upcoming
from catalyst.collectors.documents import FetchedDocument, store_fetched
from catalyst.collectors.sources import ctgov
from catalyst.db.repository import upsert_company
from catalyst.db.tables import TABLES
from catalyst.extract.text import TEXT_VERSION, document_text
from catalyst.llm.prompts import load_prompt
from catalyst.publish import estimates
from catalyst.publish.estimates import (
    Estimate,
    RegistryFacts,
    Step,
    completion_window,
    field_check,
    field_quote,
    next_step,
    preview_estimates,
    publish_estimates,
)
from catalyst.references import record_trial, trial_from_study
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CatalystField,
    CatalystStatus,
    CatalystTrial,
    CatalystType,
    CheckOutcome,
    Claim,
    ClaimSubject,
    Company,
    CompanyTicker,
    DateConfidence,
    DatePrecision,
    DocumentMatch,
    SourceDocument,
    SourceTier,
    Trial,
)
from catalyst.schemas.enums import REGISTRY_ONLY_TYPES
from catalyst.snapshots import LocalSnapshotStore, SnapshotStore
from conftest import FIXTURES

TODAY = date(2026, 11, 24)
NOW = datetime(2026, 11, 24, 23, 0, tzinfo=UTC)  # 18:00 in New York
ARISE = "NCT04677543"
COMPLETION = '"primaryCompletionDateStruct":{"date":"2027-03","type":"ESTIMATED"}'


def _record(
    completion: dict[str, str] | None = None,
    status: str = "ACTIVE_NOT_RECRUITING",
    updated: str = "2026-09-01",
    phases: tuple[str, ...] = ("PHASE3",),
) -> dict[str, Any]:
    """ARISE's recorded registry record, as a version posted on `updated` while the trial ran."""
    studies = json.loads((FIXTURES / "ctgov-insmed" / "00_clinicaltrials.gov_api_v2_studies").read_text())
    study = copy.deepcopy(next(s for s in studies["studies"] if ARISE in json.dumps(s["protocolSection"])))
    study.pop("resultsSection", None)
    study["hasResults"] = False
    protocol = study["protocolSection"]
    protocol["designModule"]["phases"] = list(phases)
    module = protocol["statusModule"]
    for key in ("resultsFirstPostDateStruct", "resultsFirstSubmitDate", "resultsFirstSubmitQcDate"):
        module.pop(key, None)
    module["overallStatus"] = status
    module["primaryCompletionDateStruct"] = completion or {"date": "2027-03", "type": "ESTIMATED"}
    module["completionDateStruct"] = {"date": "2027-09", "type": "ESTIMATED"}
    module["lastUpdateSubmitDate"] = updated
    module["lastUpdatePostDateStruct"] = {"date": updated, "type": "ACTUAL"}
    return study


def _text(study: dict[str, Any]) -> str:
    return document_text(ctgov.to_document(study).content, "application/json")


# --- The field and its quote --------------------------------------------------------------------


def test_the_quote_is_the_field_exactly_as_the_stored_copy_writes_it() -> None:
    study = _record()
    text = _text(study)
    found = field_quote(text, study, estimates.COMPLETION_PATH)
    assert found is not None
    assert found.quote == COMPLETION
    assert text[found.start : found.end] == COMPLETION
    assert found.value == {"date": "2027-03", "type": "ESTIMATED"}


def test_no_quote_for_a_missing_field_or_one_the_copy_writes_twice() -> None:
    study = _record()
    del study["protocolSection"]["statusModule"]["primaryCompletionDateStruct"]
    assert field_quote(_text(study), study, estimates.COMPLETION_PATH) is None
    twice = _record()
    # The same field, written again elsewhere in the record, makes the quote's place ambiguous.
    twice["derivedSection"]["primaryCompletionDateStruct"] = {"date": "2027-03", "type": "ESTIMATED"}
    assert _text(twice).count(COMPLETION) == 2
    assert field_quote(_text(twice), twice, estimates.COMPLETION_PATH) is None


def _claim(study: dict[str, Any], **changes: Any) -> Claim:
    text = _text(study)
    found = field_quote(text, study, estimates.COMPLETION_PATH)
    assert found is not None
    values: dict[str, Any] = {
        "source_document_id": uuid4(),
        "subject_type": ClaimSubject.TRIAL,
        "field": estimates.claim_field(estimates.COMPLETION_PATH),
        "value": {
            "path": list(estimates.COMPLETION_PATH),
            "value": found.value,
            "nct_id": ARISE,
            "text_version": TEXT_VERSION,
        },
        "quote": found.quote,
        "quote_start": found.start,
        "quote_end": found.end,
        "agent": estimates.AGENT,
        "prompt_id": estimates.PROMPT[0],
        "prompt_version": estimates.PROMPT[1],
        "provider": estimates.PROVIDER,
        "model": estimates.MODEL,
        "extracted_at": NOW,
    }
    return Claim.model_validate(values | changes)


def test_the_field_check_passes_only_when_the_path_holds_the_quoted_value() -> None:
    study = _record()
    claim = _claim(study)
    assert field_check(claim, _text(study), study).outcome is CheckOutcome.PASS
    later = _record({"date": "2027-06", "type": "ESTIMATED"})  # the record no longer says March
    assert field_check(claim, _text(later), later).outcome is CheckOutcome.FAIL
    moved = claim.model_copy(update={"quote_start": claim.quote_start + 1, "quote_end": claim.quote_end + 1})
    assert field_check(moved, _text(study), study).outcome is CheckOutcome.FAIL
    other_value = dict(claim.value) | {"value": {"date": "2027-04", "type": "ESTIMATED"}}  # type: ignore[arg-type]
    assert field_check(claim.model_copy(update={"value": other_value}), _text(study), study).outcome is (
        CheckOutcome.FAIL
    )
    other_trial = dict(claim.value) | {"nct_id": "NCT00000001"}  # type: ignore[arg-type]
    assert field_check(claim.model_copy(update={"value": other_trial}), _text(study), study).outcome is (
        CheckOutcome.FAIL
    )


@pytest.mark.parametrize("prompt", [("extract_catalysts", "1.0.0"), ("verify_catalysts", "1.0.0")])
def test_no_reader_prompt_offers_a_registry_only_type(prompt: tuple[str, str]) -> None:
    # Only plain code writes a primary completion, so the planted-error test plants only the
    # types a reader can propose, and the reader can never propose this one.
    schema = load_prompt(*prompt).output_schema
    offered = set(schema["properties"]["catalysts"]["items"]["properties"]["catalyst_type"]["enum"])
    assert offered
    assert offered.isdisjoint(t.value for t in REGISTRY_ONLY_TYPES)
    assert CatalystType.PRIMARY_COMPLETION in REGISTRY_ONLY_TYPES


# --- The registry's date, read as a window -------------------------------------------------------


def test_completion_window_reads_a_month_or_a_day() -> None:
    assert completion_window("2027-03") == (date(2027, 3, 1), date(2027, 3, 31), DatePrecision.MONTH)
    assert completion_window("2028-02") == (date(2028, 2, 1), date(2028, 2, 29), DatePrecision.MONTH)
    assert completion_window("2027-03-15") == (date(2027, 3, 15), date(2027, 3, 15), DatePrecision.DAY)


@pytest.mark.parametrize("text", ["2027", "2027-13", "2027-02-30", "March 2027", " 2027-03", "2027-3", ""])
def test_completion_window_refuses_anything_else(text: str) -> None:
    assert completion_window(text) is None


@given(day=st.dates(min_value=date(2000, 1, 1), max_value=date(2099, 12, 31)))
def test_a_month_covers_the_whole_month_and_a_day_only_itself(day: date) -> None:
    start, end, precision = completion_window(day.strftime("%Y-%m")) or (None, None, None)
    assert precision is DatePrecision.MONTH
    assert start is not None
    assert end is not None
    assert start == day.replace(day=1)
    assert start <= day <= end
    assert (end + timedelta(days=1)).day == 1
    assert end.month == day.month
    assert completion_window(day.isoformat()) == (day, day, DatePrecision.DAY)


@given(text=st.text(max_size=12))
def test_completion_window_never_raises(text: str) -> None:
    window = completion_window(text)
    assert window is None or window[0] <= window[1]


# --- What to do for one trial ----------------------------------------------------------------------


def _facts(**changes: Any) -> RegistryFacts:
    values: dict[str, Any] = {
        "status": "RECRUITING",
        "phases": ("PHASE3",),
        "completion": {"date": "2027-03", "type": "ESTIMATED"},
    }
    return RegistryFacts(**(values | changes))


OPEN = Estimate(CatalystStatus.UPCOMING, (date(2027, 3, 1), date(2027, 3, 31)))


@pytest.mark.parametrize(
    ("facts", "live", "company_dated", "step"),
    [
        (_facts(), None, False, Step.CREATE),
        (_facts(), None, True, Step.NONE),  # a company-stated date always wins
        (_facts(completion={"date": "2026-10", "type": "ESTIMATED"}), None, False, Step.NONE),  # passed
        (_facts(completion={"date": "2026-11-23", "type": "ESTIMATED"}), None, False, Step.NONE),
        (_facts(completion={"date": "2026-11-24", "type": "ESTIMATED"}), None, False, Step.CREATE),
        (_facts(completion={"date": "2027-03", "type": "ACTUAL"}), None, False, Step.NONE),
        (_facts(completion=None), None, False, Step.NONE),
        (_facts(phases=("PHASE1",)), None, False, Step.NONE),
        (_facts(phases=("EARLY_PHASE1",)), None, False, Step.NONE),
        (_facts(phases=()), None, False, Step.NONE),
        (_facts(phases=("NA",)), None, False, Step.NONE),
        (_facts(phases=("PHASE1", "PHASE2")), None, False, Step.CREATE),
        (_facts(phases=("PHASE4",)), None, False, Step.CREATE),
        (_facts(status="TERMINATED"), None, False, Step.NONE),
        (_facts(status="COMPLETED"), None, False, Step.NONE),
        (_facts(status="NOT_YET_RECRUITING"), None, False, Step.CREATE),
        (_facts(), OPEN, False, Step.NONE),  # nothing changed
        (_facts(), OPEN, True, Step.REPLACE),
        (_facts(completion={"date": "2027-05", "type": "ESTIMATED"}), OPEN, False, Step.MOVE),
        (_facts(completion={"date": "2027-03-20", "type": "ESTIMATED"}), OPEN, False, Step.MOVE),  # narrows
        (_facts(completion={"date": "2027-11", "type": "ESTIMATED"}), OPEN, False, Step.HOLD),  # > 6 months
        (_facts(completion={"date": "2026-10", "type": "ESTIMATED"}), OPEN, False, Step.HOLD),  # past
        (_facts(completion={"date": "2026-11-02", "type": "ACTUAL"}), OPEN, False, Step.RESOLVE),
        (_facts(status="TERMINATED"), OPEN, False, Step.RESOLVE),
        (_facts(status="WITHDRAWN"), OPEN, False, Step.RESOLVE),
        (_facts(status="SUSPENDED"), OPEN, False, Step.RESOLVE),
        (_facts(), Estimate(CatalystStatus.REPLACED, OPEN.window), False, Step.NONE),
        (_facts(), Estimate(CatalystStatus.REPLACED, OPEN.window), True, Step.NONE),
        (_facts(), Estimate(CatalystStatus.RESOLVED, OPEN.window), False, Step.NONE),
    ],
)
def test_next_step(facts: RegistryFacts, live: Estimate | None, company_dated: bool, step: Step) -> None:
    found, reason = next_step(facts, live, company_dated, TODAY)
    assert found is step
    assert reason


# --- The job, on the database ------------------------------------------------------------------------


def _store_record(
    session_factory: sessionmaker[Session], store: SnapshotStore, study: dict[str, Any], company: UUID | None
) -> FetchedDocument:
    fetched = ctgov.to_document(study)
    link = (company, DocumentMatch.CTGOV_LEAD_SPONSOR) if company else None
    store_fetched(fetched, store, session_factory, link, lambda: NOW)
    with session_factory.begin() as session:
        record_trial(session, trial_from_study(study))
    return fetched


def _insmed(session_factory: sessionmaker[Session]) -> UUID:
    with session_factory.begin() as session:
        return upsert_company(session, 1104506, "INSMED Inc", "2834").id


def _rows(session_factory: sessionmaker[Session], model: type) -> list[dict[str, Any]]:  # type: ignore[type-arg]
    with session_factory() as session:
        return [dict(r) for r in session.execute(select(TABLES[model])).mappings()]


def _run(session_factory: sessionmaker[Session], store: SnapshotStore, at: datetime = NOW) -> Any:
    return publish_estimates(session_factory, store, lambda: at)


def _shown(session_factory: sessionmaker[Session], today: date = TODAY) -> list[Any]:
    with read_session(session_factory) as session:
        return list(upcoming(session, Query(), Access.PAID, today).catalysts)


def test_a_trial_with_no_company_date_gets_its_estimate_with_its_source(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _store_record(session_factory, store, _record(), _insmed(session_factory))
    report = _run(session_factory, store)
    assert report.created == 1
    [row] = _rows(session_factory, Catalyst)
    assert row["catalyst_type"] == CatalystType.PRIMARY_COMPLETION
    assert row["date_confidence"] == DateConfidence.REGISTRY_ESTIMATE
    assert (row["window_start"], row["window_end"]) == (date(2027, 3, 1), date(2027, 3, 31))
    assert row["window_precision"] == DatePrecision.MONTH
    assert row["window_raw_text"] == "2027-03"
    assert row["title"] == "Primary completion: ARISE trial"
    [claim] = _rows(session_factory, Claim)
    assert claim["quote"] == COMPLETION
    assert claim["agent"] == estimates.AGENT
    assert claim["provider"] == estimates.PROVIDER
    [link] = _rows(session_factory, CatalystTrial)
    assert link["claim_id"] == claim["id"]
    [shown] = _shown(session_factory)
    assert shown.window.raw_text == "2027-03"
    assert [t.nct_id for t in shown.trials] == [ARISE]
    assert {s.quote for s in shown.sources} == {COMPLETION}
    assert {s.url for s in shown.sources} == {f"https://clinicaltrials.gov/study/{ARISE}"}
    assert {s.source_tier for s in shown.sources} == {SourceTier.REGISTRY}


def test_running_again_writes_nothing_new(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    store = LocalSnapshotStore(tmp_path)
    _store_record(session_factory, store, _record(), _insmed(session_factory))
    _run(session_factory, store)
    before = [len(_rows(session_factory, m)) for m in (Catalyst, CatalystChange, Claim, CatalystTrial)]
    again = _run(session_factory, store)
    assert again.trials_read == 0  # the estimate already rests on this record: its copy is not read again
    assert again.created == again.moved == again.replaced == again.resolved == 0
    assert [
        len(_rows(session_factory, m)) for m in (Catalyst, CatalystChange, Claim, CatalystTrial)
    ] == before


def test_a_record_not_linked_to_a_covered_company_gets_nothing(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _store_record(session_factory, store, _record(), None)
    assert _run(session_factory, store).created == 0
    assert _rows(session_factory, Catalyst) == []


def test_a_phase_1_trial_gets_nothing_and_its_copy_is_not_read(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _store_record(session_factory, store, _record(phases=("PHASE1",)), _insmed(session_factory))
    report = _run(session_factory, store)
    assert (report.created, report.trials_read) == (0, 0)
    assert report.left == {"the trial is not phase 2 or later": 1}


def test_only_the_latest_stored_record_counts(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    store = LocalSnapshotStore(tmp_path)
    insmed = _insmed(session_factory)
    _store_record(session_factory, store, _record(), insmed)
    _store_record(
        session_factory,
        store,
        _record({"date": "2026-11-02", "type": "ACTUAL"}, updated="2026-11-10"),
        insmed,
    )
    assert _run(session_factory, store).created == 0  # the trial has completed; nothing to estimate
    assert _rows(session_factory, Catalyst) == []


def test_a_newer_estimate_moves_the_date_and_keeps_the_old_one_in_history(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    insmed = _insmed(session_factory)
    _store_record(session_factory, store, _record(), insmed)
    _run(session_factory, store)
    _store_record(
        session_factory,
        store,
        _record({"date": "2027-05", "type": "ESTIMATED"}, updated="2026-11-20"),
        insmed,
    )
    report = _run(session_factory, store, NOW + timedelta(hours=1))
    assert report.moved == 1
    [row] = _rows(session_factory, Catalyst)
    assert (row["window_start"], row["window_end"]) == (date(2027, 5, 1), date(2027, 5, 31))
    with read_session(session_factory) as session:
        detail = catalyst(session, row["id"], Access.PAID)
    assert detail is not None
    moves = {c.field: (c.old_value, c.new_value) for c in detail.catalyst.latest_date_change}
    assert moves["window_start"] == ("2027-03-01", "2027-05-01")
    assert moves["window_raw_text"] == ("2027-03", "2027-05")
    assert {s.quote for s in detail.catalyst.sources} >= {COMPLETION.replace("2027-03", "2027-05")}


def test_a_move_of_more_than_six_months_waits(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    store = LocalSnapshotStore(tmp_path)
    insmed = _insmed(session_factory)
    _store_record(session_factory, store, _record(), insmed)
    _run(session_factory, store)
    _store_record(
        session_factory,
        store,
        _record({"date": "2028-01", "type": "ESTIMATED"}, updated="2026-11-20"),
        insmed,
    )
    report = _run(session_factory, store, NOW + timedelta(hours=1))
    assert report.moved == 0
    assert sum(report.held.values()) == 1
    [row] = _rows(session_factory, Catalyst)
    assert row["window_start"] == date(2027, 3, 1)


def test_a_completed_trial_resolves_its_estimate(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    insmed = _insmed(session_factory)
    _store_record(session_factory, store, _record(), insmed)
    _run(session_factory, store)
    actual = _record({"date": "2026-11-02", "type": "ACTUAL"}, status="COMPLETED", updated="2026-11-20")
    _store_record(session_factory, store, actual, insmed)
    assert _run(session_factory, store, NOW + timedelta(hours=1)).resolved == 1
    [row] = _rows(session_factory, Catalyst)
    assert row["status"] == CatalystStatus.RESOLVED
    assert _shown(session_factory) == []
    status = [c for c in _rows(session_factory, CatalystChange) if c["field"] == CatalystField.STATUS]
    cited = {c["claim_id"] for c in status if c["new_value"] == "resolved"}
    quotes = {c["quote"] for c in _rows(session_factory, Claim) if c["id"] in cited}
    assert quotes == {'"primaryCompletionDateStruct":{"date":"2026-11-02","type":"ACTUAL"}'}


def _company_catalyst(
    session: Session, company: UUID, trial: UUID, learned: datetime, filing: int = 99
) -> UUID:
    """Publish a company-stated readout for the trial, written as the catalyst writer writes one."""
    doc = SourceDocument(
        source_key=f"sec:0001104506-26-{filing:06d}:ex99-1.htm",
        url=f"https://www.sec.gov/Archives/edgar/data/1104506/0001104506260{filing:05d}/ex99-1.htm",
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type="8-K EX-99.1",
        published_at=learned,
        retrieved_at=learned,
        sha256=f"{filing:064d}",
        snapshot_key=f"sha256/00/{filing:064d}",
        content_type="text/html",
    )
    session.execute(insert(TABLES[SourceDocument]).values(**doc.model_dump()))
    quote = "topline data from the ARISE trial in the first half of 2027"
    claim = Claim(
        source_document_id=doc.id,
        subject_type=ClaimSubject.CATALYST,
        field="catalyst:0123456789abcdef",
        value={"catalyst_type": "topline_readout"},
        quote=quote,
        quote_start=10,
        quote_end=10 + len(quote),
        agent="extractor",
        prompt_id="extract_catalysts",
        prompt_version="1.0.0",
        provider="anthropic",
        model="claude-test",
        extracted_at=learned,
    )
    session.execute(insert(TABLES[Claim]).values(**claim.model_dump()))
    row = Catalyst(
        catalyst_type=CatalystType.TOPLINE_READOUT,
        company_id=company,
        title="Topline data: ARISE trial",
        window_start=date(2027, 1, 1),
        window_end=date(2027, 6, 30),
        window_precision=DatePrecision.HALF,
        window_raw_text="first half of 2027",
        date_confidence=DateConfidence.COMPANY_CONFIRMED,
        status=CatalystStatus.UPCOMING,
    )
    session.execute(insert(TABLES[Catalyst]).values(**row.model_dump()))
    values = row.model_dump(mode="json")
    for name in CatalystField:
        if name is not CatalystField.DRUGS and values[name.value] is not None:
            change = CatalystChange(
                catalyst_id=row.id,
                field=name,
                new_value=values[name.value],
                claim_id=claim.id,
                learned_at=learned,
            )
            session.execute(insert(TABLES[CatalystChange]).values(**change.model_dump()))
    link = CatalystTrial(catalyst_id=row.id, trial_id=trial, claim_id=claim.id)
    session.execute(insert(TABLES[CatalystTrial]).values(**link.model_dump()))
    return claim.id


def _trial_id(session_factory: sessionmaker[Session]) -> UUID:
    [trial] = _rows(session_factory, Trial)
    return trial["id"]


def test_a_company_date_replaces_the_estimate_and_the_change_is_kept(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    insmed = _insmed(session_factory)
    _store_record(session_factory, store, _record(), insmed)
    _run(session_factory, store)
    [estimate] = _rows(session_factory, Catalyst)
    with session_factory.begin() as session:
        company_claim = _company_catalyst(
            session, insmed, _trial_id(session_factory), NOW + timedelta(hours=1)
        )
    report = _run(session_factory, store, NOW + timedelta(hours=2))
    assert report.replaced == 1
    replaced = next(r for r in _rows(session_factory, Catalyst) if r["id"] == estimate["id"])
    assert replaced["status"] == CatalystStatus.REPLACED
    [change] = [
        c
        for c in _rows(session_factory, CatalystChange)
        if c["catalyst_id"] == estimate["id"] and c["field"] == CatalystField.STATUS and c["old_value"]
    ]
    assert (change["old_value"], change["new_value"], change["claim_id"]) == (
        "upcoming",
        "replaced",
        company_claim,
    )
    shown = _shown(session_factory)
    assert [c.catalyst_type for c in shown] == [CatalystType.TOPLINE_READOUT]
    assert _run(session_factory, store, NOW + timedelta(hours=3)).created == 0  # never comes back


def test_a_trial_with_a_company_date_never_gets_an_estimate(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    insmed = _insmed(session_factory)
    _store_record(session_factory, store, _record(), insmed)
    with session_factory.begin() as session:
        _company_catalyst(session, insmed, _trial_id(session_factory), NOW - timedelta(days=1))
    assert _run(session_factory, store).created == 0
    assert [r["catalyst_type"] for r in _rows(session_factory, Catalyst)] == [CatalystType.TOPLINE_READOUT]


def test_a_passed_estimate_is_never_written_nor_shown_as_upcoming(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    insmed = _insmed(session_factory)
    _store_record(session_factory, store, _record({"date": "2026-10", "type": "ESTIMATED"}), insmed)
    assert _run(session_factory, store).created == 0
    _store_record(
        session_factory,
        store,
        _record({"date": "2026-12", "type": "ESTIMATED"}, updated="2026-11-20"),
        insmed,
    )
    assert _run(session_factory, store).created == 1
    assert len(_shown(session_factory, date(2026, 12, 31))) == 1
    assert _shown(session_factory, date(2027, 1, 1)) == []  # its window has passed


class _BrokenStore(LocalSnapshotStore):
    def get(self, key: str) -> bytes:
        return b'{"protocolSection": {}}'


def test_a_copy_that_does_not_match_its_hash_writes_nothing(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _store_record(session_factory, store, _record(), _insmed(session_factory))
    report = _run(session_factory, _BrokenStore(tmp_path))
    assert report.created == 0
    assert report.copies_unreadable == 1
    assert _rows(session_factory, Catalyst) == []


# --- The dry run: what would publish, with nothing written (ADR 0067, consequences) ---------------


def _trial(nct_id: str, name: str, **changes: Any) -> dict[str, Any]:
    """ARISE's record, renumbered and renamed as another trial of the same sponsor."""
    study = _record(**changes)
    identification = study["protocolSection"]["identificationModule"]
    identification["nctId"] = nct_id
    identification["acronym"] = name
    identification["briefTitle"] = f"A study named {name}"
    return study


_ALL_ROWS = "md5(coalesce(string_agg(r::text, chr(10) ORDER BY r::text), ''))"


def _fingerprint(session_factory: sessionmaker[Session]) -> dict[str, Any]:
    """Every row of every table, and every sequence, as Postgres writes them out as text."""
    with session_factory() as session:
        tables = session.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
        ).scalars()
        found: dict[str, Any] = {
            name: session.execute(
                text(f'SELECT count(*), {_ALL_ROWS} FROM "{name}" r')  # noqa: S608 - names from pg_tables
            ).one()
            for name in list(tables)
        }
        found["sequences"] = session.execute(
            text("SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = 'public' ORDER BY 1")
        ).all()
    return found


LATER = NOW + timedelta(hours=1)


def _every_step(session_factory: sessionmaker[Session], store: SnapshotStore) -> UUID:
    """Publish four estimates, then store what calls for each step and for each reason to skip."""
    insmed = _insmed(session_factory)
    with session_factory.begin() as session:
        session.execute(
            update(TABLES[Company]).where(TABLES[Company].c.id == insmed).values(display_name="Insmed")
        )
        ticker = CompanyTicker(
            company_id=insmed, ticker="INSM", exchange="Nasdaq", valid_from=date(2000, 6, 1)
        )
        session.execute(insert(TABLES[CompanyTicker]).values(**ticker.model_dump()))
    for nct_id, name in (
        ("NCT09000001", "MOVE"),
        ("NCT09000002", "HOLD"),
        ("NCT09000003", "CLOSE"),
        ("NCT09000004", "REPLACE"),
    ):
        _store_record(session_factory, store, _trial(nct_id, name), insmed)
    assert _run(session_factory, store).created == 4
    newer = "2026-11-20"
    later_records = [
        _trial("NCT09000001", "MOVE", completion={"date": "2027-05", "type": "ESTIMATED"}, updated=newer),
        _trial("NCT09000002", "HOLD", completion={"date": "2028-01", "type": "ESTIMATED"}, updated=newer),
        _trial(
            "NCT09000003",
            "CLOSE",
            completion={"date": "2026-11-02", "type": "ACTUAL"},
            status="COMPLETED",
            updated=newer,
        ),
        _trial("NCT09000005", "NEW", completion={"date": "2027-06", "type": "ESTIMATED"}),
        _trial("NCT09000006", "DAY", completion={"date": "2027-08-15", "type": "ESTIMATED"}),
        _trial("NCT09000007", "EARLY", phases=("PHASE1",)),
        _trial("NCT09000008", "DATED"),
        _trial("NCT09000009", "PASSED", completion={"date": "2026-10", "type": "ESTIMATED"}),
    ]
    for study in later_records:
        _store_record(session_factory, store, study, insmed)
    _store_record(session_factory, store, _trial("NCT09000010", "OTHER"), None)  # no covered company
    with session_factory.begin() as session:
        trials = {r.nct_id: r.id for r in session.execute(select(TABLES[Trial].c.nct_id, TABLES[Trial].c.id))}
        _company_catalyst(session, insmed, trials["NCT09000004"], NOW + timedelta(minutes=10), filing=98)
        _company_catalyst(session, insmed, trials["NCT09000008"], NOW + timedelta(minutes=10), filing=97)
    return insmed


EXPECTED_SKIPS = {
    "the trial is not phase 2 or later": 1,
    "the trial has a company-stated date": 1,
    "the estimated date has passed": 1,
    "the record is not linked to exactly one covered company": 1,
}


def test_the_dry_run_counts_every_step_and_writes_nothing(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _every_step(session_factory, store)
    before = _fingerprint(session_factory)
    preview = preview_estimates(session_factory, store, lambda: LATER)
    assert _fingerprint(session_factory) == before  # byte for byte: every row and every sequence
    counts = preview.counts()
    assert counts["nothing_written"] is True
    assert (counts["would_create"], counts["would_move"], counts["would_close"]) == (2, 1, 1)
    assert counts["would_hold_over_6_months"] == 1
    assert counts["would_hold_passed"] == 0
    assert counts["would_be_replaced_by_company_date"] == 1
    assert counts["skipped"] == EXPECTED_SKIPS
    # The real job, run next on the same rows, does exactly what the dry run said it would.
    report = publish_estimates(session_factory, store, lambda: LATER)
    assert report.summary() == preview.report.summary()
    assert _fingerprint(session_factory) != before


def test_the_dry_run_counts_by_month_for_the_next_12_months_and_by_company(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _every_step(session_factory, store)
    counts = preview_estimates(session_factory, store, lambda: LATER).counts()
    months = counts["by_month"]
    assert list(months) == [f"2026-{m}" for m in ("11", "12")] + [f"2027-{m:02d}" for m in range(1, 11)] + [
        "later"
    ]
    # New and moved estimates are what the issue would print: May (moved), June, and 15 August.
    assert {k: v for k, v in months.items() if v} == {"2027-05": 1, "2027-06": 1, "2027-08": 1}
    assert counts["by_company_top_20"] == [{"company": "Insmed", "ticker": "INSM", "count": 3}]


def test_the_sample_rows_read_as_the_issue_would_print_them(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _every_step(session_factory, store)
    rows = preview_estimates(session_factory, store, lambda: LATER).sample()
    assert [r["trial_number"] for r in rows] == ["NCT09000001", "NCT09000005", "NCT09000006"]
    assert rows[0] == {
        "company": "Insmed",
        "ticker": "INSM",
        "trial_number": "NCT09000001",
        "trial_title": "A study named MOVE",
        "phase": "Phase 3",
        "estimated": "Estimated May 2027",
        "registry_link": "https://clinicaltrials.gov/study/NCT09000001",
        "change": "moved",
    }
    assert (rows[1]["estimated"], rows[1]["change"]) == ("Estimated June 2027", "new")
    assert rows[2]["estimated"] == "Estimated 15 August 2027"


def test_the_dry_run_opens_only_a_read_only_transaction(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _every_step(session_factory, store)
    engine = session_factory.kw["bind"]
    statements: list[str] = []

    def seen(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement.strip().upper())

    event.listen(engine, "before_cursor_execute", seen)
    try:
        preview_estimates(session_factory, store, lambda: LATER)
    finally:
        event.remove(engine, "before_cursor_execute", seen)
    assert statements[0] == "SET TRANSACTION READ ONLY"
    assert not [s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE", "TRUNCATE"))]


def test_the_dry_run_checks_each_field_as_the_real_job_does(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    # A copy whose bytes do not match its hash is counted as unreadable, and nothing would be made.
    store = LocalSnapshotStore(tmp_path)
    _store_record(session_factory, store, _record(), _insmed(session_factory))
    counts = preview_estimates(session_factory, _BrokenStore(tmp_path), lambda: NOW).counts()
    assert counts["would_create"] == 0
    assert counts["skipped"] == {"the stored copy could not be read or did not match its hash": 1}
    # A field written twice in the copy has no one place, so no claim, and no entry, would be made.
    twice = _trial("NCT09000011", "TWICE")
    twice["derivedSection"]["primaryCompletionDateStruct"] = {"date": "2027-03", "type": "ESTIMATED"}
    _store_record(session_factory, store, twice, _insmed(session_factory))
    counts = preview_estimates(session_factory, store, lambda: NOW).counts()
    assert counts["would_create"] == 1  # ARISE
    assert counts["skipped"] == {"the field is not written once in the stored copy": 1}


@pytest.mark.parametrize("total", [0, 1, 49, 50, 51, 400])
def test_the_sample_spreads_up_to_50_rows_across_the_dates(total: int) -> None:
    picked = estimates.spread(list(range(total)), 50)
    assert len(picked) == min(total, 50)
    assert picked == sorted(set(picked))
    if total:
        assert (picked[0], picked[-1]) == (0, total - 1)


def test_the_dry_run_command_prints_counts_and_keeps_the_rows_in_the_file(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshots = tmp_path / "snapshots"
    store = LocalSnapshotStore(snapshots)
    _every_step(session_factory, store)
    before = _fingerprint(session_factory)
    monkeypatch.setenv("CATALYST_DATABASE_URL", database_url)
    monkeypatch.setattr(cli, "_utc_now", lambda: LATER)
    sample = tmp_path / "sample.json"
    argv = ["publish-estimates", "--dry-run", "--snapshot-dir", str(snapshots), "--sample-out", str(sample)]
    assert cli.main(argv) == 0
    out = capsys.readouterr()
    counts = json.loads(out.out)
    assert counts["would_create"] == 2
    # The printed counts and the logs name no trial: the rows go only to the file.
    for printed in (out.out, out.err):
        assert "NCT0900" not in printed
        assert "A study named" not in printed
    rows = json.loads(sample.read_text())["rows"]
    assert [r["trial_number"] for r in rows] == ["NCT09000001", "NCT09000005", "NCT09000006"]
    assert _fingerprint(session_factory) == before
    assert cli.main(argv) == 1  # never writes over an existing file
    assert "already exists" in capsys.readouterr().err


def test_the_sample_file_is_only_for_a_dry_run(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert cli.main(["publish-estimates", "--sample-out", str(tmp_path / "sample.json")]) == 2
    assert "--dry-run" in capsys.readouterr().err
    assert not (tmp_path / "sample.json").exists()
