"""What every collector produces, and the one way fetched documents are stored."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.repository import link_company_document, save_source_document
from catalyst.schemas import DocumentMatch, SourceDocument, SourceTier
from catalyst.snapshots import SnapshotStore

NEW_YORK = ZoneInfo("America/New_York")


def new_york_midnight(day: date) -> datetime:
    """Read a date-only publication time as the start of that day in New York."""
    return datetime.combine(day, time(0), NEW_YORK).astimezone(UTC)


@dataclass(frozen=True)
class FetchedDocument:
    """One document as fetched, before storage."""

    source_key: str
    url: str
    publisher: str
    source_tier: SourceTier
    document_type: str
    published_at: datetime
    content: bytes
    content_type: str


@dataclass
class CollectionStats:
    """Counts for one collector run."""

    source: str
    created: int = 0
    existing: int = 0
    error: str | None = None
    documents: list[SourceDocument] = field(default_factory=list[SourceDocument])

    def add(self, document: SourceDocument, created: bool) -> None:
        """Count one stored document."""
        self.documents.append(document)
        if created:
            self.created += 1
        else:
            self.existing += 1


def _utc_now() -> datetime:
    return datetime.now(UTC)


def store_fetched(
    fetched: FetchedDocument,
    store: SnapshotStore,
    session_factory: sessionmaker[Session],
    company: tuple[UUID, DocumentMatch] | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> tuple[SourceDocument, bool]:
    """Snapshot the bytes, record the document once per source key, and link it to a company if given."""
    snapshot = store.put(fetched.content)
    document = SourceDocument(
        source_key=fetched.source_key,
        url=fetched.url,
        publisher=fetched.publisher,
        source_tier=fetched.source_tier,
        document_type=fetched.document_type,
        published_at=fetched.published_at,
        retrieved_at=now(),
        sha256=snapshot.sha256,
        snapshot_key=snapshot.key,
        content_type=fetched.content_type,
    )
    with session_factory.begin() as session:
        saved, created = save_source_document(session, document)
        if company is not None:
            link_company_document(session, company[0], saved.id, company[1])
    return saved, created
