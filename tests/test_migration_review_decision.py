"""The review-decision migration adds a closed table and never drops a person's decisions (ADR 0040)."""

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from conftest import alembic_config, truncate_all

REVISION = "b7d01b8b82cf"
BEFORE = "b3f1c7e2a9d4"
_CLOSED = (
    "SELECT c.relrowsecurity AND NOT EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid) "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relname = 'review_decision'"
)


@pytest.fixture
def config(engine: Engine, database_url: str) -> Iterator[Config]:
    """Alembic config for the test database; leaves it empty and at the latest revision."""
    migrations = alembic_config(database_url)
    yield migrations
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM review_decision"))  # so the downgrade below may run
    command.downgrade(migrations, BEFORE)
    command.upgrade(migrations, "head")
    truncate_all(engine)


def _closed(engine: Engine) -> bool | None:
    """Whether review_decision has row-level security on and no policy, or None when it is absent."""
    with engine.connect() as connection:
        return connection.execute(text(_CLOSED)).scalar_one_or_none()


def test_upgrade_adds_the_table_closed_and_downgrade_removes_it_when_empty(
    config: Config, engine: Engine
) -> None:
    command.upgrade(config, REVISION)
    assert _closed(engine) is True  # only the server's own role reads or writes it
    command.downgrade(config, BEFORE)
    assert _closed(engine) is None
    command.upgrade(config, REVISION)
    assert _closed(engine) is True


def test_downgrade_refuses_to_drop_a_persons_decisions(config: Config, engine: Engine) -> None:
    command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO source_document (id, source_key, url, publisher, source_tier, document_type, "
                "published_at, retrieved_at, sha256, snapshot_key, content_type) VALUES "
                "(gen_random_uuid(), 'k', 'https://www.sec.gov/x', 'SEC EDGAR', 'regulator_or_sec', "
                "'8-K EX-99.1', now(), now(), repeat('a', 64), 'sha256/aa/' || repeat('a', 64), 'text/html')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO claim (id, source_document_id, subject_type, field, value, quote, quote_start, "
                "quote_end, agent, prompt_id, prompt_version, provider, model, extracted_at) "
                "SELECT gen_random_uuid(), id, 'catalyst', 'catalyst:x', '{}', 'q', 0, 1, 'extractor', 'p', "
                "'1', 'anthropic', 'm', now() FROM source_document"
            )
        )
        connection.execute(
            text(
                "INSERT INTO review_decision (id, claim_id, action, reviewer, reasons_seen, decided_at) "
                "SELECT gen_random_uuid(), id, 'reject', 'acarchidi', '[\"check x has not run\"]', now() "
                "FROM claim"
            )
        )
    with pytest.raises(RuntimeError, match="nothing can rebuild"):
        command.downgrade(config, BEFORE)
    assert _closed(engine) is True
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM review_decision")).scalar_one() == 1
