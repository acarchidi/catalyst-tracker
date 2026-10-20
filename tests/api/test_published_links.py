"""The weekly link check reads only the links published records cite (spec: Provenance, item 6; ADR 0042).

A link the read API would never show is not a published link, so it is not checked or counted.
Missing and moved links are dead; a refusal or a site error is counted but is not a dead link,
since it says nothing about the document itself.
"""

from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session, sessionmaker

from api.conftest import Seed
from catalyst.db.repository import save_source_document
from catalyst.links import (
    LinkReport,
    LinkResult,
    LinkStatus,
    check_links,
    link_client,
    link_summary,
    published_urls,
)
from catalyst.schemas import SourceDocument, SourceTier

SEC_1 = "https://www.sec.gov/Archives/edgar/data/1111/000001/ex99-1.htm"
SEC_2 = "https://www.sec.gov/Archives/edgar/data/1111/000002/ex99-1.htm"
UNCITED = "https://www.sec.gov/Archives/edgar/data/1111/000009/ex99-1.htm"


def _uncited(session_factory: sessionmaker[Session]) -> None:
    sha = "9" * 64
    document = SourceDocument(
        source_key="sec:uncited",
        url=UNCITED,
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type="8-K EX-99.1",
        published_at=datetime(2026, 8, 3, tzinfo=UTC),
        retrieved_at=datetime(2026, 8, 3, tzinfo=UTC),
        sha256=sha,
        snapshot_key=f"sha256/99/{sha}",
        content_type="text/html",
    )
    with session_factory.begin() as session:
        save_source_document(session, document)


def test_only_links_that_published_records_cite_are_listed(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    _uncited(session_factory)
    assert published_urls(session_factory) == [SEC_1, SEC_2]


def test_the_check_fetches_only_the_given_links(session_factory: sessionmaker[Session], seed: Seed) -> None:
    _uncited(session_factory)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(200, content=b"User-agent: *\nAllow: /\n")
        return httpx.Response(200 if str(request.url) == SEC_1 else 404)

    client = link_client("data@example.org", httpx.MockTransport(handler), rate_limits=False)
    report = check_links(session_factory, client, urls=published_urls(session_factory))
    assert [(r.url, r.status) for r in report.results] == [(SEC_1, LinkStatus.OK), (SEC_2, LinkStatus.BROKEN)]
    assert UNCITED not in seen


def test_the_summary_counts_missing_and_moved_links_as_dead_and_lists_them() -> None:
    report = LinkReport(
        [
            LinkResult(SEC_1, LinkStatus.OK),
            LinkResult(SEC_2, LinkStatus.BROKEN, "HTTP 404"),
            LinkResult(UNCITED, LinkStatus.MOVED, "moved to https://www.sec.gov/new"),
            LinkResult("https://clinicaltrials.gov/study/NCT1", LinkStatus.FAILED, "HTTP 503"),
        ]
    )
    summary = link_summary(report)
    assert summary == {
        "checked": 4,
        "working": 1,
        "dead": 2,
        "dead_percent": "50.0",
        "not_answered": 1,
        "dead_links": [{"url": SEC_2, "verdict": "missing"}, {"url": UNCITED, "verdict": "moved"}],
    }


def test_an_empty_check_has_no_dead_rate() -> None:
    assert link_summary(LinkReport())["dead_percent"] is None
