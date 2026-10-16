"""Source copies are stored under their SHA-256 hash, so storing the same bytes twice is harmless."""

import hashlib
from pathlib import Path

import pytest

from catalyst.snapshots import LocalSnapshotStore, SnapshotIntegrityError, SnapshotNotFoundError, snapshot_key


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


def test_key_names_the_hash(tmp_path: Path) -> None:
    snapshot = LocalSnapshotStore(tmp_path).put(b"x")
    assert snapshot.key == f"sha256/{snapshot.sha256[:2]}/{snapshot.sha256}"


def test_get_refuses_a_copy_that_changed(tmp_path: Path) -> None:
    store = LocalSnapshotStore(tmp_path)
    snapshot = store.put(b"what the page said")
    (tmp_path / snapshot.key).write_bytes(b"what someone edited")
    with pytest.raises(SnapshotIntegrityError):
        store.get(snapshot.key)


def test_get_of_a_missing_copy_says_so(tmp_path: Path) -> None:
    store = LocalSnapshotStore(tmp_path)
    with pytest.raises(SnapshotNotFoundError):
        store.get(snapshot_key(hashlib.sha256(b"never stored").hexdigest()))


@pytest.mark.parametrize("key", ["../etc/passwd", "sha256/ab/xyz", "sha256/00/" + "a" * 64, "a" * 64])
def test_malformed_keys_are_refused(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError, match="snapshot key"):
        LocalSnapshotStore(tmp_path).get(key)
