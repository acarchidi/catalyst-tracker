"""The check-versions migration adds two empty columns; its downgrade drops them and keeps outcomes."""

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from conftest import alembic_config, truncate_all

REVISION = "a64c0de5e7a1"
BEFORE = "d051b393d828"
AT = datetime(2026, 11, 21, 8, 0, tzinfo=UTC)


@pytest.fixture
def config(engine: Engine, database_url: str) -> Iterator[Config]:
    """Alembic config for the test database; leaves it empty and at the latest revision."""
    migrations = alembic_config(database_url)
    yield migrations
    command.upgrade(migrations, "head")
    truncate_all(engine)


def _columns(engine: Engine) -> set[str]:
    query = "SELECT column_name FROM information_schema.columns WHERE table_name = 'claim_check'"
    with engine.connect() as connection:
        return set(connection.execute(text(query)).scalars())


def _a_stored_outcome(engine: Engine) -> str:
    """Store one claim with one check outcome, written as the code before this revision wrote it."""
    document, claim, check = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO source_document (id, source_key, url, publisher, source_tier, document_type, "
                "published_at, retrieved_at, sha256, snapshot_key, content_type) VALUES (:id, 'sec:x', "
                "'https://www.sec.gov/x.htm', 'SEC EDGAR', 'regulator_or_sec', '8-K EX-99.1', :at, :at, "
                ":sha, :key, 'text/html')"
            ),
            {"id": document, "at": AT, "sha": "a" * 64, "key": "sha256/aa/" + "a" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO claim (id, source_document_id, subject_type, field, value, quote, quote_start, "
                "quote_end, agent, prompt_id, prompt_version, provider, model, extracted_at) VALUES (:id, "
                ":document, 'catalyst', 'catalyst:1', '{}', 'q', 0, 1, 'extractor', 'extract_catalysts', "
                "'1.0.0', 'anthropic', 'm', :at)"
            ),
            {"id": claim, "document": document, "at": AT},
        )
        connection.execute(
            text(
                "INSERT INTO claim_check (id, claim_id, check_name, outcome, reason, checked_at) "
                "VALUES (:id, :claim, 'window_not_past', 'fail', 'the window has passed', :at)"
            ),
            {"id": check, "claim": claim, "at": AT},
        )
    return str(check)


def _outcome(engine: Engine, check: str) -> tuple[str, str]:
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT outcome, reason FROM claim_check WHERE id = :id"), {"id": check}
        ).one()
        return row.outcome, row.reason


def test_upgrade_adds_empty_columns_and_downgrade_removes_them_keeping_outcomes(
    config: Config, engine: Engine
) -> None:
    command.downgrade(config, BEFORE)
    truncate_all(engine)
    check = _a_stored_outcome(engine)
    command.upgrade(config, REVISION)
    assert {"rule_version", "input_digest"} <= _columns(engine)
    with engine.connect() as connection:
        empty = connection.execute(text("SELECT rule_version, input_digest FROM claim_check")).one()
    assert tuple(empty) == (None, None)  # checked again, once, on the next run
    command.downgrade(config, BEFORE)
    assert not {"rule_version", "input_digest"} & _columns(engine)
    assert _outcome(engine, check) == ("fail", "the window has passed")
    command.upgrade(config, REVISION)
    assert _outcome(engine, check) == ("fail", "the window has passed")
