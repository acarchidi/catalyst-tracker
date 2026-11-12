"""The indication-attempt migration adds a closed table and its downgrade removes it (ADR 0053)."""

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from conftest import alembic_config, truncate_all

REVISION = "d051b393d828"
BEFORE = "3798b407da9a"
_CLOSED = (
    "SELECT c.relrowsecurity AND NOT EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid) "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relname = 'indication_attempt'"
)


@pytest.fixture
def config(engine: Engine, database_url: str) -> Iterator[Config]:
    """Alembic config for the test database; leaves it empty and at the latest revision."""
    migrations = alembic_config(database_url)
    yield migrations
    command.upgrade(migrations, "head")
    truncate_all(engine)


def _closed(engine: Engine) -> bool | None:
    """Whether indication_attempt has row-level security on and no policy, or None when absent."""
    with engine.connect() as connection:
        return connection.execute(text(_CLOSED)).scalar_one_or_none()


def test_upgrade_adds_the_table_closed_and_downgrade_removes_it(config: Config, engine: Engine) -> None:
    command.upgrade(config, REVISION)
    assert _closed(engine) is True  # only the server's own role reads or writes it
    command.downgrade(config, BEFORE)
    assert _closed(engine) is None
    command.upgrade(config, REVISION)
    assert _closed(engine) is True
