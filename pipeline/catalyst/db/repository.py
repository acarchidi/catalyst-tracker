"""Writes to the database. Every write is idempotent, so any job is safe to retry."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalyst.db.tables import TABLES
from catalyst.schemas import SourceDocument

_SOURCE_DOCUMENT = TABLES[SourceDocument]


def save_source_document(session: Session, doc: SourceDocument) -> tuple[SourceDocument, bool]:
    """Store `doc` unless the same copy (same URL and hash) is already stored.

    Returns the stored record and whether this call created it. When the copy already
    exists, the stored record keeps its original id and retrieval time.
    """
    statement = (
        insert(_SOURCE_DOCUMENT)
        .values(**doc.model_dump())
        .on_conflict_do_nothing(constraint="uq_source_document_url_sha256")
        .returning(_SOURCE_DOCUMENT.c.id)
    )
    created = session.execute(statement).scalar_one_or_none() is not None
    row = (
        session.execute(
            select(_SOURCE_DOCUMENT).where(
                _SOURCE_DOCUMENT.c.url == doc.url, _SOURCE_DOCUMENT.c.sha256 == doc.sha256
            )
        )
        .mappings()
        .one()
    )
    return SourceDocument.model_validate(dict(row)), created
