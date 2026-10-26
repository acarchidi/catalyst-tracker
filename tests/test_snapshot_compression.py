"""Compressed source copies in Supabase Storage (ADR 0046).

New copies are stored gzip-compressed at `<key>.gz`, where the key is still the SHA-256 of the
original bytes. Reads try the compressed copy first, fall back to a plain copy stored before
compression, inflate under a size cap, and always check the original bytes against the key.
"""

from __future__ import annotations

import gzip
import hashlib

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from catalyst.snapshots import (
    COMPRESSED_SUFFIX,
    SnapshotIntegrityError,
    SnapshotNotFoundError,
    SnapshotStoreError,
    SupabaseSnapshotStore,
    compress_copy,
    decompress_copy,
    snapshot_key,
)
from fake_storage import BUCKET, KEY, URL, FakeStorage, store_for


def _key(content: bytes) -> str:
    return snapshot_key(hashlib.sha256(content).hexdigest())


def _small_store(fake: FakeStorage, limit: int) -> SupabaseSnapshotStore:
    return SupabaseSnapshotStore(URL, KEY, transport=httpx.MockTransport(fake), max_snapshot_bytes=limit)


@settings(max_examples=60)
@given(st.binary(max_size=4096))
def test_any_bytes_come_back_exactly(content: bytes) -> None:
    fake = FakeStorage()
    store = store_for(fake)
    snapshot = store.put(content)
    assert snapshot.key == _key(content)
    assert snapshot.size_bytes == len(content)
    assert store.get(snapshot.key) == content


def test_the_compressed_copy_is_the_same_for_the_same_bytes() -> None:
    content = b"<html>" + b"Phase 3 topline data expected 2H26. " * 200 + b"</html>"
    assert compress_copy(content) == compress_copy(content)
    assert len(compress_copy(content)) < len(content) // 10
    assert gzip.decompress(compress_copy(content)) == content


def test_storing_compressed_bytes_twice_keeps_one_object() -> None:
    fake = FakeStorage()
    store = store_for(fake)
    store.put(b"same bytes")
    store.put(b"same bytes")
    assert list(fake.objects) == [f"{BUCKET}/{_key(b'same bytes')}{COMPRESSED_SUFFIX}"]


def test_a_plain_copy_stored_before_compression_is_still_read() -> None:
    fake = FakeStorage()
    content = b"an 8-K exhibit stored in October"
    fake.objects[f"{BUCKET}/{_key(content)}"] = content
    assert store_for(fake).get(_key(content)) == content


def test_the_compressed_copy_is_read_first() -> None:
    fake = FakeStorage()
    content = b"what the page said"
    key = _key(content)
    fake.objects[f"{BUCKET}/{key}{COMPRESSED_SUFFIX}"] = compress_copy(content)
    fake.objects[f"{BUCKET}/{key}"] = content
    store_for(fake).get(key)
    reads = [r.url.path for r in fake.requests if r.method == "GET"]
    assert reads == [f"/storage/v1/object/authenticated/{BUCKET}/{key}{COMPRESSED_SUFFIX}"]


def test_a_damaged_compressed_copy_is_refused_even_when_a_plain_copy_exists() -> None:
    fake = FakeStorage()
    content = b"original"
    key = _key(content)
    fake.objects[f"{BUCKET}/{key}{COMPRESSED_SUFFIX}"] = compress_copy(b"edited")
    fake.objects[f"{BUCKET}/{key}"] = content
    with pytest.raises(SnapshotIntegrityError):
        store_for(fake).get(key)


def test_a_failed_read_of_the_compressed_copy_is_an_error_not_a_fallback() -> None:
    fake = FakeStorage()
    content = b"original"
    key = _key(content)
    fake.objects[f"{BUCKET}/{key}{COMPRESSED_SUFFIX}"] = compress_copy(content)
    fake.objects[f"{BUCKET}/{key}"] = content
    fake.failing_reads.add(f"{BUCKET}/{key}{COMPRESSED_SUFFIX}")
    with pytest.raises(SnapshotStoreError, match="HTTP 500"):
        store_for(fake).get(key)


def test_neither_copy_means_not_found() -> None:
    with pytest.raises(SnapshotNotFoundError):
        store_for(FakeStorage()).get(_key(b"never stored"))


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(compress_copy(b"x" * 1000)[:-9], id="truncated"),
        pytest.param(compress_copy(b"x" * 1000) + b"trailing", id="trailing-bytes"),
        pytest.param(compress_copy(b"x" * 1000) + compress_copy(b"y"), id="second-member"),
        pytest.param(b"x" * 1000, id="not-gzip"),
    ],
)
def test_a_malformed_compressed_copy_is_refused(stored: bytes) -> None:
    key = _key(b"x" * 1000)
    with pytest.raises(SnapshotIntegrityError):
        decompress_copy(key, stored)


def test_a_decompression_bomb_stops_at_the_cap() -> None:
    bomb = compress_copy(b"\0" * 5_000_000)  # about 5 KB that inflates to 5 MB
    with pytest.raises(SnapshotIntegrityError, match="larger than"):
        decompress_copy(_key(b"\0" * 5_000_000), bomb, limit=1_000_000)


def test_the_store_applies_its_cap_on_every_read() -> None:
    fake = FakeStorage()
    content = b"\0" * 5000
    fake.objects[f"{BUCKET}/{_key(content)}{COMPRESSED_SUFFIX}"] = compress_copy(content)
    with pytest.raises(SnapshotIntegrityError, match="larger than"):
        _small_store(fake, 1000).get(_key(content))


def test_bytes_over_the_cap_are_stored_plain_so_they_stay_readable() -> None:
    fake = FakeStorage()
    store = _small_store(fake, 1000)
    content = b"a" * 1001
    snapshot = store.put(content)
    assert list(fake.objects) == [f"{BUCKET}/{snapshot.key}"]
    assert store.get(snapshot.key) == content


def test_delete_plain_removes_the_plain_copy_only_after_checking_the_compressed_one() -> None:
    fake = FakeStorage()
    store = store_for(fake)
    content = b"a quote lives here"
    key = _key(content)
    fake.objects[f"{BUCKET}/{key}"] = content
    store.store_compressed(key, content)
    store.delete_plain(key)
    assert list(fake.objects) == [f"{BUCKET}/{key}{COMPRESSED_SUFFIX}"]
    assert store.get(key) == content


@pytest.mark.parametrize("compressed", [None, compress_copy(b"edited"), b"not gzip"])
def test_delete_plain_refuses_without_a_good_compressed_copy(compressed: bytes | None) -> None:
    fake = FakeStorage()
    content = b"a quote lives here"
    key = _key(content)
    fake.objects[f"{BUCKET}/{key}"] = content
    if compressed is not None:
        fake.objects[f"{BUCKET}/{key}{COMPRESSED_SUFFIX}"] = compressed
    with pytest.raises(SnapshotStoreError):
        store_for(fake).delete_plain(key)
    assert fake.objects[f"{BUCKET}/{key}"] == content
    assert not [r for r in fake.requests if r.method == "DELETE"]


def test_store_compressed_refuses_bytes_that_do_not_match_the_key() -> None:
    fake = FakeStorage()
    with pytest.raises(SnapshotIntegrityError):
        store_for(fake).store_compressed(_key(b"original"), b"edited")
    assert fake.objects == {}


@pytest.mark.parametrize("key", ["../etc/passwd", "sha256/ab/xyz", "sha256/00/" + "a" * 64])
def test_maintenance_calls_refuse_malformed_keys(key: str) -> None:
    store = store_for(FakeStorage())
    for call in (store.read_plain, store.delete_plain, store.discard_damaged_compressed):
        with pytest.raises(ValueError, match="snapshot key"):
            call(key)


def test_a_damaged_compressed_copy_is_discarded_only_when_the_plain_copy_is_good() -> None:
    fake = FakeStorage()
    store = store_for(fake)
    content = b"original"
    key = _key(content)
    fake.objects[f"{BUCKET}/{key}{COMPRESSED_SUFFIX}"] = compress_copy(b"edited")
    fake.objects[f"{BUCKET}/{key}"] = b"also edited"
    assert store.discard_damaged_compressed(key) is False  # no good copy to fall back on: keep both
    assert len(fake.objects) == 2
    fake.objects[f"{BUCKET}/{key}"] = content
    assert store.discard_damaged_compressed(key) is True
    assert list(fake.objects) == [f"{BUCKET}/{key}"]
    assert store.get(key) == content


def test_a_good_compressed_copy_is_never_discarded() -> None:
    fake = FakeStorage()
    store = store_for(fake)
    content = b"original"
    store.put(content)
    fake.objects[f"{BUCKET}/{_key(content)}"] = content
    assert store.discard_damaged_compressed(_key(content)) is False
    assert len(fake.objects) == 2


def test_listing_walks_every_folder_page_by_page() -> None:
    fake = FakeStorage()
    store = store_for(fake)
    stored = [bytes([n]) * (n + 1) for n in range(9)]
    for content in stored:
        store.put(content)
    fake.objects[f"{BUCKET}/{_key(b'old')}"] = b"old"
    found = {o.name: o.size_bytes for o in store.list_objects(page_size=2)}
    expected = {f"{_key(c)}{COMPRESSED_SUFFIX}": len(compress_copy(c)) for c in stored}
    assert found == expected | {_key(b"old"): 3}
    assert set(fake.list_limits) == {2}


def test_a_listing_failure_is_a_store_error() -> None:
    store = store_for(FakeStorage(fail_with=503))
    with pytest.raises(SnapshotStoreError, match="503"):
        list(store.list_objects())
