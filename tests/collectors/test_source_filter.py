"""`catalyst collect --sources`: run only the named collectors; without it, every one as before."""

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from catalyst import cli
from catalyst.collectors.documents import CollectionStats
from catalyst.collectors.run import SOURCES, PilotCompany, build_clients, collect_all
from catalyst.snapshots import LocalSnapshotStore
from conftest import RecordedServer

NOW = datetime(2026, 10, 16, 15, 0, tzinfo=UTC)
SINCE = date(2026, 8, 1)
# Named at every source, so a run that skipped none would ask the trial and FDA registries too.
VERTEX = PilotCompany(
    ticker="VRTX",
    cik=875320,
    ctgov_sponsors=frozenset({"Vertex Pharmaceuticals Incorporated"}),
    openfda_manufacturer="Vertex",
)


def _run(
    session_factory: sessionmaker[Session], tmp_path: Path, sources: frozenset[str]
) -> tuple[RecordedServer, dict[str, tuple[int, int]]]:
    server = RecordedServer("sec/875320", "fedreg-2026-08", "wires-prnewswire")
    clients = build_clients("data@example.org", server.transport, rate_limits=False)
    stats = collect_all(
        [VERTEX],
        clients,
        LocalSnapshotStore(tmp_path),
        session_factory,
        SINCE,
        now=lambda: NOW,
        sec_forms=frozenset({"8-K"}),
        sources=sources,
    )
    return server, {s.source: (s.created, s.existing) for s in stats}


def _hosts(server: RecordedServer) -> set[str]:
    return {url.split("/")[2] for url in server.requested}


def test_the_sources_are_the_collectors_run_names() -> None:
    assert SOURCES == ("sec", "ctgov", "openfda", "fedreg", "wire")


def test_sec_only_asks_the_sec_and_nothing_else(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    server, counts = _run(session_factory, tmp_path, frozenset({"sec"}))
    assert counts == {"sec:VRTX": (1, 0)}
    assert _hosts(server) == {"data.sec.gov", "www.sec.gov"}


def test_source_wide_feeds_alone_never_ask_about_a_company(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    server, counts = _run(session_factory, tmp_path, frozenset({"fedreg", "wire"}))
    assert counts == {"fedreg": (4, 0), "wire:prnewswire": (20, 0)}
    assert _hosts(server) == {"www.federalregister.gov", "www.prnewswire.com"}


def test_a_trial_source_without_sec_still_finds_the_company_but_stores_no_filings(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    server, counts = _run(session_factory, tmp_path, frozenset({"ctgov"}))
    assert set(counts) == {"ctgov:VRTX"}  # not recorded here, so it fails on its own
    assert "clinicaltrials.gov" in _hosts(server)
    assert "api.fda.gov" not in _hosts(server)
    assert not [url for url in server.requested if "/Archives/" in url]  # the company record only


def test_every_source_is_the_default(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    _, everything = _run(session_factory, tmp_path, frozenset(SOURCES))
    assert set(everything) == {"sec:VRTX", "ctgov:VRTX", "openfda:VRTX", "fedreg", "wire:prnewswire"}


def test_a_company_whose_sec_record_fails_is_reported_and_the_run_goes_on(
    session_factory: sessionmaker[Session], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    server = RecordedServer("sec/875320", "fedreg-2026-08", "wires-prnewswire")  # CIK 1 is not recorded
    clients = build_clients("data@example.org", server.transport, rate_limits=False)
    missing = PilotCompany(ticker="GONE", cik=1)
    stats = collect_all(
        [missing, PilotCompany(ticker="VRTX", cik=875320)],
        clients,
        LocalSnapshotStore(tmp_path),
        session_factory,
        SINCE,
        now=lambda: NOW,
        sec_forms=frozenset({"8-K"}),
    )
    by_source = {s.source: s for s in stats}
    assert by_source["sec:GONE"].error is not None
    assert (by_source["sec:VRTX"].created, by_source["sec:VRTX"].error) == (1, None)
    assert by_source["fedreg"].created == 4
    assert any(r.getMessage() == "collector_failed" for r in caplog.records)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def collect(*args: Any, **kwargs: Any) -> list[CollectionStats]:
        seen.append({"args": args, **kwargs})
        return [CollectionStats("sec:VRTX")]

    class _Engine:
        def dispose(self) -> None:
            pass

    monkeypatch.setenv("SEC_CONTACT_EMAIL", "data@example.org")
    monkeypatch.setattr(cli, "collect_all", collect)
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _Engine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    return seen


def _collect(tmp_path: Path, *extra: str) -> int:
    return cli.main(["collect", "--since", "2026-08-01", "--snapshot-dir", str(tmp_path), *extra])


def test_without_the_flag_the_command_runs_as_before(calls: list[dict[str, Any]], tmp_path: Path) -> None:
    assert _collect(tmp_path) == 0
    [call] = calls
    assert "sources" not in call  # collect_all's own default: every source


def test_the_flag_passes_the_named_sources(calls: list[dict[str, Any]], tmp_path: Path) -> None:
    assert _collect(tmp_path, "--sources", "sec, fedreg") == 0
    assert calls[0]["sources"] == frozenset({"sec", "fedreg"})


@pytest.mark.parametrize("value", ["sec,prices", "", "sec,,"])
def test_an_unknown_or_empty_source_is_refused_with_the_valid_names(
    calls: list[dict[str, Any]], tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    with pytest.raises(SystemExit) as stopped:
        _collect(tmp_path, "--sources", value)
    assert stopped.value.code == 2
    assert "valid sources: sec, ctgov, openfda, fedreg, wire" in capsys.readouterr().err
    assert calls == []
