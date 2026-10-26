"""The Supabase Storage snapshot store, against a fake of the Storage API (tests/fake_storage.py)."""

from __future__ import annotations

import gzip
import hashlib

import httpx
import pytest

from catalyst.snapshots import (
    SnapshotIntegrityError,
    SnapshotNotFoundError,
    SnapshotStoreError,
    SupabaseSnapshotStore,
    snapshot_key,
)
from fake_storage import BUCKET, KEY, URL, FakeStorage, store_for


def test_put_then_get_returns_the_exact_bytes() -> None:
    fake = FakeStorage()
    store = store_for(fake)
    content = b"%PDF-1.7 a 10-Q"
    snapshot = store.put(content)
    assert snapshot.sha256 == hashlib.sha256(content).hexdigest()
    assert snapshot.key == snapshot_key(snapshot.sha256)
    # Stored compressed, under the original bytes' hash plus ".gz" (ADR 0046).
    assert list(fake.objects) == [f"{BUCKET}/{snapshot.key}.gz"]
    assert gzip.decompress(fake.objects[f"{BUCKET}/{snapshot.key}.gz"]) == content
    assert store.get(snapshot.key) == content


@pytest.mark.parametrize("status", [400, 409])
def test_storing_the_same_bytes_again_is_harmless(status: int) -> None:
    fake = FakeStorage(duplicate_status=status)
    store = store_for(fake)
    assert store.put(b"same") == store.put(b"same")
    assert len(fake.objects) == 1


def test_get_refuses_a_copy_that_changed() -> None:
    fake = FakeStorage()
    store = store_for(fake)
    snapshot = store.put(b"original")
    fake.objects[f"{BUCKET}/{snapshot.key}.gz"] = gzip.compress(b"edited")
    with pytest.raises(SnapshotIntegrityError):
        store.get(snapshot.key)


def test_get_refuses_a_plain_copy_that_changed() -> None:
    fake = FakeStorage()
    key = snapshot_key(hashlib.sha256(b"original").hexdigest())
    fake.objects[f"{BUCKET}/{key}"] = b"edited"
    with pytest.raises(SnapshotIntegrityError):
        store_for(fake).get(key)


def test_get_of_a_missing_copy_says_so() -> None:
    with pytest.raises(SnapshotNotFoundError):
        store_for(FakeStorage()).get(snapshot_key("a" * 64))


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_other_failures_raise_without_the_key(status: int) -> None:
    store = store_for(FakeStorage(fail_with=status))
    with pytest.raises(SnapshotStoreError) as raised:
        store.put(b"x")
    assert str(status) in str(raised.value)
    assert KEY not in str(raised.value)


def test_ensure_bucket_creates_a_private_bucket_once() -> None:
    fake = FakeStorage()
    store = store_for(fake)
    store.ensure_bucket()
    store.ensure_bucket()
    assert fake.buckets == {BUCKET: False}


def test_ensure_bucket_refuses_a_public_bucket() -> None:
    fake = FakeStorage()
    fake.buckets[BUCKET] = True
    with pytest.raises(SnapshotStoreError, match="public"):
        store_for(fake).ensure_bucket()


def test_requests_go_only_to_the_configured_project() -> None:
    fake = FakeStorage()
    store_for(fake).put(b"x")
    assert {r.url.host for r in fake.requests} == {"example.supabase.co"}
    assert all(r.url.scheme == "https" for r in fake.requests)


def test_a_plain_http_project_address_is_refused() -> None:
    with pytest.raises(ValueError, match="https"):
        SupabaseSnapshotStore("http://example.supabase.co", KEY)


def test_an_unreadable_bucket_answer_is_a_store_error() -> None:
    def not_json(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"name": BUCKET})
        return httpx.Response(200, content=b"<html>proxy error</html>")

    store = SupabaseSnapshotStore(URL, KEY, transport=httpx.MockTransport(not_json))
    with pytest.raises(SnapshotStoreError, match="unreadable"):
        store.ensure_bucket()
