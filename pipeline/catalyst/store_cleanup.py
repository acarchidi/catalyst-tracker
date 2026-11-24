"""Delete stored copies of filings that are fully read and back no claim (ADR 0066).

The owner (2026-11-23): "we have no need to store them once they are fully read". A stored copy is
evidence only while a claim rests on it (spec: Provenance; ADR 0008, ADR 0046), so a copy is
deleted only when every one of these holds for every document that names it (copies are stored
by hash, so two documents with the same bytes share one copy):

- the document is a 10-Q, a 10-K, or an 8-K exhibit other than 99.1 (`is_filing`);
- it is fully read (`source_document.fully_read_at`);
- no claim of any reader, published or held, comes from it.

The document's row stays, with `copy_deleted_at` set, so its link, its readings, and the fact that
it was read all remain, and the collector never fetches it again. The default is a dry run that
counts; nothing is deleted without `delete=True`. Each copy is checked again, with its rows locked,
just before it is deleted. Re-running deletes nothing twice. Logs and summaries hold counts only.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.extract.filings import filing_documents, is_filing
from catalyst.logs import log_event
from catalyst.schemas import Claim, SourceDocument
from catalyst.snapshots import DeletingStore, SnapshotStoreError

TO_DELETE = "to_delete"
DELETED = "deleted"
ALREADY_DELETED = "already_deleted"
KEPT_NOT_READ = "kept_not_fully_read"
KEPT_CLAIM = "kept_backs_a_claim"
KEPT_SHARED = "kept_copy_shared"

_log = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Row:
    document_type: str
    fully_read: bool
    deleted: bool
    backs_a_claim: bool

    @property
    def may_go(self) -> bool:
        """Say whether this document lets its copy be deleted."""
        return is_filing(self.document_type) and self.fully_read and not self.backs_a_claim


@dataclass
class CleanupReport:
    """What one clean-up found and did: counts by document type, never content."""

    deleting: bool
    by_type: defaultdict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter[str]))
    copies_to_delete: int = 0
    copies_deleted: int = 0
    copies_failed: int = 0

    def summary(self) -> dict[str, Any]:
        """Return the counts as plain JSON."""
        return {
            "deleting": self.deleting,
            "copies_to_delete": self.copies_to_delete,
            "copies_deleted": self.copies_deleted,
            "copies_failed": self.copies_failed,
            "by_document_type": {
                kind: dict(sorted((+counts).items()))
                for kind, counts in sorted(self.by_type.items())
                if +counts
            },
        }


def _backs_a_claim() -> Any:
    documents, claims = TABLES[SourceDocument], TABLES[Claim]
    return exists().where(claims.c.source_document_id == documents.c.id)


def _rows(session: Session, keys: Any, *, lock: bool = False) -> dict[str, list[_Row]]:
    """Return every document naming each copy, with what decides whether the copy may go."""
    documents = TABLES[SourceDocument]
    query = select(
        documents.c.snapshot_key,
        documents.c.document_type,
        documents.c.fully_read_at.is_not(None).label("fully_read"),
        documents.c.copy_deleted_at.is_not(None).label("deleted"),
        _backs_a_claim().label("backs_a_claim"),
    ).where(documents.c.snapshot_key.in_(keys))
    if lock:
        query = query.with_for_update(of=documents)
    found: dict[str, list[_Row]] = defaultdict(list)
    for row in session.execute(query):
        found[row.snapshot_key].append(
            _Row(row.document_type, row.fully_read, row.deleted, row.backs_a_claim)
        )
    return found


def _verdict(row: _Row, others: list[_Row]) -> str:
    if row.deleted:
        return ALREADY_DELETED
    if not row.fully_read:
        return KEPT_NOT_READ
    if row.backs_a_claim:
        return KEPT_CLAIM
    if not all(other.may_go for other in others):
        return KEPT_SHARED
    return TO_DELETE


def classify(session: Session, report: CleanupReport) -> dict[str, list[str]]:
    """Count every filing's copy by what may happen to it; return the copies that may be deleted.

    Each copy that may go comes with the document types of the documents naming it.
    """
    in_scope = select(TABLES[SourceDocument].c.snapshot_key).where(filing_documents())
    deletable: dict[str, list[str]] = {}
    for key, rows in sorted(_rows(session, in_scope.scalar_subquery()).items()):
        live = [row for row in rows if not row.deleted]
        verdicts = [_verdict(row, [o for o in live if o is not row]) for row in rows]
        for row, verdict in zip(rows, verdicts, strict=True):
            if is_filing(row.document_type):
                report.by_type[row.document_type][verdict] += 1
        if live and all(row.may_go for row in live):
            deletable[key] = [row.document_type for row in live]
    report.copies_to_delete = len(deletable)
    return deletable


def _delete(session_factory: sessionmaker[Session], store: DeletingStore, key: str, at: datetime) -> bool:
    """Check the copy again with its rows locked, then delete it and mark its rows; return whether it went.

    The copy is deleted before the rows are marked: if marking fails, the next run finds the copy
    gone, deletes nothing more, and marks the rows then.
    """
    documents = TABLES[SourceDocument]
    with session_factory.begin() as session:
        live = [row for row in _rows(session, [key], lock=True).get(key, []) if not row.deleted]
        if not live or not all(row.may_go for row in live):
            return False
        store.delete_copy(key)
        session.execute(
            update(documents)
            .where(documents.c.snapshot_key == key, documents.c.copy_deleted_at.is_(None))
            .values(copy_deleted_at=at)
        )
    return True


def clean_store(
    session_factory: sessionmaker[Session],
    store: DeletingStore,
    *,
    delete: bool = False,
    now: Callable[[], datetime] = _utc_now,
) -> CleanupReport:
    """Count, and with `delete` also delete, the stored copies of fully read filings that back no claim."""
    report = CleanupReport(deleting=delete)
    with session_factory() as session:
        deletable = classify(session, report)
    if delete:
        for key, kinds in deletable.items():
            try:
                went = _delete(session_factory, store, key, now())
            except (SnapshotStoreError, httpx.HTTPError, OSError) as error:
                report.copies_failed += 1
                log_event(_log, "copy_not_deleted", error=type(error).__name__)
                continue
            if went:
                report.copies_deleted += 1
                for kind in kinds:
                    report.by_type[kind][TO_DELETE] -= 1
                    report.by_type[kind][DELETED] += 1
    log_event(
        _log,
        "store_cleaned",
        deleting=delete,
        copies_to_delete=report.copies_to_delete,
        copies_deleted=report.copies_deleted,
        copies_failed=report.copies_failed,
    )
    return report
