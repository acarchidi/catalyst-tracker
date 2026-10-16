"""EDGAR parsing, checked against recorded copies of Vertex Pharmaceuticals' real filings.

Expected values below were read by hand from the recorded index pages, not from the code.
"""

import itertools
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from catalyst.collectors.sec.edgar import SUBMISSIONS_URL, find_press_release, list_documents, recent_filings

BASE = "https://www.sec.gov/Archives/edgar/data/875320"
EARNINGS_8K = f"{BASE}/000087532026000256/0000875320-26-000256-index.htm"
OFFICER_8K = f"{BASE}/000087532026000264/0000875320-26-000264-index.htm"


def _submissions(responses: dict[str, tuple[bytes, str]]) -> bytes:
    return responses[SUBMISSIONS_URL.format(cik=875320)][0]


def test_recent_8ks_are_newest_first_and_only_8ks(vertex_sec: dict[str, tuple[bytes, str]]) -> None:
    filings = recent_filings(_submissions(vertex_sec), "8-K")
    assert len(filings) == 53
    assert {f.form for f in filings} == {"8-K"}
    assert [f.accession_number for f in filings[:2]] == ["0000875320-26-000264", "0000875320-26-000256"]
    assert all(a.accepted_at >= b.accepted_at for a, b in itertools.pairwise(filings))


def test_index_url_matches_edgar(vertex_sec: dict[str, tuple[bytes, str]]) -> None:
    filings = recent_filings(_submissions(vertex_sec), "8-K")
    assert filings[0].index_url == OFFICER_8K
    assert filings[1].index_url == EARNINGS_8K


def test_acceptance_time_is_true_utc(vertex_sec: dict[str, tuple[bytes, str]]) -> None:
    # The index page shows "Accepted 2026-08-03 16:09:43" in New York time.
    filing = recent_filings(_submissions(vertex_sec), "8-K")[1]
    new_york = datetime(2026, 8, 3, 16, 9, 43, tzinfo=ZoneInfo("America/New_York"))
    assert filing.accepted_at == new_york
    assert filing.accepted_at == datetime(2026, 8, 3, 20, 9, 43, tzinfo=UTC)


def test_finds_the_press_release_exhibit(vertex_sec: dict[str, tuple[bytes, str]]) -> None:
    exhibit = find_press_release(vertex_sec[EARNINGS_8K][0].decode())
    assert exhibit is not None
    assert exhibit.type == "EX-99.1"
    assert exhibit.url == f"{BASE}/000087532026000256/ex-991_q22026.htm"


def test_filing_without_a_press_release_returns_none(vertex_sec: dict[str, tuple[bytes, str]]) -> None:
    assert find_press_release(vertex_sec[OFFICER_8K][0].decode()) is None


def test_inline_xbrl_viewer_links_become_direct_document_links(
    vertex_sec: dict[str, tuple[bytes, str]],
) -> None:
    documents = list_documents(vertex_sec[EARNINGS_8K][0].decode())
    assert documents[0].type == "8-K"
    assert documents[0].url == f"{BASE}/000087532026000256/vrtx-20260803.htm"
    assert all(d.url.startswith("https://www.sec.gov/Archives/") for d in documents)
