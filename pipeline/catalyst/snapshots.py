"""Private copies of source documents, stored under their SHA-256 hash (spec: Provenance, item 5).

The walking skeleton stores copies on local disk. Supabase Storage replaces this in the
"Source snapshot store" roadmap item, behind the same `put`/`get` methods (ADR 0002).
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Snapshot:
    """A stored copy: its hash, its storage key, and its size."""

    sha256: str
    key: str
    size_bytes: int


class LocalSnapshotStore:
    """Content-addressed store on local disk. Storing the same bytes twice keeps one copy."""

    def __init__(self, root: Path) -> None:
        """Use `root` as the storage directory, creating it if needed."""
        self._root = root

    def put(self, content: bytes) -> Snapshot:
        """Store `content` and return its hash and key."""
        digest = hashlib.sha256(content).hexdigest()
        key = f"sha256/{digest[:2]}/{digest}"
        path = self._root / key
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".partial")
            temporary.write_bytes(content)
            temporary.replace(path)
        return Snapshot(sha256=digest, key=key, size_bytes=len(content))

    def get(self, key: str) -> bytes:
        """Return the stored bytes for `key`."""
        return (self._root / key).read_bytes()
