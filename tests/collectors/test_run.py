"""The whole collection run on recorded responses: documents land once, linked to their company."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.run import PilotCompany, build_clients, collect_all, load_companies
from catalyst.db.tables import TABLES
from catalyst.schemas import CompanyDocument, SourceDocument
from catalyst.snapshots import LocalSnapshotStore
from conftest import RecordedServer

NOW = datetime(2026, 10, 16, 15, 0, tzinfo=UTC)
VERTEX = PilotCompany(ticker="VRTX", cik=875320)
SINCE = date(2026, 8, 1)


@pytest.fixture
def server() -> RecordedServer:
    return RecordedServer("sec/875320", "fedreg-2026-08", "wires-prnewswire")


def _count(session_factory: sessionmaker[Session], model: type) -> int:  # type: ignore[type-arg]
    with session_factory() as session:
        return session.execute(select(func.count()).select_from(TABLES[model])).scalar_one()


def _run(
    server: RecordedServer, session_factory: sessionmaker[Session], tmp_path: Path
) -> dict[str, tuple[int, int]]:
    clients = build_clients("data@example.org", server.transport, rate_limits=False)
    stats = collect_all(
        [VERTEX],
        clients,
        LocalSnapshotStore(tmp_path),
        session_factory,
        SINCE,
        now=lambda: NOW,
        sec_forms=frozenset({"8-K"}),
    )
    return {s.source: (s.created, s.existing) for s in stats if s.error is None}


def test_one_run_stores_every_source(
    server: RecordedServer, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    counts = _run(server, session_factory, tmp_path)
    assert counts == {"sec:VRTX": (1, 0), "fedreg": (4, 0), "wire:prnewswire": (20, 0)}
    assert _count(session_factory, SourceDocument) == 25
    assert _count(session_factory, CompanyDocument) == 1  # only the SEC exhibit names its company


def test_a_second_run_adds_nothing(
    server: RecordedServer, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _run(server, session_factory, tmp_path)
    counts = _run(server, session_factory, tmp_path)
    assert counts == {"sec:VRTX": (0, 0), "fedreg": (0, 4), "wire:prnewswire": (0, 20)}
    assert _count(session_factory, SourceDocument) == 25
    assert _count(session_factory, CompanyDocument) == 1


def test_one_failing_source_does_not_stop_the_others(
    session_factory: sessionmaker[Session], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    server = RecordedServer("sec/875320", "wires-prnewswire")  # Federal Register not recorded: 404
    clients = build_clients("data@example.org", server.transport, rate_limits=False)
    stats = collect_all(
        [VERTEX],
        clients,
        LocalSnapshotStore(tmp_path),
        session_factory,
        SINCE,
        now=lambda: NOW,
        sec_forms=frozenset({"8-K"}),
    )
    by_source = {s.source: s for s in stats}
    assert by_source["fedreg"].error is not None
    assert by_source["wire:prnewswire"].created == 20
    assert any(r.getMessage() == "collector_failed" for r in caplog.records)


def test_the_company_record_comes_from_the_sec(
    server: RecordedServer, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _run(server, session_factory, tmp_path)
    with session_factory() as session:
        row = session.execute(text("SELECT cik, name, sic_code FROM company")).one()
    assert row.cik == 875320
    assert row.name.startswith("Vertex Pharmaceuticals") or row.name.upper().startswith(
        "VERTEX PHARMACEUTICALS"
    )
    assert row.sic_code == "2834"


def test_the_pilot_list_loads() -> None:
    companies = load_companies(Path(__file__).resolve().parents[2] / "config" / "pilot_companies.toml")
    assert len(companies) == 10
    assert {c.ticker for c in companies} >= {"VRTX", "MRNA", "INSM"}
    assert all(c.ctgov_sponsors for c in companies)


def test_the_preview_report_lists_each_latest_document_with_its_link(
    server: RecordedServer, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    from catalyst.collectors.report import latest_documents_report

    _run(server, session_factory, tmp_path)
    engine = session_factory.kw["bind"]
    report = latest_documents_report(engine)
    exhibit = "https://www.sec.gov/Archives/edgar/data/875320/000087532026000256/ex-991_q22026.htm"
    assert f"| SEC filing | 8-K EX-99.1 | 2026-08-03 20:09 | [open]({exhibit}) |" in report
    assert "| Federal Register | Federal Register notice | 2026-09-25 04:00 |" in report
    assert "| fedreg | 4 |" in report


EXHIBIT_URL = "https://www.sec.gov/Archives/edgar/data/875320/000087532026000256/ex-991_q22026.htm"


def test_a_filing_that_fails_part_way_is_retried_whole_next_run(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    failing = RecordedServer(
        "sec/875320", "fedreg-2026-08", "wires-prnewswire", overrides={EXHIBIT_URL: (503, b"")}
    )
    first = _run(failing, session_factory, tmp_path)
    assert "sec:VRTX" not in first  # the SEC source failed and stored nothing
    second = _run(
        RecordedServer("sec/875320", "fedreg-2026-08", "wires-prnewswire"), session_factory, tmp_path
    )
    assert second["sec:VRTX"] == (1, 0)


def test_a_body_that_will_not_parse_fails_only_its_source(
    session_factory: sessionmaker[Session], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    feed = "https://www.prnewswire.com/rss/health-latest-news/biotechnology-list.rss"
    server = RecordedServer(
        "sec/875320", "fedreg-2026-08", "wires-prnewswire", overrides={feed: (200, b"<html>blocked")}
    )
    clients = build_clients("data@example.org", server.transport, rate_limits=False)
    stats = collect_all(
        [VERTEX],
        clients,
        LocalSnapshotStore(tmp_path),
        session_factory,
        SINCE,
        now=lambda: NOW,
        sec_forms=frozenset({"8-K"}),
    )
    by_source = {s.source: s for s in stats}
    assert by_source["wire:prnewswire"].error is not None
    assert by_source["fedreg"].created == 4
    assert any(r.getMessage() == "collector_failed" for r in caplog.records)


def test_trials_and_fda_records_link_to_their_company_once(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    from functools import partial

    from catalyst.collectors.run import Collector
    from catalyst.collectors.sources import ctgov, openfda
    from catalyst.db.repository import upsert_company
    from catalyst.schemas import DocumentMatch

    server = RecordedServer("ctgov-insmed", "openfda-insmed")
    clients = build_clients("data@example.org", server.transport, rate_limits=False)
    collector = Collector(LocalSnapshotStore(tmp_path), session_factory, lambda: NOW)
    with session_factory.begin() as session:
        insmed = upsert_company(session, 1104506, "INSMED Inc", "2834").id
    trials = partial(ctgov.collect_sponsor_studies, clients.web, frozenset({"Insmed Incorporated"}), 2, 5)
    fda = partial(openfda.collect_applications, clients.web, "Insmed", frozenset({"INSMED INC"}))
    collector.run("ctgov:INSM", trials, (insmed, DocumentMatch.CTGOV_LEAD_SPONSOR))
    collector.run("openfda:INSM", fda, (insmed, DocumentMatch.OPENFDA_SPONSOR))
    trial_stats = collector.run("ctgov:INSM", trials, (insmed, DocumentMatch.CTGOV_LEAD_SPONSOR))
    fda_stats = collector.run("openfda:INSM", fda, (insmed, DocumentMatch.OPENFDA_SPONSOR))
    assert (trial_stats.created, trial_stats.existing) == (0, 9)
    assert (fda_stats.created, fda_stats.existing) == (0, 2)
    assert _count(session_factory, CompanyDocument) == 11
    assert _count(session_factory, SourceDocument) == 11
