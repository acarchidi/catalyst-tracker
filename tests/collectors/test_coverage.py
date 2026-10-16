"""The coverage list: US-listed companies under the covered industry codes, kept only with trial evidence.

Runs on a recorded EDGAR, SEC ticker-file, and ClinicalTrials.gov session for industry code 8731
(2026-10-16): 182 registrants, 13 listed on a US exchange.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from catalyst.collectors.coverage import (
    CoverageEntry,
    Override,
    browse_page_ciks,
    build_coverage,
    coverage_toml,
    listed_companies,
    load_overrides,
    unconfirmed_markdown,
)
from catalyst.collectors.run import Clients, build_clients, load_companies
from conftest import FIXTURES, RecordedServer

INCYTE, MAXCYTE, IQVIA, ABSCI = 879169, 1287098, 1478242, 1672688
RECORDED = FIXTURES / "coverage-8731"


def recorded_file(url_part: str) -> bytes:
    index = json.loads((RECORDED / "responses.json").read_text())
    name = next(entry["file"] for url, entry in index.items() if url_part in url)
    return (RECORDED / name).read_bytes()


@pytest.fixture
def server() -> RecordedServer:
    return RecordedServer("coverage-8731")


@pytest.fixture
def clients(server: RecordedServer) -> Clients:
    return build_clients("data@example.org", server.transport, rate_limits=False)


def build(clients: Clients, overrides: dict[int, Override] | None = None) -> list[CoverageEntry]:
    return build_coverage(clients.sec, clients.web, overrides or {}, sic_codes=(8731,))


def test_browse_pages_give_every_registrant_and_stop_at_the_last_page() -> None:
    first, more = browse_page_ciks(recorded_file("start=0&"))
    second, last_more = browse_page_ciks(recorded_file("start=100&"))
    assert (len(first), more) == (100, True)
    assert (len(second), last_more) == (82, False)
    assert INCYTE in first + second


def test_only_companies_on_a_us_exchange_are_listed() -> None:
    listed = listed_companies(recorded_file("company_tickers_exchange"))
    payload = json.loads(recorded_file("company_tickers_exchange"))
    otc = {row[0] for row in payload["data"] if row[3] == "OTC"} - {
        row[0] for row in payload["data"] if row[3] not in ("OTC", None)
    }
    assert otc
    assert not otc & listed.keys()
    assert listed[INCYTE].ticker == "INCY"
    assert listed[INCYTE].exchange == "Nasdaq"


def test_the_main_ticker_skips_rights_and_class_suffixes() -> None:
    payload = json.dumps(
        {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [
                [14272, "BRISTOL MYERS SQUIBB CO", "CELG-RI", "NYSE"],
                [14272, "BRISTOL MYERS SQUIBB CO", "BMY", "NYSE"],
            ],
        }
    ).encode()
    assert listed_companies(payload)[14272].ticker == "BMY"


def test_developers_are_included_and_service_firms_go_to_review(clients: Clients) -> None:
    entries = {e.listing.cik: e for e in build(clients)}
    assert len(entries) == 13
    assert {cik for cik, e in entries.items() if e.included} == {INCYTE, MAXCYTE}
    assert entries[INCYTE].ctgov_sponsors == ["Incyte Corporation"]
    assert entries[INCYTE].sponsors["Incyte Corporation"] == 271
    assert not entries[IQVIA].included


def test_overrides_add_registry_names_or_exclude_by_hand(clients: Clients, server: RecordedServer) -> None:
    overrides = {
        ABSCI: Override(
            cik=ABSCI, note="test: registry name differs", ctgov_sponsors=frozenset({"Absci LLC"})
        ),
        INCYTE: Override(cik=INCYTE, note="test: excluded by hand", include=False),
    }
    entries = {e.listing.cik: e for e in build(clients, overrides)}
    assert entries[ABSCI].included
    assert entries[ABSCI].ctgov_sponsors == ["Absci LLC"]
    assert not entries[INCYTE].included
    assert not any("query.spons=incyte" in url for url in server.requested)


def test_the_written_list_is_one_the_collectors_read(clients: Clients, tmp_path: Path) -> None:
    entries = build(clients)
    path = tmp_path / "coverage.toml"
    path.write_text(coverage_toml(entries, date(2026, 10, 16)), encoding="utf-8")
    companies = {c.cik: c for c in load_companies(path)}
    assert set(companies) == {INCYTE, MAXCYTE}
    incyte = companies[INCYTE]
    assert (incyte.ticker, incyte.sic, incyte.exchange, incyte.ctgov_trials) == ("INCY", 8731, "Nasdaq", 271)
    assert incyte.ctgov_sponsors == frozenset({"Incyte Corporation"})
    assert "2 of 13 listed companies included" in path.read_text()


def test_the_review_list_names_every_company_left_out_with_a_link(clients: Clients) -> None:
    overrides = {INCYTE: Override(cik=INCYTE, note="test reason", include=False)}
    text = unconfirmed_markdown(build(clients, overrides), date(2026, 10, 16))
    rows = [line for line in text.splitlines() if line.startswith("| ") and "EDGAR" in line]
    assert len(rows) == 12
    assert "Excluded by hand: test reason" in text
    assert f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={IQVIA}" in text


def test_overrides_file_needs_a_note_and_a_missing_file_means_none(tmp_path: Path) -> None:
    assert load_overrides(tmp_path / "absent.toml") == {}
    bad = tmp_path / "bad.toml"
    bad.write_text("[[company]]\ncik = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="note"):
        load_overrides(bad)


def test_the_checked_in_overrides_load() -> None:
    overrides = load_overrides(Path(__file__).resolve().parents[2] / "config" / "coverage_overrides.toml")
    assert len(overrides) == 10
    assert overrides[1682852].ctgov_sponsors == frozenset({"ModernaTX, Inc."})
