"""NLM's MeSH descriptor file, stored with its hash like any source (ADR 0053, decision 2).

The file maps an indication's words to one MeSH disease (`catalyst.mesh`). It backs no published
value by itself: the indication's words come from a quote, and MeSH only names the disease.
One file a year, about 17 MB compressed; a run stores it only when its content is new.
"""

import hashlib
from collections.abc import Iterator
from datetime import date

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
    """Fetch the year's descriptor file once and yield it."""
    response = client.get(FILE_URL.format(year=year))
    yield to_document(year, response.content)


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
