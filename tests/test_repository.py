"""Writing a source document is idempotent: re-running a collector creates no duplicates."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.repository import save_source_document
from catalyst.db.tables import TABLES
from catalyst.schemas import SourceDocument, SourceTier


def _doc(sha: str = "b" * 64) -> SourceDocument:
    return SourceDocument(
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


def _count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(TABLES[SourceDocument])).scalar_one()


def test_first_save_creates_a_row(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        saved, created = save_source_document(session, _doc())
        assert created
        assert _count(session) == 1
    assert saved.url.endswith("ex99.htm")


def test_saving_the_same_copy_twice_keeps_one_row_and_the_first_id(
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


def test_a_changed_copy_of_the_same_url_is_kept_as_a_new_row(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        save_source_document(session, _doc("b" * 64))
        _, created = save_source_document(session, _doc("c" * 64))
        assert created
        assert _count(session) == 2
