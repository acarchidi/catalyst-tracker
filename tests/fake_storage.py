"""An in-memory fake of the Supabase Storage API, for tests of the snapshot store.

The fake follows supabase/storage's routes: objects at /object/{bucket}/{path}, reads at
/object/authenticated/{bucket}/{path}, listing at /object/list/{bucket}, deletes with DELETE
/object/{bucket}/{path}, buckets at /bucket. "Already exists" comes back as HTTP 400 or 409 with
statusCode "409" and error "Duplicate" in the body; "not found" as HTTP 400 with statusCode "404".
A listing returns one folder level: sub-folders with a null id and null metadata, files with
their size in `metadata.size`, sorted by name, `limit` entries from `offset`.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from catalyst.snapshots import SupabaseSnapshotStore

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
        # Called with each uploaded object's name and bytes; returns the bytes actually kept.
        self.on_upload: Callable[[str, bytes], bytes] = lambda _name, content: content
        # Object names whose reads answer HTTP 500.
        self.failing_reads: set[str] = set()
        self.list_limits: list[int] = []

    def _duplicate(self) -> httpx.Response:
        body = {"statusCode": "409", "error": "Duplicate", "message": "The resource already exists"}
        return httpx.Response(self.duplicate_status, json=body)

    @staticmethod
    def _not_found() -> httpx.Response:
        return httpx.Response(400, json={"statusCode": "404", "error": "not_found"})

    def _list(self, bucket: str, body: dict[str, object]) -> httpx.Response:
        prefix = str(body.get("prefix", ""))
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        limit, offset = int(str(body.get("limit", 100))), int(str(body.get("offset", 0)))
        self.list_limits.append(limit)
        entries: dict[str, dict[str, object]] = {}
        for name, content in self.objects.items():
            path = name.removeprefix(f"{bucket}/")
            if not name.startswith(f"{bucket}/") or not path.startswith(prefix):
                continue
            first, _, rest = path.removeprefix(prefix).partition("/")
            if rest:
                entries[first] = {"name": first, "id": None, "metadata": None}
            else:
                entries[first] = {
                    "name": first,
                    "id": f"id-{first}",
                    "metadata": {"size": len(content), "mimetype": "application/octet-stream"},
                }
        page = [entries[name] for name in sorted(entries)][offset : offset + limit]
        return httpx.Response(200, json=page)

    def __call__(self, request: httpx.Request) -> httpx.Response:  # noqa: C901 - one branch per route
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
        if request.method == "POST" and path.startswith("/object/list/"):
            return self._list(path.removeprefix("/object/list/"), json.loads(request.content))
        if request.method == "POST" and path.startswith("/object/"):
            assert request.headers["x-upsert"] == "false"
            name = path.removeprefix("/object/")
            if name in self.objects:
                return self._duplicate()
            self.objects[name] = self.on_upload(name, request.content)
            return httpx.Response(200, json={"Key": name})
        if request.method == "GET" and path.startswith("/object/authenticated/"):
            name = path.removeprefix("/object/authenticated/")
            if name in self.failing_reads:
                return httpx.Response(500, json={"statusCode": "500", "error": "internal"})
            if name not in self.objects:
                return self._not_found()
            return httpx.Response(200, content=self.objects[name])
        if request.method == "DELETE" and path.startswith("/object/"):
            name = path.removeprefix("/object/")
            if name not in self.objects:
                return self._not_found()
            del self.objects[name]
            return httpx.Response(200, json={"message": "Successfully deleted"})
        return httpx.Response(404)


def store_for(fake: FakeStorage) -> SupabaseSnapshotStore:
    """Return a Supabase store that talks only to `fake`."""
    return SupabaseSnapshotStore(URL, KEY, transport=httpx.MockTransport(fake))
