"""The extractor-readings migration adds why no model read a document; the downgrade undoes it (ADR 0038)."""

from collections.abc import Iterator, Mapping
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from conftest import alembic_config, truncate_all

REVISION = "b7e2c4d9a1f3"
BEFORE = "5e7a0c3b9d21"

_SOURCE = (
    "INSERT INTO source_document (id, source_key, url, publisher, source_tier, document_type, published_at, "
    "retrieved_at, sha256, snapshot_key, content_type) VALUES (:source, 'k', 'https://www.sec.gov/x', "
    "'SEC EDGAR', 'regulator_or_sec', '8-K EX-99.1', now(), now(), repeat('a', 64), "
    "'sha256/aa/' || repeat('a', 64), 'application/pdf')"
)
_READING = (
    "INSERT INTO document_reading (id, source_document_id, agent, prompt_id, prompt_version, model, "
    "catalysts, read_at{column}) VALUES (:{name}, :source, 'extractor', 'extract_catalysts', :version, "
    "'m', 0, now(){value})"
)


@pytest.fixture
def config(engine: Engine, database_url: str) -> Iterator[Config]:
    """Alembic config for the test database; leaves it empty and at the latest revision."""
    migrations = alembic_config(database_url)
    yield migrations
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE source_document CASCADE"))
    command.upgrade(migrations, "head")
    truncate_all(engine)


def _column(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'document_reading' AND column_name = 'skipped'"
            )
        ).scalar_one_or_none()


def _insert(engine: Engine, statement: str, ids: Mapping[str, object]) -> None:
    with engine.begin() as connection:
        connection.execute(text(statement), ids)


def _readings(engine: Engine) -> set[UUID]:
    with engine.connect() as connection:
        return set(connection.execute(text("SELECT id FROM document_reading")).scalars())


def test_upgrade_adds_an_optional_skip_reason_and_downgrade_removes_it(
    config: Config, engine: Engine
) -> None:
    command.upgrade(config, REVISION)
    assert _column(engine) == "YES"
    command.downgrade(config, BEFORE)
    assert _column(engine) is None
    command.upgrade(config, REVISION)
    assert _column(engine) == "YES"


def test_only_known_skip_reasons_are_stored(config: Config, engine: Engine) -> None:
    command.upgrade(config, REVISION)
    ids = {"source": uuid4(), "bad": uuid4(), "version": "1.0.0"}
    _insert(engine, _SOURCE, ids)
    bad = _READING.format(column=", skipped", name="bad", value=", 'tired'")
    with pytest.raises(IntegrityError, match="skipped_allowed"):
        _insert(engine, bad, ids)


def test_downgrade_keeps_model_readings_and_drops_skips(config: Config, engine: Engine) -> None:
    """Without the column a skip would look like a model's reading; the next run checks it again for free."""
    command.upgrade(config, REVISION)
    ids = {"source": uuid4(), "read": uuid4(), "skip": uuid4(), "version": "1.0.0"}
    with engine.begin() as connection:
        connection.execute(text(_SOURCE), ids)
        connection.execute(text(_READING.format(column="", name="read", value="")), ids)
        skip = _READING.format(column=", skipped", name="skip", value=", 'not_text'")
        connection.execute(text(skip), ids | {"version": "1.0.1"})
    command.downgrade(config, BEFORE)
    assert _readings(engine) == {ids["read"]}
