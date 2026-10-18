"""The trial-claim migration gives each trial link its claim and never drops one silently (ADR 0018, 5b)."""

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from conftest import alembic_config, truncate_all

REVISION = "5e7a0c3b9d21"
BEFORE = "2ae23d097b49"

_ROWS = (
    "INSERT INTO company (id, cik, name) VALUES (:company, 1, 'Acme Bio')",
    "INSERT INTO trial (id, nct_id) VALUES (:trial, 'NCT04929483')",
    "INSERT INTO catalyst (id, catalyst_type, company_id, title, window_start, window_end, window_precision, "
    "window_raw_text, date_confidence, status) VALUES (:catalyst, 'topline_readout', :company, 'RAINIER', "
    "'2027-07-01', '2027-12-31', 'half', '2H27', 'company_confirmed', 'upcoming')",
    "INSERT INTO source_document (id, source_key, url, publisher, source_tier, document_type, published_at, "
    "retrieved_at, sha256, snapshot_key, content_type) VALUES (:source, 'k', 'https://www.sec.gov/x', "
    "'SEC EDGAR', 'regulator_or_sec', '8-K EX-99.1', now(), now(), repeat('a', 64), "
    "'sha256/aa/' || repeat('a', 64), 'text/html')",
    "INSERT INTO claim (id, source_document_id, subject_type, field, value, quote, quote_start, quote_end, "
    "agent, prompt_id, prompt_version, provider, model, extracted_at) VALUES (:claim, :source, 'catalyst', "
    "'catalyst:x', '{}', 'RAINIER', 0, 7, 'extractor', 'p', '1', 'anthropic', 'm', now())",
)


@pytest.fixture
def config(engine: Engine, database_url: str) -> Iterator[Config]:
    """Alembic config for the test database; leaves it empty and at the latest revision."""
    migrations = alembic_config(database_url)
    yield migrations
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE company, source_document, trial CASCADE"))
    command.upgrade(migrations, "head")
    truncate_all(engine)


@pytest.fixture
def ids() -> dict[str, UUID]:
    return {name: uuid4() for name in ("company", "trial", "catalyst", "source", "claim", "link")}


def _column(engine: Engine) -> tuple[str, str] | None:
    """Return the claim column's (nullability, foreign table), or None when it does not exist."""
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT c.is_nullable, ccu.table_name FROM information_schema.columns c "
                "LEFT JOIN information_schema.key_column_usage k ON k.table_name = c.table_name "
                "AND k.column_name = c.column_name "
                "LEFT JOIN information_schema.constraint_column_usage ccu "
                "ON ccu.constraint_name = k.constraint_name "
                "WHERE c.table_name = 'catalyst_trial' AND c.column_name = 'claim_id'"
            )
        ).one_or_none()
    return None if row is None else (row[0], row[1])


def _store(engine: Engine, ids: dict[str, UUID], *, with_claim: bool) -> None:
    with engine.begin() as connection:
        for statement in _ROWS:
            connection.execute(text(statement), ids)
        with_link = (
            "INSERT INTO catalyst_trial (id, catalyst_id, trial_id, claim_id) "
            "VALUES (:link, :catalyst, :trial, :claim)"
        )
        without = "INSERT INTO catalyst_trial (id, catalyst_id, trial_id) VALUES (:link, :catalyst, :trial)"
        connection.execute(text(with_link if with_claim else without), ids)


def test_upgrade_adds_a_required_claim_and_downgrade_removes_it(config: Config, engine: Engine) -> None:
    command.upgrade(config, REVISION)
    assert _column(engine) == ("NO", "claim")
    command.downgrade(config, BEFORE)
    assert _column(engine) is None
    command.upgrade(config, REVISION)
    assert _column(engine) == ("NO", "claim")


def test_upgrade_refuses_trial_links_that_have_no_claim(
    config: Config, engine: Engine, ids: dict[str, UUID]
) -> None:
    command.downgrade(config, BEFORE)
    _store(engine, ids, with_claim=False)
    with pytest.raises(RuntimeError, match="trial links without a claim"):
        command.upgrade(config, REVISION)


def test_downgrade_refuses_to_drop_the_claim_of_a_stored_link(
    config: Config, engine: Engine, ids: dict[str, UUID]
) -> None:
    _store(engine, ids, with_claim=True)
    with pytest.raises(RuntimeError, match="would lose the claim"):
        command.downgrade(config, BEFORE)
