"""`--sources ctgov-linked`: collect only the trials our claims and catalysts name (ADR 0040).

The recorded responses in tests/fixtures/ctgov-linked are real ClinicalTrials.gov answers to
`filter.ids` requests, two trials at a time. NCT00000099 is not in the registry, so the first
answer holds one trial. Expected values were read by hand from the fixture files.
"""

import json
import logging
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import JsonValue
from sqlalchemy import insert, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst import cli
from catalyst.collectors.linked_trials import linked_nct_ids
from catalyst.collectors.run import ON_REQUEST, SOURCES, PilotCompany, build_clients, collect_all
from catalyst.collectors.sources import ctgov
from catalyst.db.repository import upsert_company
from catalyst.db.tables import TABLES
from catalyst.schemas import (
    Catalyst,
    CatalystStatus,
    CatalystTrial,
    CatalystType,
    Claim,
    ClaimSubject,
    CompanyDocument,
    DateConfidence,
    DatePrecision,
    DocumentMatch,
    Entity,
    EntityMatch,
    MatchedEntity,
    MatchStatus,
    SourceDocument,
    SourceTier,
    Trial,
)
from catalyst.snapshots import LocalSnapshotStore
from conftest import RecordedServer

NOW = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
INSMED = PilotCompany(ticker="INSM", cik=1104506, ctgov_sponsors=frozenset({"Insmed Incorporated"}))
LINKED = ["NCT00000099", "NCT01487304", "NCT04677543", "NCT04677569"]


def _add(session: Session, record: Entity) -> None:
    session.execute(insert(TABLES[type(record)]).values(**record.model_dump()))


def _document(session: Session, key: str) -> UUID:
    document = SourceDocument(
        source_key=key,
        url="https://example.org/release",
        publisher="Example",
        source_tier=SourceTier.WIRE,
        document_type="8-K",
        published_at=NOW,
        retrieved_at=NOW,
        sha256="a" * 64,
        snapshot_key=f"sha256/aa/{'a' * 64}",
        content_type="text/html",
    )
    _add(session, document)
    return document.id


def _claim(
    session: Session,
    document: UUID,
    quote: str,
    value: dict[str, JsonValue] | None = None,
    trial: UUID | None = None,
) -> UUID:
    """Add a catalyst claim, or with `trial` a claim about that trial."""
    claim = Claim(
        source_document_id=document,
        subject_type=ClaimSubject.TRIAL if trial else ClaimSubject.CATALYST,
        subject_id=trial,
        field="catalyst:window",
        value=value if value is not None else {"quote": quote},
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
    _add(session, claim)
    return claim.id


def _match(session: Session, claim: UUID, name: str, trial: UUID | None) -> None:
    _add(
        session,
        EntityMatch(
            claim_id=claim,
            entity_type=MatchedEntity.TRIAL,
            name=name,
            status=MatchStatus.RESOLVED if trial else MatchStatus.UNKNOWN,
            matched_id=trial,
            candidates=[str(trial)] if trial else [],
            matched_by="nct_id" if trial else None,
            resolved_at=NOW,
        ),
    )


def _seed_links(session_factory: sessionmaker[Session]) -> None:
    """Name each trial in `LINKED` one different way, plus trials nothing names."""
    with session_factory.begin() as session:
        company = upsert_company(session, INSMED.cik, "INSMED Inc", None).id
        document = _document(session, "sec:1:release")
        trials = {
            nct: Trial(nct_id=nct)
            for nct in ("NCT04677569", "NCT04677543", "NCT01487304", "NCT05555555", "NCT06666666")
        }
        for trial in trials.values():
            _add(session, trial)
        catalyst = Catalyst(
            catalyst_type=CatalystType.TOPLINE_READOUT,
            company_id=company,
            title="Topline data",
            window_start=date(2026, 7, 1),
            window_end=date(2026, 9, 30),
            window_precision=DatePrecision.QUARTER,
            window_raw_text="3Q26",
            date_confidence=DateConfidence.COMPANY_CONFIRMED,
            status=CatalystStatus.UPCOMING,
        )
        _add(session, catalyst)
        plain = _claim(session, document, "topline data from ENCORE in the third quarter")
        # A catalyst's trial link: ENCORE.
        _add(
            session,
            CatalystTrial(catalyst_id=catalyst.id, trial_id=trials["NCT04677569"].id, claim_id=plain),
        )
        # A name that resolved to a trial: ARISE, by its study name.
        _match(session, plain, "ARISE", trials["NCT04677543"].id)
        # A registry number in a quote, written in small letters, that nothing resolved yet.
        unresolved = _claim(session, document, "results of nct00000099 are expected")
        _match(session, unresolved, "nct00000099", None)
        # A registry number only in a claim's value.
        _claim(session, document, "the pivotal study", {"trial": "NCT01487304"})
        # NCT05555555 and NCT06666666 are only in the trial table, as a sponsor-wide run leaves them.


def test_linked_trials_are_those_claims_and_catalysts_name(session_factory: sessionmaker[Session]) -> None:
    _seed_links(session_factory)
    with session_factory() as session:
        assert linked_nct_ids(session) == LINKED


def test_a_claim_about_a_trial_links_that_trial(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        trial = Trial(nct_id="NCT05555555")
        _add(session, trial)
        _add(session, Trial(nct_id="NCT06666666"))
        _claim(session, _document(session, "sec:1:release"), "enrollment completed", trial=trial.id)
    with session_factory() as session:
        assert linked_nct_ids(session) == ["NCT05555555"]


def test_nothing_named_means_no_trials(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        assert linked_nct_ids(session) == []


def test_trials_are_fetched_by_number_in_batches_and_missing_ones_are_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    server = RecordedServer("ctgov-linked")
    clients = build_clients("data@example.org", server.transport, rate_limits=False)
    documents = list(ctgov.collect_studies_by_id(clients.web, LINKED, batch_size=2))
    assert [d.source_key for d in documents] == [
        "ctgov:NCT01487304:2016-10-14",
        "ctgov:NCT04677569:2026-02-02",
        "ctgov:NCT04677543:2024-06-26",
    ]
    assert len(server.requested) == 2  # one request per batch, no empty follow-up page
    missing = [r for r in caplog.records if r.getMessage() == "trials_not_in_registry"]
    assert [r.catalyst_fields["nct_ids"] for r in missing] == [["NCT00000099"]]  # type: ignore[attr-defined]


def test_a_record_is_stored_the_same_way_whichever_mode_fetched_it() -> None:
    sponsor = RecordedServer("ctgov-insmed")
    by_sponsor = build_clients("data@example.org", sponsor.transport, rate_limits=False)
    wide = {
        d.source_key: d
        for d in ctgov.collect_sponsor_studies(by_sponsor.web, frozenset({"Insmed Incorporated"}), 2, 5)
    }
    linked = build_clients("data@example.org", RecordedServer("ctgov-linked").transport, rate_limits=False)
    arise = next(
        d
        for d in ctgov.collect_studies_by_id(linked.web, ["NCT04677543", "NCT04677569"], batch_size=2)
        if d.source_key == "ctgov:NCT04677543:2024-06-26"
    )
    # Same key, link, date, and tier, so the record is stored once whichever mode found it first.
    # The bytes differ only because the registry refreshed derived data (disease terms, site map
    # points) between the two recordings without a new record version; the trial is the same.
    same = wide[arise.source_key]
    assert replace(same, content=b"") == replace(arise, content=b"")
    for module in ("identificationModule", "designModule", "statusModule"):
        old, new = (json.loads(d.content)["protocolSection"][module] for d in (same, arise))
        assert old == new


def test_batches_never_ask_for_more_than_the_registry_allows() -> None:
    with pytest.raises(ValueError, match="1000"):
        list(ctgov.collect_studies_by_id(build_clients("data@example.org").web, LINKED, batch_size=1001))


def _collect(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> tuple[RecordedServer, list[tuple[str, int, int, str | None]]]:
    server = RecordedServer("ctgov-linked")
    clients = build_clients("data@example.org", server.transport, rate_limits=False)
    stats = collect_all(
        [INSMED],
        clients,
        LocalSnapshotStore(tmp_path),
        session_factory,
        date(2026, 8, 1),
        now=lambda: NOW,
        sources=frozenset({"ctgov-linked"}),
        linked_batch_size=2,
    )
    return server, [(s.source, s.created, s.existing, s.error) for s in stats]


def test_the_linked_run_stores_records_trial_sizes_and_sponsor_links(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _seed_links(session_factory)
    server, stats = _collect(session_factory, tmp_path)
    assert stats == [("ctgov-linked", 3, 0, None)]
    assert {url.split("/")[2] for url in server.requested} == {"clinicaltrials.gov"}  # no SEC request
    trial, document, link = TABLES[Trial], TABLES[SourceDocument], TABLES[CompanyDocument]
    with session_factory() as session:
        sizes = dict(
            session.execute(
                select(trial.c.nct_id, trial.c.enrollment).where(trial.c.enrollment.is_not(None))
            ).all()
        )
        assert sizes == {"NCT04677569": 425, "NCT04677543": 99, "NCT01487304": 3760}
        study_names = dict(session.execute(select(trial.c.nct_id, trial.c.study_name)).all())
        assert study_names["NCT04677569"] == "ENCORE"
        linked = session.execute(
            select(document.c.source_key, link.c.matched_by)
            .join(link, link.c.source_document_id == document.c.id)
            .where(document.c.source_key.startswith("ctgov:"))
            .order_by(document.c.source_key)
        ).all()
    # Insmed leads its two trials; Novo Nordisk is not on the company list, so its trial is stored
    # with no company link.
    assert [tuple(row) for row in linked] == [
        ("ctgov:NCT04677543:2024-06-26", DocumentMatch.CTGOV_LEAD_SPONSOR.value),
        ("ctgov:NCT04677569:2026-02-02", DocumentMatch.CTGOV_LEAD_SPONSOR.value),
    ]


def test_the_linked_run_is_safe_to_repeat(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    _seed_links(session_factory)
    _collect(session_factory, tmp_path)
    _, again = _collect(session_factory, tmp_path)
    assert again == [("ctgov-linked", 0, 3, None)]
    with session_factory() as session:
        stored = select(TABLES[SourceDocument].c.id).where(
            TABLES[SourceDocument].c.source_key.startswith("ctgov:")
        )
        assert len(session.execute(stored).all()) == 3
        assert len(session.execute(select(TABLES[CompanyDocument].c.id)).all()) == 2


def test_with_nothing_linked_the_registry_is_not_asked(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    server, stats = _collect(session_factory, tmp_path)
    assert stats == [("ctgov-linked", 0, 0, None)]
    assert server.requested == []


def test_the_linked_mode_runs_only_when_named() -> None:
    assert "ctgov-linked" in ON_REQUEST
    assert "ctgov-linked" not in SOURCES  # so a run with every source never adds it
    args = cli._parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        ["collect", "--since", "2026-07-01", "--sources", "ctgov-linked"]
    )
    assert args.sources == frozenset({"ctgov-linked"})
    with pytest.raises(SystemExit):
        cli._parser().parse_args(  # pyright: ignore[reportPrivateUsage]
            ["collect", "--since", "2026-07-01", "--sources", "ctgov-everything"]
        )
