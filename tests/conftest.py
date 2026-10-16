"""Shared test fixtures. Database tests use a disposable Postgres, never the live database."""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import metadata

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


def truncate_all(engine: Engine) -> None:
    """Empty every table in the test database."""
    names = ", ".join(table.name for table in metadata.sorted_tables)
    with engine.begin() as connection:
        connection.exec_driver_sql(f"TRUNCATE {names}")


@pytest.fixture
def session_factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    """Session factory; every table is emptied after each test."""
    yield sessionmaker(engine)
    truncate_all(engine)


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


class RecordedServer:
    """An HTTP transport that answers from recorded fixture sets; anything not recorded is a 404."""

    def __init__(self, *labels: str, overrides: dict[str, tuple[int, bytes]] | None = None) -> None:
        """Serve the fixture sets in tests/fixtures/<label>/ (later labels win on duplicate URLs)."""
        self.responses: dict[str, tuple[int, bytes, str]] = {}
        for label in labels:
            folder = FIXTURES / label
            index: dict[str, dict[str, Any]] = json.loads((folder / "responses.json").read_text())
            for url, entry in index.items():
                body = (folder / entry["file"]).read_bytes()
                self.responses[url] = (entry["status"], body, entry["content_type"])
        for url, (status, body) in (overrides or {}).items():
            self.responses[url] = (status, body, "text/plain")
        self.requested: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Answer one request."""
        url = str(request.url)
        self.requested.append(url)
        status, body, content_type = self.responses.get(url, (404, b"", "text/plain"))
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    @property
    def transport(self) -> httpx.MockTransport:
        """Return this server as an httpx transport."""
        return httpx.MockTransport(self)
