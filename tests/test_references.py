"""Reference tables are filled from records the collectors already fetch (ADR 0015, part 3).

Company names and tickers come from SEC company records. Trials, their study names, and their
phases come from ClinicalTrials.gov records. Each job is safe to re-run. The resolver reads
the filled tables.
"""

import json
import logging
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.documents import CollectionStats, FetchedDocument
from catalyst.collectors.run import Collector, PilotCompany, build_clients, collect_all
from catalyst.collectors.sources import ctgov
from catalyst.db.repository import link_company_document, save_source_document, upsert_company
from catalyst.db.tables import TABLES
from catalyst.references import (
    CompanyIdentity,
    company_identity,
    load_reference_data,
    record_company_identity,
    record_trial,
    recording_trials,
    trial_from_study,
)
from catalyst.resolve import Status
from catalyst.schemas import (
    CompanyName,
    CompanyTicker,
    DocumentMatch,
    Drug,
    DrugName,
    DrugNameKind,
    EnrollmentType,
    SourceDocument,
    SourceTier,
    Trial,
    TrialPhase,
)
from catalyst.snapshots import LocalSnapshotStore, Snapshot, SnapshotStore, SnapshotStoreError
from conftest import FIXTURES, RecordedServer

TODAY = date(2026, 10, 17)
TODAY_TIME = datetime(2026, 10, 17, 15, 0, tzinfo=UTC)
VERTEX_SUBMISSIONS = FIXTURES / "sec" / "875320" / "submissions_CIK0000875320.json"


def _studies() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "ctgov-insmed" / "00_clinicaltrials.gov_api_v2_studies").read_text())[
        "studies"
    ]


def _rows(session_factory: sessionmaker[Session], model: type) -> list[dict[str, Any]]:  # type: ignore[type-arg]
    with session_factory() as session:
        return [dict(r) for r in session.execute(select(TABLES[model])).mappings()]


def _company(session_factory: sessionmaker[Session], cik: int, name: str) -> UUID:
    with session_factory.begin() as session:
        return upsert_company(session, cik, name, "2834").id


def _identity(name: str, tickers: tuple[tuple[str, str | None], ...]) -> CompanyIdentity:
    return CompanyIdentity(name=name, former_names=(), tickers=tickers)


def _record(
    session_factory: sessionmaker[Session], company: UUID, identity: CompanyIdentity, on: date
) -> None:
    with session_factory.begin() as session:
        record_company_identity(session, company, identity, on)


# SEC company records


def test_sec_record_gives_the_name_and_tickers() -> None:
    identity = company_identity(VERTEX_SUBMISSIONS.read_bytes())
    assert identity == CompanyIdentity(
        name="VERTEX PHARMACEUTICALS INC / MA", former_names=(), tickers=(("VRTX", "Nasdaq"),)
    )


def test_former_names_keep_their_dates() -> None:
    record = {
        "cik": "0001636282",
        "name": "Spyre Therapeutics, Inc.",
        "tickers": ["SYRE"],
        "exchanges": ["Nasdaq"],
        "formerNames": [
            {
                "name": "Aeglea BioTherapeutics, Inc.",
                "from": "2015-03-05T00:00:00.000Z",
                "to": "2023-11-27T00:00:00.000Z",
            }
        ],
    }
    identity = company_identity(json.dumps(record).encode())
    assert identity.former_names == (("Aeglea BioTherapeutics, Inc.", date(2015, 3, 5), date(2023, 11, 27)),)


def test_names_and_tickers_are_recorded_once(session_factory: sessionmaker[Session]) -> None:
    vertex = _company(session_factory, 875320, "VERTEX PHARMACEUTICALS INC / MA")
    identity = company_identity(VERTEX_SUBMISSIONS.read_bytes())
    _record(session_factory, vertex, identity, TODAY)
    _record(session_factory, vertex, identity, date(2026, 10, 20))
    tickers = _rows(session_factory, CompanyTicker)
    assert [(t["ticker"], t["exchange"], t["valid_from"], t["valid_to"]) for t in tickers] == [
        ("VRTX", "Nasdaq", TODAY, None)
    ]
    assert [n["name"] for n in _rows(session_factory, CompanyName)] == ["VERTEX PHARMACEUTICALS INC / MA"]


def test_a_dropped_ticker_is_closed_the_day_before(session_factory: sessionmaker[Session]) -> None:
    spyre = _company(session_factory, 1636282, "Spyre Therapeutics, Inc.")
    _record(
        session_factory, spyre, _identity("Spyre Therapeutics, Inc.", (("AGLE", "Nasdaq"),)), date(2023, 1, 2)
    )
    _record(
        session_factory,
        spyre,
        _identity("Spyre Therapeutics, Inc.", (("SYRE", "Nasdaq"),)),
        date(2023, 11, 28),
    )
    held = {t["ticker"]: (t["valid_from"], t["valid_to"]) for t in _rows(session_factory, CompanyTicker)}
    assert held == {"AGLE": (date(2023, 1, 2), date(2023, 11, 27)), "SYRE": (date(2023, 11, 28), None)}


def test_a_ticker_that_moves_company_is_closed_for_the_old_holder(
    session_factory: sessionmaker[Session],
) -> None:
    old = _company(session_factory, 1000001, "Old Holder Inc")
    new = _company(session_factory, 1000002, "New Holder Inc")
    _record(session_factory, old, _identity("Old Holder Inc", (("ABCD", "Nasdaq"),)), date(2024, 1, 2))
    _record(session_factory, new, _identity("New Holder Inc", (("ABCD", "NYSE"),)), date(2025, 6, 2))
    rows = sorted(_rows(session_factory, CompanyTicker), key=lambda t: t["valid_from"])
    assert [(t["company_id"], t["valid_from"], t["valid_to"]) for t in rows] == [
        (old, date(2024, 1, 2), date(2025, 6, 1)),
        (new, date(2025, 6, 2), None),
    ]


# ClinicalTrials.gov records


def test_a_registry_record_gives_number_study_name_and_phase() -> None:
    trials = {t.nct_id: t for t in map(trial_from_study, _studies())}
    assert (trials["NCT04677543"].study_name, trials["NCT04677543"].phase) == ("ARISE", TrialPhase.PHASE_3)
    assert (trials["NCT00102531"].study_name, trials["NCT00102531"].phase) == (None, TrialPhase.PHASE_1_2)


@pytest.mark.parametrize(
    ("phases", "expected"),
    [
        ([], None),
        (["EARLY_PHASE1"], TrialPhase.EARLY_PHASE_1),
        (["PHASE2", "PHASE3"], TrialPhase.PHASE_2_3),
        (["NA"], TrialPhase.NOT_APPLICABLE),
        (["PHASE1", "PHASE3"], None),
    ],
)
def test_registry_phases_map_to_ours_or_to_nothing(phases: list[str], expected: TrialPhase | None) -> None:
    study = _studies()[0]
    study["protocolSection"]["designModule"]["phases"] = phases
    assert trial_from_study(study).phase is expected


def test_a_registry_record_gives_the_trial_size_and_whether_it_is_final() -> None:
    trials = {t.nct_id: t for t in map(trial_from_study, _studies())}
    arise = trials["NCT04677543"]
    assert (arise.enrollment, arise.enrollment_type) == (99, EnrollmentType.ACTUAL)
    # On the second page, the registry gives NCT00351221's count without saying actual or estimated.
    page = json.loads((FIXTURES / "ctgov-insmed" / "01_clinicaltrials.gov_api_v2_studies").read_text())
    untyped = {t.nct_id: t for t in map(trial_from_study, page["studies"])}["NCT00351221"]
    assert (untyped.enrollment, untyped.enrollment_type) == (24, None)


@pytest.mark.parametrize(
    ("enrollment_info", "expected"),
    [
        ({"count": 300, "type": "ESTIMATED"}, (300, EnrollmentType.ESTIMATED)),
        ({"count": 0, "type": "ACTUAL"}, (0, EnrollmentType.ACTUAL)),
        ({"type": "ESTIMATED"}, (None, None)),  # no count: nothing to say how sure it is about
        (None, (None, None)),
        ({"count": 12, "type": "SOMETHING_NEW"}, (12, None)),
    ],
)
def test_registry_trial_sizes_map_to_ours_or_to_nothing(
    enrollment_info: dict[str, Any] | None, expected: tuple[int | None, EnrollmentType | None]
) -> None:
    study = _studies()[0]
    design = study["protocolSection"]["designModule"]
    design.pop("enrollmentInfo")
    if enrollment_info is not None:
        design["enrollmentInfo"] = enrollment_info
    trial = trial_from_study(study)
    assert (trial.enrollment, trial.enrollment_type) == expected


def test_a_negative_trial_size_is_refused() -> None:
    with pytest.raises(ValidationError):
        Trial(nct_id="NCT04677543", enrollment=-1)


def test_a_trial_is_stored_once_and_follows_registry_updates(session_factory: sessionmaker[Session]) -> None:
    study = _studies()[0]
    with session_factory.begin() as session:
        record_trial(session, trial_from_study(study))
    study["protocolSection"]["identificationModule"]["acronym"] = "ARISE-2"
    with session_factory.begin() as session:
        record_trial(session, trial_from_study(study))
    assert [(t["nct_id"], t["study_name"]) for t in _rows(session_factory, Trial)] == [
        ("NCT04677543", "ARISE-2")
    ]


def test_a_changed_trial_size_replaces_the_old_one_without_a_second_row(
    session_factory: sessionmaker[Session],
) -> None:
    study = _studies()[0]
    study["protocolSection"]["designModule"]["enrollmentInfo"] = {"count": 120, "type": "ESTIMATED"}
    with session_factory.begin() as session:
        record_trial(session, trial_from_study(study))
    study["protocolSection"]["designModule"]["enrollmentInfo"] = {"count": 99, "type": "ACTUAL"}
    for _ in range(2):  # re-collecting the same record changes nothing
        with session_factory.begin() as session:
            record_trial(session, trial_from_study(study))
    assert [(t["nct_id"], t["enrollment"], t["enrollment_type"]) for t in _rows(session_factory, Trial)] == [
        ("NCT04677543", 99, "actual")
    ]


def test_the_database_refuses_a_negative_trial_size(engine: Engine) -> None:
    with (
        pytest.raises(IntegrityError, match="ck_trial_enrollment_not_negative"),
        engine.begin() as connection,
    ):
        connection.execute(insert(TABLES[Trial]).values(id=uuid4(), nct_id="NCT04677543", enrollment=-1))


def test_trials_are_recorded_as_registry_records_are_collected(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    server = RecordedServer("ctgov-insmed")
    clients = build_clients("data@example.org", server.transport, rate_limits=False)
    insmed = _company(session_factory, 1104506, "INSMED Inc")
    trials = partial(ctgov.collect_sponsor_studies, clients.web, frozenset({"Insmed Incorporated"}), 2, 5)
    collector = Collector(LocalSnapshotStore(tmp_path), session_factory, lambda: TODAY_TIME)
    collector.run(
        "ctgov:INSM",
        partial(recording_trials, session_factory, trials),
        (insmed, DocumentMatch.CTGOV_LEAD_SPONSOR),
    )
    with session_factory() as session:
        assert session.execute(select(func.count()).select_from(TABLES[Trial])).scalar_one() == 9
        sizes = select(TABLES[Trial].c.enrollment, TABLES[Trial].c.enrollment_type)
        encore = sizes.where(TABLES[Trial].c.nct_id == "NCT04677569")
        assert session.execute(encore).one() == (425, "actual")
        references = load_reference_data(session)
    arise = references.resolve_trial("ARISE", company_id=insmed)
    assert arise.status is Status.RESOLVED
    assert (
        references.resolve_trial("ARISE", company_id=_company(session_factory, 1, "Other")).status
        is Status.UNKNOWN
    )


# The whole run


def test_the_collection_run_records_names_and_tickers(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    server = RecordedServer("sec/875320")
    clients = build_clients("data@example.org", server.transport, rate_limits=False)
    collect_all(
        [PilotCompany(ticker="VRTX", cik=875320)],
        clients,
        LocalSnapshotStore(tmp_path),
        session_factory,
        date(2026, 8, 1),
        now=lambda: TODAY_TIME,
        sec_forms=frozenset({"8-K"}),
    )
    with session_factory() as session:
        references = load_reference_data(session)
    by_ticker = references.resolve_company(ticker="VRTX", on=TODAY)
    by_name = references.resolve_company(name="Vertex Pharmaceuticals Incorporated")
    assert by_ticker.status is Status.RESOLVED
    assert by_name.id == by_ticker.id


def test_loading_reads_names_tickers_trials_and_their_sponsors(
    session_factory: sessionmaker[Session],
) -> None:
    spyre = _company(session_factory, 1636282, "Spyre Therapeutics, Inc.")
    identity = CompanyIdentity(
        name="Spyre Therapeutics, Inc.",
        former_names=(("Aeglea BioTherapeutics, Inc.", date(2015, 3, 5), date(2023, 11, 27)),),
        tickers=(("SYRE", "Nasdaq"),),
    )
    _record(session_factory, spyre, identity, TODAY)
    with session_factory.begin() as session:
        record_trial(session, Trial(nct_id="NCT06111111", study_name="SKYLINE", phase=TrialPhase.PHASE_2))
        document, _ = save_source_document(session, _registry_document("NCT06111111"))
        link_company_document(session, spyre, document.id, DocumentMatch.CTGOV_LEAD_SPONSOR)
    with session_factory() as session:
        references = load_reference_data(session)
    assert references.resolve_company(name="Aeglea BioTherapeutics").id == spyre
    assert references.resolve_company(ticker="SYRE").id == spyre
    assert references.resolve_trial("skyline", company_id=spyre).status is Status.RESOLVED


def _registry_document(nct_id: str) -> SourceDocument:
    return SourceDocument(
        source_key=f"ctgov:{nct_id}:2026-10-01",
        url=f"https://clinicaltrials.gov/study/{nct_id}",
        publisher="ClinicalTrials.gov",
        source_tier=SourceTier.REGISTRY,
        document_type="Registry record",
        published_at=TODAY_TIME,
        retrieved_at=TODAY_TIME,
        sha256="b" * 64,
        snapshot_key="sha256/bb/" + "b" * 64,
        content_type="application/json",
    )


def test_loaded_code_names_still_count_only_for_their_company(session_factory: sessionmaker[Session]) -> None:
    spyre = _company(session_factory, 1636282, "Spyre Therapeutics, Inc.")
    other = _company(session_factory, 1000003, "Other Bio Inc")
    drug = Drug(company_id=spyre, preferred_name="SPY001")
    with session_factory.begin() as session:
        session.execute(insert(TABLES[Drug]).values(**drug.model_dump()))
        name = DrugName(drug_id=drug.id, name="SPY001", kind=DrugNameKind.CODE)
        session.execute(insert(TABLES[DrugName]).values(**name.model_dump()))
    with session_factory() as session:
        references = load_reference_data(session)
    assert references.resolve_drug("SPY-001", company_id=spyre).id == drug.id
    assert references.resolve_drug("SPY-001", company_id=other).status is Status.UNKNOWN


# Bad outside data never stops a run


def test_names_and_tickers_that_break_the_rules_are_skipped(
    session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    company = _company(session_factory, 1000004, "Odd Records Inc")
    identity = CompanyIdentity(
        name="Odd Records Inc",
        former_names=(
            ("Backwards Dates Inc", date(2020, 1, 2), date(2019, 1, 1)),
            ("Fine Old Name Inc", None, None),
        ),
        tickers=(("BAD/TICKER", "Nasdaq"), ("GOOD", "An exchange name far longer than twenty characters")),
    )
    _record(session_factory, company, identity, TODAY)
    assert sorted(n["name"] for n in _rows(session_factory, CompanyName)) == [
        "Fine Old Name Inc",
        "Odd Records Inc",
    ]
    assert [(t["ticker"], t["exchange"]) for t in _rows(session_factory, CompanyTicker)] == [("GOOD", None)]
    assert sum(r.getMessage() == "reference_skipped" for r in caplog.records) == 2


def test_a_failure_recording_names_alerts_and_the_run_goes_on(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import catalyst.collectors.run as run

    def broken(*_: object) -> None:
        raise RuntimeError("names table unavailable")

    monkeypatch.setattr(run, "record_company_identity", broken)
    server = RecordedServer("sec/875320", "wires-prnewswire")
    clients = build_clients("data@example.org", server.transport, rate_limits=False)
    stats = collect_all(
        [PilotCompany(ticker="VRTX", cik=875320)],
        clients,
        LocalSnapshotStore(tmp_path),
        session_factory,
        date(2026, 8, 1),
        now=lambda: TODAY_TIME,
        sec_forms=frozenset({"8-K"}),
    )
    by_source = {s.source: s for s in stats}
    assert by_source["names:VRTX"].error == "RuntimeError: names table unavailable"
    assert by_source["sec:VRTX"].created == 1
    assert by_source["wire:prnewswire"].created == 20
    assert any(r.getMessage() == "collector_failed" for r in caplog.records)


def test_one_bad_registry_record_is_skipped_and_the_rest_are_recorded(
    session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    caplog.set_level(logging.INFO)
    good, bad = _studies()[0], _studies()[1]
    bad["protocolSection"]["identificationModule"]["acronym"] = "X" * 301
    documents = [ctgov.to_document(bad), ctgov.to_document(good)]
    collector = Collector(LocalSnapshotStore(tmp_path), session_factory, lambda: TODAY_TIME)
    stats = collector.run("ctgov:INSM", partial(recording_trials, session_factory, lambda: documents))
    assert stats.created == 2  # both records are still stored
    assert [t["nct_id"] for t in _rows(session_factory, Trial)] == [
        good["protocolSection"]["identificationModule"]["nctId"]
    ]
    assert any(r.getMessage() == "reference_skipped" for r in caplog.records)


class _FailingStore:
    """A snapshot store that cannot store anything, as when storage is down."""

    def put(self, content: bytes) -> Snapshot:
        raise SnapshotStoreError("storage unavailable")

    def get(self, key: str) -> bytes:
        raise SnapshotStoreError("storage unavailable")


def _version(count: int, kind: str, updated: str) -> FetchedDocument:
    """ARISE's registry record, as a version posted on `updated` with this size."""
    study = _studies()[0]
    study["protocolSection"]["designModule"]["enrollmentInfo"] = {"count": count, "type": kind}
    status = study["protocolSection"]["statusModule"]
    status["lastUpdateSubmitDate"] = updated
    status["lastUpdatePostDateStruct"]["date"] = updated
    return ctgov.to_document(study)


def _collect(
    session_factory: sessionmaker[Session], store: SnapshotStore, documents: list[FetchedDocument]
) -> CollectionStats:
    collector = Collector(store, session_factory, lambda: TODAY_TIME)
    return collector.run("ctgov:INSM", partial(recording_trials, session_factory, lambda: documents))


def test_a_trial_changes_only_once_its_registry_record_is_stored(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The values shown never come from a newer record than the stored one they point to (ADR 0039)."""
    _collect(session_factory, LocalSnapshotStore(tmp_path), [_version(120, "ESTIMATED", "2026-03-02")])
    failed = _collect(session_factory, _FailingStore(), [_version(99, "ACTUAL", "2026-09-14")])
    assert failed.error is not None
    rows = _rows(session_factory, Trial)
    assert [(t["enrollment"], t["enrollment_type"]) for t in rows] == [(120, "estimated")]
    keys = [d["source_key"] for d in _rows(session_factory, SourceDocument)]
    assert keys == ["ctgov:NCT04677543:2026-03-02"]
    _collect(session_factory, LocalSnapshotStore(tmp_path), [_version(99, "ACTUAL", "2026-09-14")])
    assert [(t["enrollment"], t["enrollment_type"]) for t in _rows(session_factory, Trial)] == [
        (99, "actual")
    ]


def test_a_new_trial_whose_record_cannot_be_stored_is_not_recorded(
    session_factory: sessionmaker[Session],
) -> None:
    _collect(session_factory, _FailingStore(), [_version(99, "ACTUAL", "2026-09-14")])
    assert _rows(session_factory, Trial) == []


def test_a_registry_change_to_phase_or_size_is_logged(
    session_factory: sessionmaker[Session], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    store = LocalSnapshotStore(tmp_path)
    _collect(session_factory, store, [_version(120, "ESTIMATED", "2026-03-02")])
    _collect(session_factory, store, [_version(120, "ESTIMATED", "2026-03-02")])  # unchanged: nothing logged
    assert not [r for r in caplog.records if r.getMessage() == "trial_registry_changed"]
    _collect(session_factory, store, [_version(99, "ACTUAL", "2026-09-14")])
    [changed] = [r for r in caplog.records if r.getMessage() == "trial_registry_changed"]
    fields: dict[str, Any] = changed.__dict__["catalyst_fields"]
    assert fields == {
        "nct_id": "NCT04677543",
        "changed": {"enrollment": [120, 99], "enrollment_type": ["estimated", "actual"]},
    }
