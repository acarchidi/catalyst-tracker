"""Wire-service RSS collector: press-release headlines and links, stored as the feed item.

Feeds are web content, so robots.txt applies. Only the feed item (title, link, date, summary)
is stored; full releases are fetched later, per document, by the extractor items.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as SafeXml

from catalyst.collectors.documents import FetchedDocument
from catalyst.collectors.http import PoliteHttpClient
from catalyst.schemas import SourceTier

REQUESTS_PER_SECOND = 0.5


@dataclass(frozen=True)
class Feed:
    """One RSS feed and who publishes it."""

    slug: str
    publisher: str
    url: str


FEEDS = (
    Feed(
        slug="prnewswire",
        publisher="PR Newswire",
        url="https://www.prnewswire.com/rss/health-latest-news/biotechnology-list.rss",
    ),
)


def _text(item: Element, tag: str) -> str:
    found = item.find(tag)
    return (found.text or "").strip() if found is not None else ""


def _aware(moment: datetime) -> datetime:
    """RFC 2822 "-0000" means UTC with no local zone given; Python returns it without a zone."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def parse_feed(feed: Feed, xml: bytes) -> list[FetchedDocument]:
    """Turn each item that has an https link and a date into a stored document keyed by its guid."""
    root = SafeXml.fromstring(xml)
    documents: list[FetchedDocument] = []
    for item in root.iter("item"):
        link, published = _text(item, "link"), _text(item, "pubDate")
        if not link.startswith("https://") or not published:
            continue
        identity = _text(item, "guid") or link
        documents.append(
            FetchedDocument(
                source_key=f"wire:{feed.slug}:{identity}",
                url=link,
                publisher=feed.publisher,
                source_tier=SourceTier.WIRE,
                document_type="Wire press release (feed item)",
                published_at=_aware(parsedate_to_datetime(published)),
                content=SafeXml.tostring(item),
                content_type="application/rss+xml",
            )
        )
    return documents


def collect_feed(client: PoliteHttpClient, feed: Feed) -> Iterator[FetchedDocument]:
    """Yield the feed's current items."""
    yield from parse_feed(feed, client.get(feed.url).content)
