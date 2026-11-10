"""NLM's MeSH descriptor file, stored with its hash like any source (ADR 0053, decision 2).

The file maps an indication's words to one MeSH disease (`catalyst.mesh`). It backs no published
value by itself: the indication's words come from a quote, and MeSH only names the disease.
One file a year, about 17 MB as NLM sends it; a run stores it only when its content is new.
NLM keeps only the current year's file and posts the next year's late in the year, so a run asks
for next year's file first and this year's after.

MeSH fits none of the spec's source tiers: it is a vocabulary, not a source of catalyst facts. It
is stored with tier REGISTRY (NLM's register of terms) only because every stored document needs
one, and no claim ever cites it.
"""

import hashlib
from collections.abc import Iterator
from datetime import date

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from catalyst.collectors.documents import FetchedDocument, new_york_midnight
from catalyst.collectors.http import PoliteHttpClient
from catalyst.db.tables import TABLES
from catalyst.mesh import MeshIndex
from catalyst.schemas import SourceDocument, SourceTier
from catalyst.snapshots import SnapshotStore

HOST = "nlmpubs.nlm.nih.gov"
FILE_URL = "https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh/desc{year}.gz"
PUBLISHER = "National Library of Medicine (MeSH)"
SOURCE = "mesh"
SOURCE_PREFIX = "mesh:"
REQUESTS_PER_SECOND = 1.0
MAX_BYTES = 64 * 1024 * 1024  # the 2026 file is 16.8 MB; the store's free plan takes up to 50 MB per copy
GZIP_MAGIC = b"\x1f\x8b"


def to_document(year: int, content: bytes) -> FetchedDocument:
    """Return the year's descriptor file as a document keyed by its year and a short hash."""
    digest = hashlib.sha256(content).hexdigest()[:12]
    return FetchedDocument(
        source_key=f"{SOURCE_PREFIX}desc{year}:{digest}",
        url=FILE_URL.format(year=year),
        publisher=PUBLISHER,
        source_tier=SourceTier.REGISTRY,
        document_type=f"MeSH descriptors {year}",
        published_at=new_york_midnight(date(year, 1, 1)),
        content=content,
        content_type="application/gzip",
    )


def collect_descriptors(client: PoliteHttpClient, year: int) -> Iterator[FetchedDocument]:
    """Fetch next year's descriptor file, else this year's, exactly as sent, and yield it.

    Only a gzip file counts: NLM answers a retired year with a redirect to an error page.
    """
    for candidate in (year + 1, year):
        try:
            content = client.get_as_sent(FILE_URL.format(year=candidate), MAX_BYTES)
        except httpx.HTTPStatusError:
            continue
        if content.startswith(GZIP_MAGIC):
            yield to_document(candidate, content)
            return
    raise ValueError(f"no MeSH descriptor file for {year + 1} or {year}")


def stored_index(session: Session, store: SnapshotStore) -> MeshIndex | None:
    """Build the index from the newest stored descriptor file; None when none is stored.

    The store checks the copy against its hash as it reads it, so a changed copy is never used.
    """
    table = TABLES[SourceDocument]
    row = (
        session.execute(
            select(table.c.snapshot_key)
            .where(table.c.source_key.startswith(SOURCE_PREFIX, autoescape=True))
            .order_by(table.c.published_at.desc(), table.c.retrieved_at.desc(), table.c.source_key.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    return None if row is None else MeshIndex.from_xml(store.get(row))
