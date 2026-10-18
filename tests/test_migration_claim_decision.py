"""The publish-decision migration adds a closed table and removes it cleanly (ADR 0018, part 5a)."""

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from conftest import alembic_config, truncate_all

REVISION = "2ae23d097b49"
BEFORE = "faa22a89d739"


@pytest.fixture
def config(engine: Engine, database_url: str) -> Iterator[Config]:
    """Alembic config for the test database; leaves it empty and at the latest revision."""
    migrations = alembic_config(database_url)
    yield migrations
    command.upgrade(migrations, "head")
    truncate_all(engine)


def _table(engine: Engine) -> tuple[bool, bool] | None:
    """Return whether claim_decision exists, as (row-level security on, any policy), or None."""
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT c.relrowsecurity, EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = 'claim_decision'"
            )
        ).one_or_none()
    return None if row is None else (row[0], row[1])


def _revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def test_upgrade_adds_the_table_closed_and_downgrade_removes_it(config: Config, engine: Engine) -> None:
    command.upgrade(config, REVISION)
    assert _table(engine) == (True, False)  # row-level security on, no policy: only the server's own role
    command.downgrade(config, BEFORE)
    assert _table(engine) is None
    assert _revision(engine) == BEFORE
    command.upgrade(config, REVISION)
    assert _table(engine) == (True, False)
    assert _revision(engine) == REVISION


def test_downgrade_drops_stored_decisions_which_the_job_can_rebuild(config: Config, engine: Engine) -> None:
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
                "INSERT INTO claim_decision (id, claim_id, decision, reasons, decided_at) "
                "SELECT gen_random_uuid(), id, 'review', '[\"check x has not run\"]', now() FROM claim"
            )
        )
    command.downgrade(config, BEFORE)
    assert _table(engine) is None
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM claim")).scalar_one() == 1  # claims stay
