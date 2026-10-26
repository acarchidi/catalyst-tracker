"""Recompress the bucket's plain source copies, and report what fills it (ADR 0046).

`compress_snapshots` turns each plain copy into its compressed copy without ever leaving a key
unreadable: it reads the plain copy and checks its hash, stores the compressed copy, and the
store deletes the plain copy only after reading the compressed one back and checking it. Every
step can be stopped and re-run. Logs and reports hold counts and sizes, never content.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from catalyst.db.tables import TABLES
from catalyst.logs import log_alert, log_event
from catalyst.schemas import SourceDocument
from catalyst.snapshots import (
    COMPRESSED_SUFFIX,
    KEY_PATTERN,
    SnapshotIntegrityError,
    SnapshotStoreError,
    StoredObject,
    SupabaseSnapshotStore,
    compress_copy,
)

_log = logging.getLogger(__name__)
DEFAULT_WORKERS = 8
NO_ROW = "no database row"


class Outcome(StrEnum):
    """What happened to one plain copy."""

    COMPRESSED = "compressed"  # the compressed copy is checked and the plain copy is gone
    TOO_LARGE = "too_large"  # over the size cap: kept plain
    CHANGED = "changed"  # the plain copy no longer matches its hash: left alone
    CHECK_FAILED = "check_failed"  # the compressed copy read back wrong: plain copy kept
    ERROR = "errors"  # a storage or network error: nothing deleted
    GONE = "gone"  # the plain copy disappeared between listing and reading
    NOT_STARTED = "not_started"  # the time limit came first


_PROBLEMS = (Outcome.CHANGED, Outcome.CHECK_FAILED, Outcome.ERROR)


@dataclass
class CompressReport:
    """Counts and sizes from one `compress_snapshots` run."""

    dry_run: bool
    objects: int = 0
    plain: int = 0
    already_compressed: int = 0
    other: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    # One count per `Outcome`, named by its value.
    compressed: int = 0
    too_large: int = 0
    changed: int = 0
    check_failed: int = 0
    errors: int = 0
    gone: int = 0
    not_started: int = 0

    def count(self, outcome: Outcome) -> None:
        """Count one copy's outcome."""
        setattr(self, outcome.value, getattr(self, outcome.value) + 1)

    @property
    def problems(self) -> int:
        """Copies that need a person: changed, failed the read-back check, or could not be read."""
        return sum(getattr(self, o.value) for o in _PROBLEMS)

    @property
    def ok(self) -> bool:
        """True when no copy needs a person (copies not started for lack of time are fine)."""
        return self.problems == 0

    def summary(self) -> dict[str, Any]:
        """Return the report as plain counts, for printing."""
        return asdict(self) | {"ok": self.ok}


@dataclass(frozen=True)
class _Result:
    outcome: Outcome
    removed: int = 0  # bytes of the plain copy deleted (or that would be)
    added: int = 0  # bytes of the compressed copy stored (or that would be)


def _compress_one(
    store: SupabaseSnapshotStore, plain: StoredObject, has_compressed: bool, dry_run: bool
) -> _Result:
    key = plain.name
    try:
        content = store.read_plain(key)
        if content is None:
            return _Result(Outcome.GONE)
        if len(content) > store.max_snapshot_bytes:
            return _Result(Outcome.TOO_LARGE)
        added = 0 if has_compressed else len(compress_copy(content))
        if not dry_run:
            store.store_compressed(key, content)
            try:
                store.delete_plain(key)
            except SnapshotIntegrityError:
                store.discard_damaged_compressed(key)
                return _Result(Outcome.CHECK_FAILED)
        return _Result(Outcome.COMPRESSED, removed=plain.size_bytes, added=added)
    except SnapshotIntegrityError:
        return _Result(Outcome.CHANGED)
    except (SnapshotStoreError, httpx.HTTPError):
        return _Result(Outcome.ERROR)


def compress_snapshots(
    store: SupabaseSnapshotStore,
    *,
    dry_run: bool = False,
    workers: int = DEFAULT_WORKERS,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CompressReport:
    """Compress every plain copy in the bucket; with `dry_run`, only measure what it would save.

    `deadline` is a `clock` reading after which no further copy is started, so a run can end
    cleanly before its job's time limit; the next run picks up the rest.
    """
    report = CompressReport(dry_run=dry_run)
    listed = list(store.list_objects())
    names = {o.name for o in listed}
    plain: list[StoredObject] = []
    for item in listed:
        report.objects += 1
        report.bytes_before += item.size_bytes
        kind, _ = _format(item.name)
        if kind == "plain":
            plain.append(item)
        elif kind == "gzip":
            report.already_compressed += 1
        else:
            report.other += 1
    report.plain = len(plain)
    report.bytes_after = report.bytes_before

    def run(item: StoredObject) -> _Result:
        if deadline is not None and clock() > deadline:
            return _Result(Outcome.NOT_STARTED)
        return _compress_one(store, item, item.name + COMPRESSED_SUFFIX in names, dry_run)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for done, result in enumerate(pool.map(run, plain), start=1):
            report.count(result.outcome)
            report.bytes_after += result.added - result.removed
            if done % 1000 == 0:
                log_event(_log, "snapshots_compress_progress", done=done, of=len(plain))
    fields = report.summary()
    if report.ok:
        log_event(_log, "snapshots_compress_finished", **fields)
    else:
        log_alert(_log, "snapshots_compress_finished", **fields)
    return report


@dataclass
class UsageReport:
    """The bucket's size in total, by stored format, and by the source that stored each copy."""

    objects: int = 0
    bytes: int = 0
    by_format: dict[str, dict[str, int]] = field(default_factory=dict[str, dict[str, int]])
    by_source: dict[str, dict[str, int]] = field(default_factory=dict[str, dict[str, int]])


def _is_key(name: str) -> bool:
    match = KEY_PATTERN.match(name)
    return match is not None and match.group(1) == match.group(2)[:2]


def _format(name: str) -> tuple[str, str | None]:
    """Return an object's format and the snapshot key it holds (None for anything else)."""
    if _is_key(name):
        return "plain", name
    key = name.removesuffix(COMPRESSED_SUFFIX)
    if name.endswith(COMPRESSED_SUFFIX) and _is_key(key):
        return "gzip", key
    return "other", None


def _add(table: defaultdict[str, dict[str, int]], group: str, size: int) -> None:
    table[group]["objects"] += 1
    table[group]["bytes"] += size


def snapshot_usage(store: SupabaseSnapshotStore, sources: Mapping[str, str] | None = None) -> UsageReport:
    """Total the bucket by format, and, given each key's source group, by source."""
    report = UsageReport()
    formats: defaultdict[str, dict[str, int]] = defaultdict(lambda: {"objects": 0, "bytes": 0})
    by_source: defaultdict[str, dict[str, int]] = defaultdict(lambda: {"objects": 0, "bytes": 0})
    for item in store.list_objects():
        report.objects += 1
        report.bytes += item.size_bytes
        kind, key = _format(item.name)
        _add(formats, kind, item.size_bytes)
        if sources is not None:
            _add(by_source, sources.get(key, NO_ROW) if key else NO_ROW, item.size_bytes)
    report.by_format = dict(formats)
    report.by_source = dict(sorted(by_source.items(), key=lambda kv: -kv[1]["bytes"]))
    log_event(_log, "snapshots_usage", objects=report.objects, bytes=report.bytes)
    return report


def source_group(source_key: str, document_type: str) -> str:
    """Name the collector that stored a document; SEC documents also by form, such as "sec 10-Q"."""
    collector = source_key.split(":", 1)[0]
    return f"{collector} {document_type}" if collector == "sec" else collector


def snapshot_sources(session: Session) -> dict[str, str]:
    """Map each snapshot key to the source group of the first document recorded with it.

    Documents with the same bytes share one copy, so each copy is counted once.
    """
    table = TABLES[SourceDocument]
    query = select(table.c.snapshot_key, table.c.source_key, table.c.document_type).order_by(
        table.c.source_key
    )
    groups: dict[str, str] = {}
    for key, source_key, document_type in session.execute(query):
        groups.setdefault(key, source_group(source_key, document_type))
    return groups
