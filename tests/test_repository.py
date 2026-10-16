"""Writes are idempotent: re-running a collector creates no duplicate documents, links, or companies."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.repository import link_company_document, save_source_document, upsert_company
from catalyst.db.tables import TABLES
from catalyst.schemas import CompanyDocument, DocumentMatch, SourceDocument, SourceTier
from catalyst.schemas.base import Entity


def _doc(sha: str = "b" * 64, key: str = "sec:0000000001-24-000001:ex99.htm") -> SourceDocument:
    return SourceDocument(
        source_key=key,
        url="https://www.sec.gov/Archives/edgar/data/1/000000000124000001/ex99.htm",
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type="8-K EX-99.1",
        published_at=datetime(2024, 10, 30, 20, 5, tzinfo=UTC),
        retrieved_at=datetime(2024, 10, 31, 12, 0, tzinfo=UTC),
        sha256=sha,
        snapshot_key=f"sha256/{sha[:2]}/{sha}",
        content_type="text/html",
    )


def _count(session: Session, model: type[Entity] = SourceDocument) -> int:
    return session.execute(select(func.count()).select_from(TABLES[model])).scalar_one()


def test_first_save_creates_a_row(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        saved, created = save_source_document(session, _doc())
        assert created
        assert _count(session) == 1
    assert saved.url.endswith("ex99.htm")


def test_saving_the_same_document_twice_keeps_one_row_and_the_first_id(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        first, _ = save_source_document(session, _doc())
    with session_factory.begin() as session:
        second, created = save_source_document(session, _doc())
        assert not created
        assert second.id == first.id
        assert second.retrieved_at == first.retrieved_at
        assert _count(session) == 1


def test_a_refetch_with_different_bytes_but_the_same_source_key_adds_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    # Some sources return slightly different bytes on every fetch; the source's own key decides identity.
    with session_factory.begin() as session:
        save_source_document(session, _doc("b" * 64))
        _, created = save_source_document(session, _doc("c" * 64))
        assert not created
        assert _count(session) == 1


def test_a_new_version_with_a_new_source_key_is_kept_as_a_new_row(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        save_source_document(session, _doc("b" * 64, key="ctgov:NCT04567890:2026-08-01"))
        _, created = save_source_document(session, _doc("c" * 64, key="ctgov:NCT04567890:2026-09-15"))
        assert created
        assert _count(session) == 2


def test_upsert_company_returns_the_same_company_for_the_same_cik(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        first = upsert_company(session, cik=875320, name="VERTEX PHARMACEUTICALS INC / MA", sic_code="2834")
        second = upsert_company(session, cik=875320, name="VERTEX PHARMACEUTICALS INC / MA", sic_code="2834")
        assert first.id == second.id


def test_company_document_links_are_idempotent(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        company = upsert_company(session, cik=875320, name="Vertex", sic_code=None)
        document, _ = save_source_document(session, _doc())
        assert link_company_document(session, company.id, document.id, DocumentMatch.SEC_CIK)
        assert not link_company_document(session, company.id, document.id, DocumentMatch.SEC_CIK)
        assert _count(session, CompanyDocument) == 1
