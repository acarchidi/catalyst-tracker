"""`verify_snapshots` re-reads every stored copy and proves it still matches its recorded hash."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.repository import save_source_document
from catalyst.schemas import SourceDocument, SourceTier
from catalyst.snapshots import (
    LocalSnapshotStore,
    Snapshot,
    SnapshotStoreError,
    snapshot_key,
    verify_snapshots,
)


def _record(session_factory: sessionmaker[Session], snapshot: Snapshot, name: str) -> None:
    doc = SourceDocument(
        source_key=f"sec:0000000001-24-000001:{name}",
        url=f"https://www.sec.gov/Archives/edgar/data/1/000000000124000001/{name}",
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type="8-K EX-99.1",
        published_at=datetime(2024, 10, 30, 20, 5, tzinfo=UTC),
        retrieved_at=datetime(2024, 10, 31, 12, 0, tzinfo=UTC),
        sha256=snapshot.sha256,
        snapshot_key=snapshot.key,
        content_type="text/html",
    )
    with session_factory.begin() as session:
        save_source_document(session, doc)


def test_every_intact_copy_passes(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    store = LocalSnapshotStore(tmp_path)
    for name in ("a.htm", "b.htm"):
        _record(session_factory, store.put(name.encode()), name)
    report = verify_snapshots(session_factory, store)
    assert (report.checked, report.missing, report.changed) == (2, [], [])
    assert report.ok


def test_a_changed_and_a_missing_copy_are_both_named(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    changed = store.put(b"original")
    _record(session_factory, changed, "changed.htm")
    (tmp_path / changed.key).write_bytes(b"edited")
    gone = store.put(b"will be deleted")
    _record(session_factory, gone, "gone.htm")
    (tmp_path / gone.key).unlink()
    _record(session_factory, store.put(b"fine"), "fine.htm")

    report = verify_snapshots(session_factory, store)

    assert report.checked == 3
    assert report.changed == ["sec:0000000001-24-000001:changed.htm"]
    assert report.missing == ["sec:0000000001-24-000001:gone.htm"]
    assert not report.ok


def test_a_row_whose_key_does_not_match_its_hash_counts_as_changed(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    real = store.put(b"bytes")
    other_hash = "f" * 64
    wrong = Snapshot(sha256=other_hash, key=real.key, size_bytes=real.size_bytes)
    _record(session_factory, wrong, "wrong.htm")
    report = verify_snapshots(session_factory, store)
    assert report.changed == ["sec:0000000001-24-000001:wrong.htm"]
    assert snapshot_key(other_hash) != real.key


class _FlakyStore(LocalSnapshotStore):
    """Fails on one key, works for the rest."""

    def __init__(self, root: Path, bad_key: str, error: Exception) -> None:
        super().__init__(root)
        self.bad_key = bad_key
        self.error = error

    def get(self, key: str) -> bytes:
        if key == self.bad_key:
            raise self.error
        return super().get(key)


@pytest.mark.parametrize(
    "error",
    [SnapshotStoreError("HTTP 500"), httpx.ConnectError("refused"), ValueError("not a snapshot key")],
)
def test_one_unreadable_copy_is_listed_and_the_audit_continues(
    session_factory: sessionmaker[Session], tmp_path: Path, error: Exception
) -> None:
    plain = LocalSnapshotStore(tmp_path)
    bad = plain.put(b"bad")
    _record(session_factory, bad, "a-bad.htm")
    _record(session_factory, plain.put(b"good"), "b-good.htm")
    report = verify_snapshots(session_factory, _FlakyStore(tmp_path, bad.key, error))
    assert report.checked == 2
    assert report.unreadable == ["sec:0000000001-24-000001:a-bad.htm"]
    assert (report.missing, report.changed) == ([], [])
    assert not report.ok
