"""Private copies of source documents, stored under their SHA-256 hash (spec: Provenance, item 5).

Every copy is stored at `sha256/<first two hex digits>/<hash>`, so the key itself states what
the bytes must hash to. Every read re-hashes the bytes and refuses a copy that no longer
matches. Two stores share the same `put` and `get`: local disk for development and tests,
and a private Supabase Storage bucket for the live system (ADR 0008).
"""

from __future__ import annotations

import hashlib
import re
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


class SnapshotStoreError(RuntimeError):
    """The store could not complete a request; the message never includes the key."""


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
    ) -> None:
        """Connect to the project at `project_url` (https only) with its secret key."""
        if urlsplit(project_url).scheme != "https":
            raise ValueError("SUPABASE_URL must start with https://")
        self._bucket = bucket
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
        if response.json().get("public") is not False:
            raise SnapshotStoreError(f"the {self._bucket} bucket is public; source copies must stay private")

    def put(self, content: bytes) -> Snapshot:
        """Store `content` once; storing bytes that are already there is not an error."""
        snapshot = _describe(content)
        response = self._client.post(
            f"/object/{self._bucket}/{snapshot.key}",
            content=content,
            headers={"content-type": "application/octet-stream", "x-upsert": "false"},
        )
        if response.status_code != 200 and not _is_duplicate(response):
            raise _failure("storing a copy", response)
        return snapshot

    def get(self, key: str) -> bytes:
        """Return the stored bytes for `key`, refusing a copy that changed."""
        _expected_hash(key)
        response = self._client.get(f"/object/authenticated/{self._bucket}/{key}")
        if response.status_code == 200:
            return _checked(key, response.content)
        if _storage_status(response) == "404":
            raise SnapshotNotFoundError(f"no copy at {key}")
        raise _failure("reading a copy", response)


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
