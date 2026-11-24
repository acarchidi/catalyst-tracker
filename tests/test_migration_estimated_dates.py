"""The estimated-dates migration adds two allowed values and never loses a record (ADR 0067)."""

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from conftest import alembic_config, truncate_all

REVISION = "e5d2a7c41b93"
BEFORE = "a64c0de5e7a1"
_COMPANY = (
    "INSERT INTO company (id, cik, name) VALUES ('00000000-0000-0000-0000-000000000001', 1, 'Example Bio')"
)
_CATALYST = (
    "INSERT INTO catalyst (id, catalyst_type, company_id, title, window_start, window_end, window_precision, "
    "window_raw_text, date_confidence, status) VALUES (gen_random_uuid(), :kind, "
    "'00000000-0000-0000-0000-000000000001', 'Primary completion: ARISE trial', '2027-03-01', '2027-03-31', "
    "'month', '2027-03', 'registry_estimate', :status)"
)


@pytest.fixture
def config(engine: Engine, database_url: str) -> Iterator[Config]:
    """Alembic config for the test database; leaves it empty and at the latest revision."""
    migrations = alembic_config(database_url)
    yield migrations
    truncate_all(engine)
    command.upgrade(migrations, "head")


def _insert(engine: Engine, kind: str, status: str) -> None:
    with engine.begin() as connection:
        connection.execute(text(_CATALYST), {"kind": kind, "status": status})


@pytest.mark.parametrize(
    ("kind", "status"), [("primary_completion", "upcoming"), ("topline_readout", "replaced")]
)
def test_the_values_are_refused_before_and_allowed_after(
    config: Config, engine: Engine, kind: str, status: str
) -> None:
    command.downgrade(config, BEFORE)
    with engine.begin() as connection:
        connection.execute(text(_COMPANY))
    with pytest.raises(IntegrityError):
        _insert(engine, kind, status)
    command.upgrade(config, REVISION)
    _insert(engine, kind, status)


def test_the_downgrade_refuses_to_lose_an_estimate(config: Config, engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text(_COMPANY))
    _insert(engine, "primary_completion", "upcoming")
    with pytest.raises(RuntimeError, match="would lose them"):
        command.downgrade(config, BEFORE)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM catalyst")).scalar_one() == 1
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM catalyst"))
    command.downgrade(config, BEFORE)
    command.upgrade(config, REVISION)
