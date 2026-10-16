"""Walking skeleton end to end on recorded SEC responses: one filing becomes one row, once."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst import cli
from catalyst.collectors.ratelimit import MinIntervalLimiter
from catalyst.collectors.sec.edgar import EdgarClient
from catalyst.collectors.sec.press_release import NoPressReleaseError, collect_latest_press_release
from catalyst.db.models import SourceDocumentRow
from catalyst.snapshots import LocalSnapshotStore

EXHIBIT = "https://www.sec.gov/Archives/edgar/data/875320/000087532026000256/ex-991_q22026.htm"
FIXED_NOW = datetime(2026, 10, 16, 15, 0, tzinfo=UTC)


class _NoWait(MinIntervalLimiter):
    def __init__(self) -> None:
        super().__init__(max_per_second=8)

    def wait(self) -> None:
        pass


class _Recorded:
    """Serve recorded responses; anything not recorded is a 404."""

    def __init__(self, responses: dict[str, tuple[bytes, str]]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body, content_type = self.responses.get(str(request.url), (b"", ""))
        if not content_type:
            return httpx.Response(404)
        return httpx.Response(200, content=body, headers={"content-type": content_type})


def _client(server: _Recorded) -> EdgarClient:
    return EdgarClient("data@example.org", httpx.MockTransport(server), _NoWait())


def _rows(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return session.execute(select(func.count()).select_from(SourceDocumentRow)).scalar_one()


def test_latest_press_release_becomes_one_row_with_a_deep_link(
    vertex_sec: dict[str, tuple[bytes, str]], session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    server = _Recorded(vertex_sec)
    store = LocalSnapshotStore(tmp_path)
    doc, created = collect_latest_press_release(
        _client(server), store, session_factory, 875320, now=lambda: FIXED_NOW
    )
    exhibit_bytes = vertex_sec[EXHIBIT][0]
    assert created
    assert doc.url == EXHIBIT
    assert doc.publisher == "SEC EDGAR"
    assert doc.document_type == "8-K EX-99.1"
    assert doc.published_at == datetime(2026, 8, 3, 20, 9, 43, tzinfo=UTC)
    assert doc.retrieved_at == FIXED_NOW
    assert doc.sha256 == hashlib.sha256(exhibit_bytes).hexdigest()
    assert doc.content_type == "text/html"
    assert store.get(doc.snapshot_key) == exhibit_bytes
    assert _rows(session_factory) == 1
    assert len(server.requests) == 4
    assert {r.headers["User-Agent"] for r in server.requests} == {"catalyst-tracker data@example.org"}


def test_running_twice_creates_no_duplicate(
    vertex_sec: dict[str, tuple[bytes, str]], session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    first, _ = collect_latest_press_release(_client(_Recorded(vertex_sec)), store, session_factory, 875320)
    second, created = collect_latest_press_release(
        _client(_Recorded(vertex_sec)), store, session_factory, 875320
    )
    assert not created
    assert second.id == first.id
    assert _rows(session_factory) == 1


def test_no_press_release_in_recent_filings_raises(
    vertex_sec: dict[str, tuple[bytes, str]], session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    officer_index = next(v for k, v in vertex_sec.items() if k.endswith("0000875320-26-000264-index.htm"))
    server = _Recorded(vertex_sec)

    def every_index_lacks_an_exhibit(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("-index.htm"):
            server.requests.append(request)
            return httpx.Response(200, content=officer_index[0], headers={"content-type": officer_index[1]})
        return server(request)

    client = EdgarClient("data@example.org", httpx.MockTransport(every_index_lacks_an_exhibit), _NoWait())
    with pytest.raises(NoPressReleaseError, match="875320"):
        collect_latest_press_release(client, LocalSnapshotStore(tmp_path), session_factory, 875320)
    assert len(server.requests) == 1 + 10
    assert _rows(session_factory) == 0


def test_cli_prints_the_row_as_json(
    vertex_sec: dict[str, tuple[bytes, str]],
    session_factory: sessionmaker[Session],
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "data@example.org")
    monkeypatch.setenv("CATALYST_DATABASE_URL", database_url)

    def recorded_client(contact_email: str) -> EdgarClient:
        return _client(_Recorded(vertex_sec))

    monkeypatch.setattr(cli, "EdgarClient", recorded_client)
    assert cli.main(["sec-press-release", "--cik", "875320", "--snapshot-dir", str(tmp_path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["url"] == EXHIBIT
    assert printed["created"] is True
