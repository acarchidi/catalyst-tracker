"""Writes to the database. Every write is idempotent, so any job is safe to retry."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalyst.db.tables import TABLES
from catalyst.schemas import Company, CompanyDocument, DocumentMatch, SourceDocument

_SOURCE_DOCUMENT = TABLES[SourceDocument]
_COMPANY = TABLES[Company]
_COMPANY_DOCUMENT = TABLES[CompanyDocument]


def save_source_document(session: Session, doc: SourceDocument) -> tuple[SourceDocument, bool]:
    """Store `doc` unless a document with the same source key is already stored.

    Returns the stored record and whether this call created it. When the key already exists,
    the stored record keeps its original id, hash, and retrieval time.
    """
    statement = (
        insert(_SOURCE_DOCUMENT)
        .values(**doc.model_dump())
        .on_conflict_do_nothing(index_elements=["source_key"])
        .returning(_SOURCE_DOCUMENT.c.id)
    )
    created = session.execute(statement).scalar_one_or_none() is not None
    row = (
        session.execute(select(_SOURCE_DOCUMENT).where(_SOURCE_DOCUMENT.c.source_key == doc.source_key))
        .mappings()
        .one()
    )
    return SourceDocument.model_validate(dict(row)), created


def upsert_company(session: Session, cik: int, name: str, sic_code: str | None) -> Company:
    """Return the company with this SEC number, creating it if needed."""
    candidate = Company(cik=cik, name=name, sic_code=sic_code)
    session.execute(
        insert(_COMPANY).values(**candidate.model_dump()).on_conflict_do_nothing(index_elements=["cik"])
    )
    row = session.execute(select(_COMPANY).where(_COMPANY.c.cik == cik)).mappings().one()
    return Company.model_validate(dict(row))


def link_company_document(
    session: Session, company_id: UUID, document_id: UUID, matched_by: DocumentMatch
) -> bool:
    """Link a document to a company; return whether the link is new."""
    link = CompanyDocument(company_id=company_id, source_document_id=document_id, matched_by=matched_by)
    statement = (
        insert(_COMPANY_DOCUMENT)
        .values(**link.model_dump())
        .on_conflict_do_nothing(index_elements=["company_id", "source_document_id"])
        .returning(_COMPANY_DOCUMENT.c.id)
    )
    return session.execute(statement).scalar_one_or_none() is not None


def known_source_keys(session: Session, prefix: str) -> set[str]:
    """Return every stored source key that starts with `prefix`."""
    query = select(_SOURCE_DOCUMENT.c.source_key).where(
        _SOURCE_DOCUMENT.c.source_key.startswith(prefix, autoescape=True)
    )
    return set(session.execute(query).scalars())
