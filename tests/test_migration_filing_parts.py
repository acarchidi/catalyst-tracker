"""The filing-parts migration (ADR 0066): reversible, and it never forgets a deleted copy."""

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from conftest import alembic_config, truncate_all

REVISION = "1b7e8f9c6561"
BEFORE = "a64c0de5e7a1"
AT = datetime(2026, 11, 24, 2, tzinfo=UTC)


@pytest.fixture
def config(engine: Engine, database_url: str) -> Iterator[Config]:
    """Alembic config for the test database; leaves it empty and at the latest revision."""
    migrations = alembic_config(database_url)
    yield migrations
    command.upgrade(migrations, "head")
    truncate_all(engine)


def _empty(engine: Engine) -> None:
    """Empty the documents table, at whichever revision the database is."""
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE source_document CASCADE"))


def _columns(engine: Engine) -> set[str]:
    query = "SELECT column_name FROM information_schema.columns WHERE table_name = 'source_document'"
    with engine.connect() as connection:
        return set(connection.execute(text(query)).scalars())


def _tables(engine: Engine) -> set[str]:
    query = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    with engine.connect() as connection:
        return set(connection.execute(text(query)).scalars())


def _a_document(engine: Engine) -> str:
    """Store one 10-Q's row, as the code before this revision stored it."""
    document = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO source_document (id, source_key, url, publisher, source_tier, document_type, "
                "published_at, retrieved_at, sha256, snapshot_key, content_type) VALUES (:id, :key, "
                "'https://www.sec.gov/q.htm', 'SEC EDGAR', 'regulator_or_sec', '10-Q', :at, :at, :sha, "
                ":snapshot, 'text/html')"
            ),
            {
                "id": document,
                "key": f"sec:{document}",
                "at": AT,
                "sha": "a" * 64,
                "snapshot": "sha256/aa/" + "a" * 64,
            },
        )
    return document


def _mark(engine: Engine, document: str, *, read: datetime | None, deleted: datetime | None) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE source_document SET fully_read_at = :read, copy_deleted_at = :deleted WHERE id = :id"
            ),
            {"id": document, "read": read, "deleted": deleted},
        )


def test_upgrade_adds_the_part_table_and_two_empty_columns_and_downgrade_removes_them(
    config: Config, engine: Engine
) -> None:
    command.downgrade(config, BEFORE)
    _empty(engine)
    _a_document(engine)  # a document stored by the code before this revision
    assert "fully_read_at" not in _columns(engine)
    command.upgrade(config, REVISION)
    assert {"fully_read_at", "copy_deleted_at"} <= _columns(engine)
    assert "document_part_reading" in _tables(engine)
    with engine.connect() as connection:
        row = connection.execute(text("SELECT fully_read_at, copy_deleted_at FROM source_document")).one()
    assert tuple(row) == (None, None)  # nothing is read or deleted by the upgrade
    command.downgrade(config, BEFORE)
    assert "fully_read_at" not in _columns(engine)
    assert "document_part_reading" not in _tables(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM source_document")).scalar_one() == 1


def test_a_copy_cannot_be_marked_deleted_before_its_document_is_fully_read(
    config: Config, engine: Engine
) -> None:
    command.upgrade(config, REVISION)
    truncate_all(engine)
    document = _a_document(engine)
    with pytest.raises(IntegrityError):
        _mark(engine, document, read=None, deleted=AT)


def test_the_downgrade_refuses_while_a_copy_is_marked_deleted(config: Config, engine: Engine) -> None:
    command.upgrade(config, REVISION)
    truncate_all(engine)
    _mark(engine, _a_document(engine), read=AT, deleted=AT)
    with pytest.raises(RuntimeError, match="marked deleted"):
        command.downgrade(config, BEFORE)
    assert "copy_deleted_at" in _columns(engine)  # nothing was dropped
