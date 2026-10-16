"""The Supabase Storage snapshot store, against a fake of the Storage API.

The fake follows supabase/storage's routes: objects at /object/{bucket}/{path}, reads at
/object/authenticated/{bucket}/{path}, buckets at /bucket. "Already exists" comes back as
HTTP 400 or 409 with statusCode "409" and error "Duplicate" in the body.
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from catalyst.snapshots import (
    SnapshotIntegrityError,
    SnapshotNotFoundError,
    SnapshotStoreError,
    SupabaseSnapshotStore,
    snapshot_key,
)

URL = "https://example.supabase.co"
KEY = "sb_secret_test-only-not-a-real-key"
BUCKET = "source-snapshots"


class FakeStorage:
    """In-memory Storage API that checks every request carries the key the documented way."""

    def __init__(self, *, duplicate_status: int = 400, fail_with: int | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.buckets: dict[str, bool] = {}
        self.duplicate_status = duplicate_status
        self.fail_with = fail_with
        self.requests: list[httpx.Request] = []

    def _duplicate(self) -> httpx.Response:
        body = {"statusCode": "409", "error": "Duplicate", "message": "The resource already exists"}
        return httpx.Response(self.duplicate_status, json=body)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["apikey"] == KEY
        assert "authorization" not in request.headers  # new secret keys are not JWTs
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"statusCode": str(self.fail_with), "error": "boom"})
        path = request.url.path.removeprefix("/storage/v1")
        if request.method == "POST" and path == "/bucket":
            body = json.loads(request.content)
            if body["id"] in self.buckets:
                return self._duplicate()
            self.buckets[body["id"]] = body["public"]
            return httpx.Response(200, json={"name": body["id"]})
        if request.method == "GET" and path.startswith("/bucket/"):
            name = path.removeprefix("/bucket/")
            if name not in self.buckets:
                return httpx.Response(400, json={"statusCode": "404", "error": "Bucket not found"})
            return httpx.Response(200, json={"id": name, "name": name, "public": self.buckets[name]})
        if request.method == "POST" and path.startswith("/object/"):
            assert request.headers["x-upsert"] == "false"
            name = path.removeprefix("/object/")
            if name in self.objects:
                return self._duplicate()
            self.objects[name] = request.content
            return httpx.Response(200, json={"Key": name})
        if request.method == "GET" and path.startswith("/object/authenticated/"):
            name = path.removeprefix("/object/authenticated/")
            if name not in self.objects:
                return httpx.Response(400, json={"statusCode": "404", "error": "not_found"})
            return httpx.Response(200, content=self.objects[name])
        return httpx.Response(404)


def store_for(fake: FakeStorage) -> SupabaseSnapshotStore:
    return SupabaseSnapshotStore(URL, KEY, transport=httpx.MockTransport(fake))


def test_put_then_get_returns_the_exact_bytes() -> None:
    fake = FakeStorage()
    store = store_for(fake)
    content = b"%PDF-1.7 a 10-Q"
    snapshot = store.put(content)
    assert snapshot.sha256 == hashlib.sha256(content).hexdigest()
    assert snapshot.key == snapshot_key(snapshot.sha256)
    assert fake.objects == {f"{BUCKET}/{snapshot.key}": content}
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
    fake.objects[f"{BUCKET}/{snapshot.key}"] = b"edited"
    with pytest.raises(SnapshotIntegrityError):
        store.get(snapshot.key)


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
