"""Walking skeleton: fetch a company's latest 8-K press release, store a copy, record one row.

This is the thinnest end-to-end path (quality-gates.md, CP1). Later collectors reuse its parts.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.documents import FetchedDocument, store_fetched
from catalyst.collectors.sec.edgar import SUBMISSIONS_URL, EdgarClient, find_press_release, recent_filings
from catalyst.collectors.sec.filings import PUBLISHER, source_key
from catalyst.logs import log_event
from catalyst.schemas import SourceDocument, SourceTier
from catalyst.snapshots import LocalSnapshotStore

MAX_FILINGS_TO_CHECK = 10

_log = logging.getLogger(__name__)


class NoPressReleaseError(LookupError):
    """None of the company's recent 8-K filings has a press-release exhibit."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def collect_latest_press_release(
    client: EdgarClient,
    store: LocalSnapshotStore,
    session_factory: sessionmaker[Session],
    cik: int,
    now: Callable[[], datetime] = _utc_now,
) -> tuple[SourceDocument, bool]:
    """Store the newest 8-K Exhibit 99.1 for company `cik` and return its record.

    Returns the record and whether this run created it. Re-running creates no duplicate.
    """
    submissions = client.get(SUBMISSIONS_URL.format(cik=cik))
    for filing in recent_filings(submissions.content, "8-K")[:MAX_FILINGS_TO_CHECK]:
        exhibit = find_press_release(client.get(filing.index_url).text)
        if exhibit is None:
            continue
        response = client.get(exhibit.url)
        fetched = FetchedDocument(
            source_key=source_key(filing, exhibit),
            url=exhibit.url,
            publisher=PUBLISHER,
            source_tier=SourceTier.REGULATOR_OR_SEC,
            document_type=f"{filing.form} {exhibit.type}",
            published_at=filing.accepted_at,
            content=response.content,
            content_type=response.headers.get("content-type", "application/octet-stream").split(";")[0],
        )
        saved, created = store_fetched(fetched, store, session_factory, now=now)
        log_event(_log, "source_document_saved", id=str(saved.id), url=saved.url, created=created)
        return saved, created
    raise NoPressReleaseError(f"no 8-K with a press-release exhibit among CIK {cik}'s latest filings")
