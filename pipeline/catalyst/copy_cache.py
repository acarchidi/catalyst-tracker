"""A run's stored copies, each read at most once and several at a time (ADR 0064).

Six steps of the extract run read the same stored copies: matching, the company, drug, and type
checks, the publish decisions, and the writer. Each used to read its own, one at a time, at
about half a second a copy from the storage bucket. `CachedStore` keeps the bytes of each copy
it has read for the rest of the run, up to a limit, and `prefetch` reads the copies a run will
need eight at a time, as `snapshots-verify` does (ADR 0054).

The bytes kept are the ones the underlying store returned after checking them against their
hash, and a copy is keyed by that hash, so a kept copy is the copy. A copy that could not be
read is not kept: the step that needs it reads it again and handles the error as before.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from catalyst.db.tables import TABLES
from catalyst.extract.agent import ROLE
from catalyst.logs import log_event
from catalyst.schemas import Claim, SourceDocument
from catalyst.snapshots import Snapshot, SnapshotStore, SnapshotStoreError

MAX_BYTES = 512 * 2**20  # far above the 453 copies of November 2026, far below a runner's memory
WORKERS = 8

_log = logging.getLogger(__name__)


class CachedStore:
    """A snapshot store that keeps, for one run, the copies it has read."""

    def __init__(self, store: SnapshotStore, max_bytes: int = MAX_BYTES) -> None:
        """Keep up to `max_bytes` of the copies read from `store`."""
        self._store = store
        self._max = max_bytes
        self._kept: dict[str, bytes] = {}
        self._bytes = 0
        self._lock = threading.Lock()

    def put(self, content: bytes) -> Snapshot:
        """Store `content` in the underlying store."""
        return self._store.put(content)

    def get(self, key: str) -> bytes:
        """Return the copy at `key`, read from the underlying store only the first time."""
        with self._lock:
            kept = self._kept.get(key)
        if kept is not None:
            return kept
        content = self._store.get(key)
        with self._lock:
            if key not in self._kept and self._bytes + len(content) <= self._max:
                self._kept[key] = content
                self._bytes += len(content)
        return content

    def _try(self, key: str) -> bool:
        try:
            self.get(key)
        except (SnapshotStoreError, httpx.HTTPError, OSError, ValueError) as error:
            log_event(_log, "prefetch_failed", snapshot_key=key, error=type(error).__name__)
            return False
        return True

    def prefetch(self, keys: Iterable[str], workers: int = WORKERS) -> int:
        """Read each copy not yet kept, `workers` at a time; return how many are now kept."""
        todo = list(dict.fromkeys(keys))
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(self._try, todo))
        with self._lock:
            kept = sum(key in self._kept for key in todo)
        log_event(_log, "copies_prefetched", copies=len(todo), kept=kept)
        return kept


def claim_copy_keys(session: Session) -> list[str]:
    """Return the stored copies of every document an extractor catalyst claim comes from."""
    claim, document = TABLES[Claim], TABLES[SourceDocument]
    query = (
        select(document.c.snapshot_key)
        .join(claim, claim.c.source_document_id == document.c.id)
        .where(claim.c.agent == ROLE, claim.c.field.startswith("catalyst:"))
        .distinct()
        .order_by(document.c.snapshot_key)
    )
    return list(session.execute(query).scalars())
