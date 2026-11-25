"""The duplicate-entries migration adds a status and a table; its downgrade never loses a merge (ADR 0081)."""

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from conftest import alembic_config, truncate_all

REVISION = "f2b9d4c6a8e1"
BEFORE = "e5d2a7c41b93"
_COMPANY = (
    "INSERT INTO company (id, cik, name) VALUES ('00000000-0000-0000-0000-000000000001', 1, 'Example Bio')"
)
_CATALYST = (
    "INSERT INTO catalyst (id, catalyst_type, company_id, title, window_start, window_end, window_precision, "
    "window_raw_text, date_confidence, status) VALUES (gen_random_uuid(), 'topline_readout', "
    "'00000000-0000-0000-0000-000000000001', 'Topline data: zentocel', '2027-07-01', '2027-12-31', "
    "'half', 'second half of 2027', 'company_confirmed', :status)"
)
_SECURED = (
    "SELECT c.relrowsecurity AND NOT EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid) "
    "FROM pg_class c WHERE c.relname = 'catalyst_merged_source'"
)


@pytest.fixture
def config(engine: Engine, database_url: str) -> Iterator[Config]:
    """Alembic config for the test database; leaves it empty and at the latest revision."""
    migrations = alembic_config(database_url)
    yield migrations
    truncate_all(engine)
    command.upgrade(migrations, "head")


def _insert(engine: Engine, status: str) -> None:
    with engine.begin() as connection:
        connection.execute(text(_CATALYST), {"status": status})


def test_merged_is_refused_before_and_allowed_after(config: Config, engine: Engine) -> None:
    command.downgrade(config, BEFORE)
    with engine.begin() as connection:
        connection.execute(text(_COMPANY))
    with pytest.raises(IntegrityError):
        _insert(engine, "merged")
    with pytest.raises(ProgrammingError), engine.connect() as connection:
        connection.execute(text("SELECT 1 FROM catalyst_merged_source"))
    command.upgrade(config, REVISION)
    _insert(engine, "merged")
    with engine.connect() as connection:
        assert connection.execute(text(_SECURED)).scalar_one() is True  # no row reachable through the API key


def test_the_downgrade_refuses_to_lose_a_merge(config: Config, engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text(_COMPANY))
    _insert(engine, "merged")
    with pytest.raises(RuntimeError, match="show both entries again"):
        command.downgrade(config, BEFORE)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM catalyst")).scalar_one() == 1
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM catalyst"))
    command.downgrade(config, BEFORE)  # nothing merged: it reverses cleanly, and upgrades again
    command.upgrade(config, REVISION)
