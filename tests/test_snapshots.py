"""Source copies are stored under their SHA-256 hash, so storing the same bytes twice is harmless."""

import hashlib
from pathlib import Path

from catalyst.snapshots import LocalSnapshotStore


def test_put_returns_sha256_and_stores_exact_bytes(tmp_path: Path) -> None:
    store = LocalSnapshotStore(tmp_path)
    content = b"<html>Exhibit 99.1 press release</html>"
    snapshot = store.put(content)
    assert snapshot.sha256 == hashlib.sha256(content).hexdigest()
    assert snapshot.size_bytes == len(content)
    assert store.get(snapshot.key) == content


def test_put_is_idempotent(tmp_path: Path) -> None:
    store = LocalSnapshotStore(tmp_path)
    first = store.put(b"same bytes")
    second = store.put(b"same bytes")
    assert first == second
    assert len([p for p in tmp_path.rglob("*") if p.is_file()]) == 1


def test_different_bytes_get_different_keys(tmp_path: Path) -> None:
    store = LocalSnapshotStore(tmp_path)
    assert store.put(b"a").key != store.put(b"b").key
