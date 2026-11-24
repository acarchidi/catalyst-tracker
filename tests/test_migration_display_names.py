"""The display-name migration adds three empty columns to `company`; its downgrade drops them (ADR 0068)."""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from conftest import alembic_config, truncate_all

REVISION = "c3d5e7f9a1b2"
BEFORE = "a64c0de5e7a1"
COLUMNS = {"display_name", "display_name_document_id", "display_name_inputs"}


@pytest.fixture
def config(engine: Engine, database_url: str) -> Iterator[Config]:
    """Alembic config for the test database; leaves it empty and at the latest revision."""
    migrations = alembic_config(database_url)
    yield migrations
    command.upgrade(migrations, "head")
    truncate_all(engine)


def _columns(engine: Engine) -> set[str]:
    query = "SELECT column_name FROM information_schema.columns WHERE table_name = 'company'"
    with engine.connect() as connection:
        return set(connection.execute(text(query)).scalars())


def _name(engine: Engine, company: str) -> str:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT name FROM company WHERE id = :id"), {"id": company}
        ).scalar_one()


def test_upgrade_adds_empty_columns_and_downgrade_removes_them_keeping_companies(
    config: Config, engine: Engine
) -> None:
    command.downgrade(config, BEFORE)
    truncate_all(engine)
    company = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO company (id, cik, name) VALUES (:id, 1125345, 'MACROGENICS INC')"),
            {"id": company},
        )
    command.upgrade(config, REVISION)
    assert _columns(engine) >= COLUMNS
    with engine.connect() as connection:
        empty = connection.execute(
            text("SELECT display_name, display_name_document_id, display_name_inputs FROM company")
        ).one()
    assert tuple(empty) == (None, None, None)  # computed on the next run
    command.downgrade(config, BEFORE)
    assert not COLUMNS & _columns(engine)
    assert _name(engine, company) == "MACROGENICS INC"
    command.upgrade(config, REVISION)
    assert _name(engine, company) == "MACROGENICS INC"
