"""The press-release backfill on recorded SEC responses (ADR 0065).

`sec/875320` is Vertex's recorded filing list and the recorded 8-K of 2026-08-03 with its Exhibit
99.1. `sec-backfill-paged` is the same recorded list split the way EDGAR splits a long one: the
"recent" block keeps the filings from 2026-08-04 on, and the rest, unchanged, are the first older
page (`CIK0000875320-submissions-001.json`), so the 2026-08-03 8-K is reachable only from that page.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.ratelimit import NoWait
from catalyst.collectors.run import PilotCompany
from catalyst.collectors.sec.backfill import (
    BACKFILL_LAST_DAY,
    BACKFILL_SINCE,
    Filing,
    backfill_press_releases,
    backfill_window,
    collect_press_releases,
    press_release_filings,
    press_releases,
)
from catalyst.collectors.sec.edgar import EdgarClient, ExhibitRef, FilingRef
from catalyst.db.tables import TABLES
from catalyst.schemas import CompanyDocument, SourceDocument
from catalyst.snapshots import LocalSnapshotStore
from conftest import FIXTURES, RecordedServer

VERTEX = PilotCompany(ticker="VRTX", cik=875320)
SUBMISSIONS = "https://data.sec.gov/submissions/CIK0000875320.json"
OLDER_PAGE = "https://data.sec.gov/submissions/CIK0000875320-submissions-001.json"
INDEX_256 = "https://www.sec.gov/Archives/edgar/data/875320/000087532026000256/0000875320-26-000256-index.htm"
INDEX_264 = "https://www.sec.gov/Archives/edgar/data/875320/000087532026000264/0000875320-26-000264-index.htm"
EXHIBIT = "https://www.sec.gov/Archives/edgar/data/875320/000087532026000256/ex-991_q22026.htm"
# 2026-08-03 (item 9.01, with an Exhibit 99.1) and 2026-09-01 (item 5.02 only) are both recorded.
SINCE, BEFORE = date(2026, 8, 1), date(2026, 9, 5)
NOW = datetime(2026, 9, 15, 15, tzinfo=UTC)


def _client(server: RecordedServer) -> EdgarClient:
    return EdgarClient("data@example.org", server.transport, NoWait())


def _submissions(label: str) -> bytes:
    return (FIXTURES / label / "submissions_CIK0000875320.json").read_bytes()


def _count(session_factory: sessionmaker[Session], model: type) -> int:  # type: ignore[type-arg]
    with session_factory() as session:
        return session.execute(select(func.count()).select_from(TABLES[model])).scalar_one()


def test_the_period_is_2025_on_and_ends_where_the_weekday_collection_starts() -> None:
    assert backfill_window(date(2026, 11, 24)) == (date(2025, 1, 1), date(2026, 11, 14))
    assert backfill_window(BACKFILL_LAST_DAY) == (BACKFILL_SINCE, BACKFILL_LAST_DAY - timedelta(days=10))
    assert backfill_window(BACKFILL_LAST_DAY + timedelta(days=1)) is None


def test_only_8k_filings_in_the_period_are_listed_newest_first() -> None:
    server = RecordedServer("sec/875320")
    filings = press_release_filings(_client(server), _submissions("sec/875320"), SINCE, BEFORE)
    assert [(f.ref.filed_on.isoformat(), f.ref.form, f.items) for f in filings] == [
        ("2026-09-01", "8-K", "5.02"),
        ("2026-08-03", "8-K", "2.02,9.01"),
    ]
    assert server.requested == []  # the recent block reaches back to 2020: no older page is read


def test_a_list_too_short_for_the_period_reads_only_the_older_pages_that_overlap_it() -> None:
    server = RecordedServer("sec/875320", "sec-backfill-paged")
    filings = press_release_filings(_client(server), _submissions("sec-backfill-paged"), SINCE, BEFORE)
    assert [f.ref.accession_number for f in filings] == ["0000875320-26-000264", "0000875320-26-000256"]
    assert server.requested == [OLDER_PAGE]  # pages 002 and 003 end in 2020 and 2006


def test_an_8k_without_item_9_01_has_no_exhibit_to_fetch() -> None:
    ref = FilingRef(
        cik=1,
        accession_number="0000000001-26-000001",
        form="8-K",
        filed_on=date(2026, 1, 5),
        accepted_at=datetime(2026, 1, 5, 21, tzinfo=UTC),
        primary_document="a.htm",
    )
    assert not Filing(ref, "5.02,5.07").may_have_exhibits
    assert Filing(ref, "2.02, 9.01").may_have_exhibits
    assert Filing(ref, "").may_have_exhibits  # no items listed: look, to be safe


def test_only_the_exhibit_99_1_is_kept() -> None:
    documents = [
        ExhibitRef(type="8-K", description="main", url="https://www.sec.gov/a.htm"),
        ExhibitRef(type="EX-99.1", description="press release", url="https://www.sec.gov/b.htm"),
        ExhibitRef(type="EX-99.2", description="slides", url="https://www.sec.gov/c.pdf"),
        ExhibitRef(type="EX-99", description="other", url="https://www.sec.gov/d.htm"),
    ]
    assert [d.url for d in press_releases(documents)] == ["https://www.sec.gov/b.htm"]


def test_stored_filings_and_filings_without_exhibits_cost_no_request() -> None:
    server = RecordedServer("sec/875320")
    stored = {"sec:0000875320-26-000256:"}
    found = list(
        collect_press_releases(
            _client(server), _submissions("sec/875320"), SINCE, BEFORE, lambda prefix: prefix in stored
        )
    )
    assert found == []
    assert server.requested == []


def test_the_press_release_lands_once_linked_to_its_company(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    def run(server: RecordedServer) -> dict[str, object]:
        report = backfill_press_releases(
            [VERTEX],
            _client(server),
            LocalSnapshotStore(tmp_path),
            session_factory,
            today=BEFORE + timedelta(days=10),
            now=lambda: NOW,
            since=SINCE,
        )
        return report.summary()

    first_server = RecordedServer("sec/875320", "sec-backfill-paged")
    first = run(first_server)
    assert first["active"] is True
    assert first["press_releases_stored"] == 1
    assert first["sources"] == [{"source": "sec-backfill:VRTX", "created": 1, "existing": 0, "error": None}]
    assert INDEX_256 in first_server.requested
    assert EXHIBIT in first_server.requested
    assert INDEX_264 not in first_server.requested  # item 5.02 only
    with session_factory() as session:
        document = session.execute(select(TABLES[SourceDocument])).one()
    assert document.document_type == "8-K EX-99.1"
    assert document.url == EXHIBIT
    assert _count(session_factory, CompanyDocument) == 1

    again_server = RecordedServer("sec/875320", "sec-backfill-paged")
    again = run(again_server)
    assert again["press_releases_stored"] == 0
    assert _count(session_factory, SourceDocument) == 1
    assert again_server.requested == [SUBMISSIONS, OLDER_PAGE]  # nothing fetched again


def test_filings_inside_the_weekday_window_are_left_to_the_weekday_collection(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    server = RecordedServer("sec/875320")
    report = backfill_press_releases(
        [VERTEX],
        _client(server),
        LocalSnapshotStore(tmp_path),
        session_factory,
        today=date(2026, 8, 10),
        now=lambda: NOW,
        since=SINCE,
    )
    assert report.before == date(2026, 7, 31)
    assert INDEX_256 not in server.requested  # 2026-08-03 is the weekday collection's


def test_after_the_last_day_nothing_is_asked_of_the_sec(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    server = RecordedServer("sec/875320")
    report = backfill_press_releases(
        [VERTEX],
        _client(server),
        LocalSnapshotStore(tmp_path),
        session_factory,
        today=BACKFILL_LAST_DAY + timedelta(days=1),
        now=lambda: NOW,
    )
    assert report.summary()["active"] is False
    assert "ended on 2026-12-08" in str(report.reason)
    assert server.requested == []


def test_the_time_limit_stops_before_the_next_company_and_counts_the_rest(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    server = RecordedServer("sec/875320")
    moments = iter([NOW, NOW, NOW + timedelta(minutes=61)])
    report = backfill_press_releases(
        [
            VERTEX,
            VERTEX.model_copy(update={"ticker": "VRTX2"}),
            VERTEX.model_copy(update={"ticker": "VRTX3"}),
        ],
        _client(server),
        LocalSnapshotStore(tmp_path),
        session_factory,
        today=BEFORE + timedelta(days=10),
        max_minutes=60,
        since=SINCE,
        now=lambda: next(moments, NOW + timedelta(hours=2)),
    )
    assert report.companies_done == 1
    assert report.companies_left == 2
    assert "time limit" in str(report.reason)


def test_one_company_failing_does_not_stop_the_next(
    session_factory: sessionmaker[Session], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    server = RecordedServer("sec/875320")
    unknown = PilotCompany(ticker="NONE", cik=1)  # not recorded: 404
    report = backfill_press_releases(
        [unknown, VERTEX],
        _client(server),
        LocalSnapshotStore(tmp_path),
        session_factory,
        today=BEFORE + timedelta(days=10),
        now=lambda: NOW,
        since=SINCE,
    )
    assert report.failed
    assert [s.source for s in report.sources] == ["sec-backfill:NONE", "sec-backfill:VRTX"]
    assert report.summary()["press_releases_stored"] == 1
    assert any(r.getMessage() == "collector_failed" for r in caplog.records)
