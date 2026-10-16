"""The link checker: every stored source link is fetched politely and given one plain verdict.

A link passes only when the source answers 200 at that exact address. Moved, missing, refused,
and failed links are each named, so the Gate 1 report can say which records need attention.
"""

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.repository import save_source_document
from catalyst.links import LinkStatus, check_links, link_client, links_markdown
from catalyst.schemas import SourceDocument, SourceTier

SEC_URL = "https://www.sec.gov/Archives/edgar/data/875320/000087532026000256/ex-991_q22026.htm"
TRIAL_URL = "https://clinicaltrials.gov/study/NCT01234567"
ROBOTS = b"User-agent: *\nDisallow: /api/\nDisallow: /private/\n"


def serve(answers: dict[str, httpx.Response | Exception]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, content=ROBOTS)
        answer = answers.get(url, httpx.Response(404))
        if isinstance(answer, Exception):
            raise answer
        return answer

    return httpx.MockTransport(handler)


def status_of(url: str, answer: httpx.Response | Exception) -> LinkStatus:
    client = link_client("data@example.org", serve({url: answer}), rate_limits=False)
    return client.check(url).status


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (httpx.Response(200, content=b"<html>Exhibit 99.1</html>"), LinkStatus.OK),
        (httpx.Response(404), LinkStatus.BROKEN),
        (httpx.Response(410), LinkStatus.BROKEN),
        (httpx.Response(301, headers={"location": "https://www.sec.gov/new"}), LinkStatus.MOVED),
        (httpx.Response(302, headers={"location": "https://www.sec.gov/login"}), LinkStatus.MOVED),
        (httpx.Response(403), LinkStatus.REFUSED),
        (httpx.Response(429), LinkStatus.REFUSED),
        (httpx.Response(503), LinkStatus.FAILED),
        (httpx.ConnectError("refused"), LinkStatus.FAILED),
    ],
)
def test_each_answer_gets_one_verdict(answer: httpx.Response | Exception, expected: LinkStatus) -> None:
    assert status_of(SEC_URL, answer) == expected


def test_a_moved_link_names_where_it_went() -> None:
    client = link_client(
        "data@example.org",
        serve({SEC_URL: httpx.Response(301, headers={"location": "https://www.sec.gov/new"})}),
        rate_limits=False,
    )
    assert client.check(SEC_URL).detail == "moved to https://www.sec.gov/new"


def test_robots_txt_is_obeyed_and_reported() -> None:
    url = "https://clinicaltrials.gov/private/NCT01234567"
    assert status_of(url, httpx.Response(200)) == LinkStatus.NOT_CHECKED


def test_a_host_outside_the_sources_is_never_fetched() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    client = link_client("data@example.org", httpx.MockTransport(handler), rate_limits=False)
    result = client.check("https://example.com/report.pdf")
    assert result.status == LinkStatus.NOT_CHECKED
    assert seen == []


def _doc(url: str, key: str) -> SourceDocument:
    sha = "c" * 64
    return SourceDocument(
        source_key=key,
        url=url,
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type="8-K EX-99.1",
        published_at=datetime(2026, 8, 3, 20, 9, tzinfo=UTC),
        retrieved_at=datetime(2026, 10, 16, 12, 0, tzinfo=UTC),
        sha256=sha,
        snapshot_key=f"sha256/{sha[:2]}/{sha}",
        content_type="text/html",
    )


@pytest.fixture
def two_documents(session_factory: sessionmaker[Session]) -> Callable[[], None]:
    def add() -> None:
        with session_factory.begin() as session:
            save_source_document(session, _doc(SEC_URL, "sec:0000875320-26-000256:ex-991_q22026.htm"))
            save_source_document(session, _doc(TRIAL_URL, "ctgov:NCT01234567:2026-09-01"))

    return add


def test_every_stored_link_is_checked_and_a_dead_one_fails_the_report(
    session_factory: sessionmaker[Session], two_documents: Callable[[], None]
) -> None:
    two_documents()
    client = link_client("data@example.org", serve({SEC_URL: httpx.Response(200)}), rate_limits=False)
    report = check_links(session_factory, client)
    assert report.counts() == {LinkStatus.OK: 1, LinkStatus.BROKEN: 1}
    assert not report.ok
    text = links_markdown(report)
    assert "1 of 2 links work" in text
    assert TRIAL_URL in text
    assert SEC_URL not in text  # working links are counted, not listed


def test_a_sample_checks_the_same_links_every_time(
    session_factory: sessionmaker[Session], two_documents: Callable[[], None]
) -> None:
    two_documents()
    client = link_client(
        "data@example.org",
        serve({SEC_URL: httpx.Response(200), TRIAL_URL: httpx.Response(200)}),
        rate_limits=False,
    )
    first = [r.url for r in check_links(session_factory, client, sample=1, seed=7).results]
    second = [r.url for r in check_links(session_factory, client, sample=1, seed=7).results]
    assert len(first) == 1
    assert first == second
