"""Shared test fixtures. Database tests use a disposable Postgres, never the live database."""

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url
from sqlalchemy.orm import Session, sessionmaker

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _alembic_config(url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.attributes["database_url"] = url
    config.attributes["configure_logger"] = False
    return config


@pytest.fixture(scope="session")
def database_url() -> str:
    """Address of the disposable test database, from `CATALYST_TEST_DATABASE_URL`."""
    url = os.environ.get("CATALYST_TEST_DATABASE_URL")
    if not url:
        pytest.fail(
            "CATALYST_TEST_DATABASE_URL is not set. Start a disposable Postgres with "
            "scripts/local_db.sh and export the address it prints."
        )
    if not make_url(url).database or not str(make_url(url).database).endswith("_test"):
        pytest.fail("CATALYST_TEST_DATABASE_URL must name a database ending in _test; tests empty it.")
    return url


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    """Engine on a freshly migrated test database: downgrade to empty, then upgrade to the latest."""
    config = _alembic_config(database_url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    """Session factory; every table is emptied after each test."""
    yield sessionmaker(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("TRUNCATE source_document")


def sec_fixture_responses(cik: int) -> dict[str, tuple[bytes, str]]:
    """Map each recorded SEC URL to its saved body and content type."""
    folder = FIXTURES / "sec" / str(cik)
    index: dict[str, dict[str, str]] = json.loads((folder / "responses.json").read_text())
    return {
        url: ((folder / entry["file"]).read_bytes(), entry["content_type"]) for url, entry in index.items()
    }


@pytest.fixture
def vertex_sec() -> dict[str, tuple[bytes, str]]:
    """Return recorded EDGAR responses for Vertex Pharmaceuticals (CIK 875320), 2026-10-16."""
    return sec_fixture_responses(875320)
