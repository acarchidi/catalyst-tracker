"""The drug-name source migration keeps every stored name and can be undone (ADR 0035)."""

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from conftest import alembic_config, truncate_all

REVISION = "6ddac72f9906"
BEFORE = "5e7a0c3b9d21"


@pytest.fixture
def config(engine: Engine, database_url: str) -> Iterator[Config]:
    """Alembic config for the test database; leaves it empty and at the latest revision."""
    migrations = alembic_config(database_url)
    yield migrations
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE drug CASCADE"))
    command.upgrade(migrations, "head")
    truncate_all(engine)


def _columns(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = 'drug_name'")
        )
        return {row[0] for row in rows}


def _table_exists(engine: Engine) -> bool:
    with engine.connect() as connection:
        return connection.execute(text("SELECT to_regclass('withheld_drug_name')")).scalar_one() is not None


def _store_name(engine: Engine) -> UUID:
    drug, name = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO drug (id, preferred_name) VALUES (:id, 'SPY001')"), {"id": drug})
        connection.execute(
            text("INSERT INTO drug_name (id, drug_id, name, kind) VALUES (:id, :drug, 'SPY001', 'code')"),
            {"id": name, "drug": drug},
        )
    return name


def test_upgrade_marks_stored_names_as_the_claims_own_and_downgrade_removes_the_columns(
    config: Config, engine: Engine
) -> None:
    command.downgrade(config, BEFORE)
    assert not {"source", "source_refs"} & _columns(engine)
    assert not _table_exists(engine)
    name = _store_name(engine)
    command.upgrade(config, REVISION)
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT source, source_refs FROM drug_name WHERE id = :id"), {"id": name}
        ).one()
    assert (row.source, row.source_refs) == ("claim", [])
    assert _table_exists(engine)
    command.downgrade(config, BEFORE)
    assert not {"source", "source_refs"} & _columns(engine)
    assert not _table_exists(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM drug_name")).scalar_one() == 1


def test_the_upgraded_table_refuses_an_unknown_source_and_needs_one(config: Config, engine: Engine) -> None:
    command.upgrade(config, REVISION)
    drug = uuid4()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO drug (id, preferred_name) VALUES (:id, 'x')"), {"id": drug})
    statements = (
        "INSERT INTO drug_name (id, drug_id, name, kind, source_refs) VALUES (:id, :drug, 'x', 'inn', '[]')",
        "INSERT INTO drug_name (id, drug_id, name, kind, source, source_refs) "
        "VALUES (:id, :drug, 'x', 'inn', 'wiki', '[]')",
        "INSERT INTO drug_name (id, drug_id, name, kind, source, source_refs) "
        "VALUES (:id, :drug, 'x', 'inn', 'ctgov', '\"NCT01234567\"')",
    )
    for statement in statements:
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(statement), {"id": uuid4(), "drug": drug})
