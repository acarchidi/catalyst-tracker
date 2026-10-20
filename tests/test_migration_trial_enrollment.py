"""The trial-size migration adds two empty columns and reverses cleanly (ADR 0038)."""

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from conftest import alembic_config, truncate_all

REVISION = "b3f1c7e2a9d4"
BEFORE = "5e7a0c3b9d21"


@pytest.fixture
def config(engine: Engine, database_url: str) -> Iterator[Config]:
    """Alembic config for the test database; leaves it empty and at the latest revision."""
    migrations = alembic_config(database_url)
    yield migrations
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE trial CASCADE"))
    command.upgrade(migrations, "head")
    truncate_all(engine)


def _columns(engine: Engine) -> dict[str, str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'trial' AND column_name LIKE 'enrollment%'"
            )
        )
        return {row[0]: row[1] for row in rows}


def test_upgrade_adds_empty_size_columns_and_downgrade_removes_them(config: Config, engine: Engine) -> None:
    command.downgrade(config, BEFORE)
    assert _columns(engine) == {}
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO trial (id, nct_id, phase) VALUES (gen_random_uuid(), 'NCT04929483', 'phase_3')")
        )
    command.upgrade(config, REVISION)
    assert _columns(engine) == {"enrollment": "YES", "enrollment_type": "YES"}
    with engine.connect() as connection:
        stored = connection.execute(
            text("SELECT nct_id, phase, enrollment, enrollment_type FROM trial")
        ).one()
    assert tuple(stored) == ("NCT04929483", "phase_3", None, None)  # filled by the next registry run

    with engine.begin() as connection:
        connection.execute(text("UPDATE trial SET enrollment = 99, enrollment_type = 'actual'"))
    command.downgrade(config, BEFORE)  # the sizes are a copy of the registry; the next run refills them
    assert _columns(engine) == {}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT nct_id, phase FROM trial")).one() == ("NCT04929483", "phase_3")
    command.upgrade(config, REVISION)
    assert _columns(engine) == {"enrollment": "YES", "enrollment_type": "YES"}


@pytest.mark.parametrize(
    ("size", "kind", "constraint"),
    [(-1, None, "enrollment_not_negative"), (10, "guessed", "enrollment_type_allowed")],
)
def test_the_upgraded_table_refuses_bad_sizes(
    config: Config, engine: Engine, size: int, kind: str | None, constraint: str
) -> None:
    command.downgrade(config, BEFORE)
    command.upgrade(config, REVISION)
    insert = (
        "INSERT INTO trial (id, nct_id, enrollment, enrollment_type) "
        "VALUES (gen_random_uuid(), 'NCT04929483', :size, :kind)"
    )
    with pytest.raises(IntegrityError, match=constraint), engine.begin() as connection:
        connection.execute(text(insert), {"size": size, "kind": kind})
