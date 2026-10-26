"""Private copies of source documents, stored under their SHA-256 hash (spec: Provenance, item 5).

Every copy is stored at `sha256/<first two hex digits>/<hash>`, so the key itself states what
the bytes must hash to. Every read re-hashes the bytes and refuses a copy that no longer
matches. Two stores share the same `put` and `get`: local disk for development and tests,
and a private Supabase Storage bucket for the live system (ADR 0008).

The Supabase store keeps new copies gzip-compressed at `<key>.gz`; the key is still the hash of
the original bytes, and reads inflate under a size cap before checking it (ADR 0046).
"""

from __future__ import annotations

import gzip
import hashlib
import re
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.schemas import SourceDocument

KEY_PATTERN = re.compile(r"^sha256/([0-9a-f]{2})/([0-9a-f]{64})$")
DEFAULT_BUCKET = "source-snapshots"
TIMEOUT_SECONDS = 60
COMPRESSED_SUFFIX = ".gz"
# No source copy comes near this (Storage's free plan refuses uploads over 50 MB); a compressed
# copy that inflates past it is refused as damaged, so a crafted object cannot exhaust memory.
# Bytes over it are stored plain, so they stay readable.
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
LIST_PAGE_SIZE = 100


class SnapshotStoreError(RuntimeError):
    """The store could not complete a request; the message never includes the secret key."""


class SnapshotNotFoundError(SnapshotStoreError):
    """No copy is stored under the requested key."""


class SnapshotIntegrityError(SnapshotStoreError):
    """A stored copy's bytes no longer hash to the value its key names."""


@dataclass(frozen=True)
class Snapshot:
    """A stored copy: its hash, its storage key, and its size."""

    sha256: str
    key: str
    size_bytes: int


class SnapshotStore(Protocol):
    """Content-addressed storage for source copies."""

    def put(self, content: bytes) -> Snapshot:
        """Store `content` (once) and return its hash and key."""
        ...

    def get(self, key: str) -> bytes:
        """Return the bytes stored under `key`, after checking they still match their hash."""
        ...


def snapshot_key(sha256: str) -> str:
    """Return the storage key for a SHA-256 hex digest."""
    return f"sha256/{sha256[:2]}/{sha256}"


def _expected_hash(key: str) -> str:
    match = KEY_PATTERN.match(key)
    if not match or match.group(1) != match.group(2)[:2]:
        raise ValueError(f"not a snapshot key: {key!r}")
    return match.group(2)


def _describe(content: bytes) -> Snapshot:
    digest = hashlib.sha256(content).hexdigest()
    return Snapshot(sha256=digest, key=snapshot_key(digest), size_bytes=len(content))


def _checked(key: str, content: bytes) -> bytes:
    if hashlib.sha256(content).hexdigest() != _expected_hash(key):
        raise SnapshotIntegrityError(f"the copy at {key} no longer matches its hash")
    return content


def compress_copy(content: bytes) -> bytes:
    """Return `content` as one gzip member; the same bytes always give the same result."""
    return gzip.compress(content, compresslevel=9, mtime=0)


def decompress_copy(key: str, data: bytes, limit: int = MAX_SNAPSHOT_BYTES) -> bytes:
    """Inflate the compressed copy of `key`, refusing one that is malformed or inflates past `limit`.

    The hash is not checked here; `get` checks it on the inflated bytes.
    """
    inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)  # gzip framing only
    try:
        content = inflater.decompress(data, limit + 1)
    except zlib.error as error:
        raise SnapshotIntegrityError(f"the compressed copy at {key} cannot be read") from error
    if len(content) > limit:
        raise SnapshotIntegrityError(f"the compressed copy at {key} inflates to larger than {limit} bytes")
    if not inflater.eof or inflater.unused_data:
        raise SnapshotIntegrityError(f"the compressed copy at {key} is cut short or has extra bytes")
    return content


@dataclass(frozen=True)
class StoredObject:
    """One object in the bucket, as Storage lists it: its full name and its stored size."""

    name: str
    size_bytes: int


class LocalSnapshotStore:
    """Content-addressed store on local disk. Storing the same bytes twice keeps one copy."""

    def __init__(self, root: Path) -> None:
        """Use `root` as the storage directory, creating it if needed."""
        self._root = root

    def put(self, content: bytes) -> Snapshot:
        """Store `content` and return its hash and key."""
        snapshot = _describe(content)
        path = self._root / snapshot.key
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".partial")
            temporary.write_bytes(content)
            temporary.replace(path)
        return snapshot

    def get(self, key: str) -> bytes:
        """Return the stored bytes for `key`, refusing a copy that changed."""
        _expected_hash(key)
        path = self._root / key
        if not path.is_file():
            raise SnapshotNotFoundError(f"no copy at {key}")
        return _checked(key, path.read_bytes())


class SupabaseSnapshotStore:
    """Content-addressed store in a private Supabase Storage bucket.

    Requests carry the secret key in the `apikey` header only: Supabase's new secret keys
    are not JWTs and are refused as a Bearer token.
    """

    def __init__(
        self,
        project_url: str,
        secret_key: str,
        *,
        bucket: str = DEFAULT_BUCKET,
        transport: httpx.BaseTransport | None = None,
        max_snapshot_bytes: int = MAX_SNAPSHOT_BYTES,
    ) -> None:
        """Connect to the project at `project_url` (https only) with its secret key."""
        if urlsplit(project_url).scheme != "https":
            raise ValueError("SUPABASE_URL must start with https://")
        self._bucket = bucket
        self.max_snapshot_bytes = max_snapshot_bytes
        self._client = httpx.Client(
            base_url=project_url.rstrip("/") + "/storage/v1",
            headers={"apikey": secret_key},
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,
            transport=transport,
        )

    def ensure_bucket(self) -> None:
        """Create the private bucket if it does not exist; refuse to use a public one."""
        response = self._client.post(
            "/bucket", json={"id": self._bucket, "name": self._bucket, "public": False}
        )
        if response.status_code != 200 and not _is_duplicate(response):
            raise _failure("creating the snapshot bucket", response)
        response = self._client.get(f"/bucket/{self._bucket}")
        if response.status_code != 200:
            raise _failure("reading the snapshot bucket", response)
        try:
            public: object = response.json().get("public")
        except (ValueError, AttributeError) as error:
            raise SnapshotStoreError(
                "Supabase Storage answered the bucket check with unreadable data"
            ) from error
        if public is not False:
            raise SnapshotStoreError(f"the {self._bucket} bucket is public; source copies must stay private")

    def put(self, content: bytes) -> Snapshot:
        """Store `content` once, compressed; storing bytes that are already there is not an error.

        Bytes over the size cap are stored plain, so reading them never trips the cap.
        """
        snapshot = _describe(content)
        if len(content) > self.max_snapshot_bytes:
            self._upload(snapshot.key, content, "application/octet-stream")
        else:
            self._upload(snapshot.key + COMPRESSED_SUFFIX, compress_copy(content), "application/gzip")
        return snapshot

    def get(self, key: str) -> bytes:
        """Return the original bytes for `key`, refusing a copy that changed.

        The compressed copy is read first; only when there is none is the plain copy read. A
        compressed copy that fails its check is an error, never a reason to read the plain one.
        """
        _expected_hash(key)
        data = self._download(key + COMPRESSED_SUFFIX)
        if data is not None:
            return _checked(key, decompress_copy(key, data, self.max_snapshot_bytes))
        data = self._download(key)
        if data is not None:
            return _checked(key, data)
        raise SnapshotNotFoundError(f"no copy at {key}")

    # Maintenance calls for `catalyst snapshots-compress` (ADR 0046). Each takes a snapshot key,
    # never a free-form object name, so nothing outside the copies can be touched.

    def read_plain(self, key: str) -> bytes | None:
        """Return the plain copy of `key` after checking its hash, or None when there is none."""
        _expected_hash(key)
        data = self._download(key)
        return None if data is None else _checked(key, data)

    def store_compressed(self, key: str, content: bytes) -> None:
        """Store the compressed copy of `content` under `key`; refuse bytes that do not match it."""
        _checked(key, content)
        self._upload(key + COMPRESSED_SUFFIX, compress_copy(content), "application/gzip")

    def _compressed_is_good(self, key: str) -> bool:
        data = self._download(key + COMPRESSED_SUFFIX)
        if data is None:
            raise SnapshotNotFoundError(f"no compressed copy at {key}")
        try:
            _checked(key, decompress_copy(key, data, self.max_snapshot_bytes))
        except SnapshotIntegrityError:
            return False
        return True

    def delete_plain(self, key: str) -> None:
        """Delete the plain copy of `key`, but only after reading back and checking the compressed one.

        Raises, deleting nothing, when the compressed copy is missing or does not inflate to bytes
        that match `key`. A plain copy that is already gone is not an error.
        """
        _expected_hash(key)
        if not self._compressed_is_good(key):
            raise SnapshotIntegrityError(f"the compressed copy at {key} does not match its hash")
        self._delete(key)

    def discard_damaged_compressed(self, key: str) -> bool:
        """Delete a compressed copy that fails its check, when the plain copy is there and good.

        Returns whether it deleted it. A good compressed copy, or one with no good plain copy to
        fall back on, is kept: this only ever removes bytes that `get` would refuse.
        """
        _expected_hash(key)
        try:
            if self._compressed_is_good(key):
                return False
        except SnapshotNotFoundError:
            return False
        try:
            plain = self.read_plain(key)
        except SnapshotIntegrityError:
            return False
        if plain is None:
            return False
        self._delete(key + COMPRESSED_SUFFIX)
        return True

    def list_objects(self, prefix: str = "", page_size: int = LIST_PAGE_SIZE) -> Iterator[StoredObject]:
        """Yield every object under `prefix`, walking sub-folders, `page_size` entries per request."""
        offset = 0
        while True:
            body = {
                "prefix": prefix,
                "limit": page_size,
                "offset": offset,
                "sortBy": {"column": "name", "order": "asc"},
            }
            response = self._client.post(f"/object/list/{self._bucket}", json=body)
            if response.status_code != 200:
                raise _failure("listing copies", response)
            entries = _entries(response)
            for name, size in entries:
                path = f"{prefix}/{name}" if prefix else name
                if size is None:
                    yield from self.list_objects(path, page_size)
                else:
                    yield StoredObject(path, size)
            if len(entries) < page_size:
                return
            offset += page_size

    def _upload(self, name: str, data: bytes, content_type: str) -> None:
        response = self._client.post(
            f"/object/{self._bucket}/{name}",
            content=data,
            headers={"content-type": content_type, "x-upsert": "false"},
        )
        if response.status_code != 200 and not _is_duplicate(response):
            raise _failure("storing a copy", response)

    def _download(self, name: str) -> bytes | None:
        response = self._client.get(f"/object/authenticated/{self._bucket}/{name}")
        if response.status_code == 200:
            return response.content
        if _storage_status(response) == "404":
            return None
        raise _failure("reading a copy", response)

    def _delete(self, name: str) -> None:
        response = self._client.delete(f"/object/{self._bucket}/{name}")
        if response.status_code != 200 and _storage_status(response) != "404":
            raise _failure("deleting a copy", response)


def _entries(response: httpx.Response) -> list[tuple[str, int | None]]:
    """Read a listing page: each entry's name, and its size, or None for a sub-folder."""
    try:
        body: object = response.json()
    except ValueError as error:
        raise SnapshotStoreError("Supabase Storage answered a listing with unreadable data") from error
    if not isinstance(body, list):
        raise SnapshotStoreError("Supabase Storage answered a listing with unreadable data")
    entries: list[tuple[str, int | None]] = []
    for item in body:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(item, dict):
            raise SnapshotStoreError("Supabase Storage answered a listing with unreadable data")
        fields: dict[str, object] = item  # pyright: ignore[reportUnknownVariableType]
        name, metadata = fields.get("name"), fields.get("metadata")
        if not isinstance(name, str) or not name:
            raise SnapshotStoreError("Supabase Storage answered a listing with unreadable data")
        if fields.get("id") is None:
            entries.append((name, None))
            continue
        size: object = metadata.get("size") if isinstance(metadata, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if not isinstance(size, int):
            raise SnapshotStoreError("Supabase Storage listed a copy without its size")
        entries.append((name, size))
    return entries


def _storage_status(response: httpx.Response) -> str:
    """Return Storage's own status code from an error body (it often answers HTTP 400)."""
    try:
        body: object = response.json()
    except ValueError:
        return str(response.status_code)
    if not isinstance(body, dict):
        return str(response.status_code)
    fields: dict[str, object] = body  # pyright: ignore[reportUnknownVariableType]
    return str(fields.get("statusCode", response.status_code))


def _is_duplicate(response: httpx.Response) -> bool:
    return response.status_code in (400, 409) and _storage_status(response) == "409"


def _failure(action: str, response: httpx.Response) -> SnapshotStoreError:
    return SnapshotStoreError(
        f"Supabase Storage refused {action}: HTTP {response.status_code} (status {_storage_status(response)})"
    )


@dataclass
class VerifyReport:
    """What `verify_snapshots` found, by source key."""

    checked: int = 0
    missing: list[str] = field(default_factory=list[str])
    changed: list[str] = field(default_factory=list[str])
    unreadable: list[str] = field(default_factory=list[str])

    @property
    def ok(self) -> bool:
        """True when every recorded copy was read, exists, and matches its hash."""
        return not self.missing and not self.changed and not self.unreadable


def verify_snapshots(session_factory: sessionmaker[Session], store: SnapshotStore) -> VerifyReport:
    """Re-read every source document's copy and check it against the recorded hash.

    One copy that cannot be read (a storage or network error, a malformed row) is listed as
    unreadable and the audit continues, so one failure never hides the rest of the report.
    """
    table = TABLES[SourceDocument]
    with session_factory() as session:
        rows = session.execute(
            select(table.c.source_key, table.c.sha256, table.c.snapshot_key).order_by(table.c.source_key)
        ).all()
    report = VerifyReport()
    for source_key, sha256, key in rows:
        report.checked += 1
        if key != snapshot_key(sha256):
            report.changed.append(source_key)
            continue
        try:
            store.get(key)
        except SnapshotNotFoundError:
            report.missing.append(source_key)
        except SnapshotIntegrityError:
            report.changed.append(source_key)
        except (SnapshotStoreError, httpx.HTTPError, ValueError):
            report.unreadable.append(source_key)
    return report
