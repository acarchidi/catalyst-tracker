"""A filing EDGAR answers 404 for is skipped, counted and named; the company carries on (ADR 0065).

Shaped like live run 45406121773: the index page of one 8-K filed through a filing agent (its
accession number starts with the agent's number, here Vertex's 2026-07-07 8-K, 0001193125-26-296710)
answers 404, while the company's own 8-K of 2026-08-03 is recorded in full.
"""

import logging
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.ratelimit import NoWait
from catalyst.collectors.run import PilotCompany
from catalyst.collectors.sec.backfill import backfill_press_releases, collect_press_releases
from catalyst.collectors.sec.edgar import EdgarClient
from catalyst.snapshots import LocalSnapshotStore
from conftest import FIXTURES, RecordedServer

VERTEX = PilotCompany(ticker="VRTX", cik=875320)
AGENT_INDEX = (
    "https://www.sec.gov/Archives/edgar/data/875320/000119312526296710/0001193125-26-296710-index.htm"
)
EXHIBIT = "https://www.sec.gov/Archives/edgar/data/875320/000087532026000256/ex-991_q22026.htm"
SINCE, BEFORE = date(2026, 7, 1), date(2026, 9, 5)
TODAY = date(2026, 9, 15)
NOW = datetime(2026, 9, 15, 15, tzinfo=UTC)


def _server(status: int = 404) -> RecordedServer:
    return RecordedServer("sec/875320", overrides={AGENT_INDEX: (status, b"Not Found")})


def _client(server: RecordedServer) -> EdgarClient:
    return EdgarClient("data@example.org", server.transport, NoWait())


def test_a_missing_filing_is_reported_and_the_next_filing_is_still_read() -> None:
    server = _server()
    missing: list[str] = []
    submissions = (FIXTURES / "sec/875320/submissions_CIK0000875320.json").read_bytes()
    found = list(
        collect_press_releases(_client(server), submissions, SINCE, BEFORE, lambda _p: False, missing.append)
    )
    assert [d.url for d in found] == [EXHIBIT]
    assert missing == [AGENT_INDEX]


def test_a_missing_filing_is_not_a_company_failure(
    session_factory: sessionmaker[Session], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    report = backfill_press_releases(
        [VERTEX],
        _client(_server()),
        LocalSnapshotStore(tmp_path),
        session_factory,
        today=TODAY,
        now=lambda: NOW,
        since=SINCE,
    )
    summary = report.summary()
    assert not report.failed
    assert summary["sources"] == [{"source": "sec-backfill:VRTX", "created": 1, "existing": 0, "error": None}]
    assert summary["filings_not_found"] == 1
    assert summary["not_found"] == [AGENT_INDEX]
    assert any(r.getMessage() == "filing_not_found" for r in caplog.records)


def test_another_error_still_stops_the_company_and_counts_as_a_failure(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    report = backfill_press_releases(
        [VERTEX],
        _client(_server(500)),
        LocalSnapshotStore(tmp_path),
        session_factory,
        today=TODAY,
        now=lambda: NOW,
        since=SINCE,
    )
    assert report.failed  # a server error may pass; the next run tries the company again
    assert report.summary()["filings_not_found"] == 0
