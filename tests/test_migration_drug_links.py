"""The several-drugs migration never drops a stored drug link silently, in either direction (ADR 0016)."""

from collections.abc import Callable, Iterator
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, text

from conftest import alembic_config, truncate_all

BEFORE = "c14b85b5863c"

_INSERTS = {
    "company": "INSERT INTO company (id, cik, name) VALUES (:company, 1636282, 'Spyre')",
    "drug": "INSERT INTO drug (id, company_id, preferred_name) VALUES (:drug, :company, 'SPY001')",
    "combination": (
        "INSERT INTO drug (id, company_id, preferred_name) VALUES (:combination, :company, 'SPY120')"
    ),
    "catalyst": (
        "INSERT INTO catalyst (id, catalyst_type, company_id, title, window_start, window_end, "
        "window_precision, window_raw_text, date_confidence, status) VALUES (:catalyst, 'topline_readout', "
        ":company, 'SKYLINE Part B', '2027-01-01', '2027-12-31', 'year', '2027', 'company_confirmed', "
        "'upcoming')"
    ),
    "source": (
        "INSERT INTO source_document (id, source_key, url, publisher, source_tier, document_type, "
        "published_at, retrieved_at, sha256, snapshot_key, content_type) VALUES (:source, 'k', "
        "'https://www.sec.gov/x', 'SEC EDGAR', 'regulator_or_sec', '8-K EX-99.1', now(), now(), "
        "repeat('a', 64), 'sha256/aa/' || repeat('a', 64), 'text/html')"
    ),
    "claim": (
        "INSERT INTO claim (id, source_document_id, subject_type, subject_id, field, value, quote, "
        "quote_start, quote_end, agent, prompt_id, prompt_version, provider, model, extracted_at) "
        "VALUES (:claim, :source, 'catalyst', :catalyst, 'drugs', '\"SPY001\"', 'SPY001', 0, 6, "
        "'extractor', 'p', '1', 'anthropic', 'm', now())"
    ),
}


@pytest.fixture
def ids() -> dict[str, UUID]:
    """Fresh ids for one linked company, drugs, catalyst, source, and claim."""
    return {name: uuid4() for name in ("company", "drug", "combination", "catalyst", "source", "claim")}


@pytest.fixture
def config(engine: Engine, database_url: str) -> Iterator[Config]:
    """Alembic config for the test database; leaves it empty and at the latest revision."""
    migrations = alembic_config(database_url)
    yield migrations
    with engine.begin() as connection:  # works at either revision; change history allows TRUNCATE
        connection.execute(text("TRUNCATE company, source_document CASCADE"))
    command.upgrade(migrations, "head")
    truncate_all(engine)


def _store(connection: Connection, ids: dict[str, UUID], *names: str) -> None:
    for name in names:
        connection.execute(text(_INSERTS[name]), ids)


def _history_row(field: str) -> Callable[[Connection, dict[str, UUID]], None]:
    def insert(connection: Connection, ids: dict[str, UUID]) -> None:
        connection.execute(
            text(
                "INSERT INTO catalyst_change (id, catalyst_id, field, new_value, claim_id, learned_at, "
                "is_correction) VALUES (:id, :catalyst, :field, '1', :claim, now(), false)"
            ),
            {**ids, "id": uuid4(), "field": field},
        )

    return insert


def _old_drug_column(connection: Connection, ids: dict[str, UUID]) -> None:
    connection.execute(text("UPDATE catalyst SET drug_id = :drug"), ids)


def _drug_link(connection: Connection, ids: dict[str, UUID]) -> None:
    connection.execute(
        text(
            "INSERT INTO catalyst_drug (id, catalyst_id, drug_id, role, claim_id) "
            "VALUES (:id, :catalyst, :drug, 'alone', :claim)"
        ),
        {**ids, "id": uuid4()},
    )


def _component(connection: Connection, ids: dict[str, UUID]) -> None:
    connection.execute(
        text(
            "INSERT INTO drug_component (id, combination_id, component_id, claim_id) "
            "VALUES (:id, :combination, :drug, :claim)"
        ),
        {**ids, "id": uuid4()},
    )


@pytest.mark.parametrize(
    ("leftover", "message"),
    [(_old_drug_column, r"catalyst\.drug_id"), (_history_row("drug_id"), "'drug_id' rows")],
    ids=["catalyst names a drug", "history names drug_id"],
)
def test_upgrade_refuses_to_lose_a_stored_drug(
    config: Config,
    engine: Engine,
    ids: dict[str, UUID],
    leftover: Callable[[Connection, dict[str, UUID]], None],
    message: str,
) -> None:
    command.downgrade(config, BEFORE)
    with engine.begin() as connection:
        _store(connection, ids, "company", "drug", "catalyst", "source", "claim")
        leftover(connection, ids)
    with pytest.raises(RuntimeError, match=message):
        command.upgrade(config, "head")


@pytest.mark.parametrize(
    ("leftover", "message"),
    [
        (_drug_link, "catalyst_drug has links"),
        (_component, "drug_component has rows"),
        (_history_row("drugs"), "'drugs' rows"),
    ],
    ids=["drug link", "combination part", "history names drugs"],
)
def test_downgrade_refuses_to_lose_a_stored_drug(
    config: Config,
    engine: Engine,
    ids: dict[str, UUID],
    leftover: Callable[[Connection, dict[str, UUID]], None],
    message: str,
) -> None:
    with engine.begin() as connection:
        _store(connection, ids, "company", "drug", "combination", "catalyst", "source", "claim")
        leftover(connection, ids)
    with pytest.raises(RuntimeError, match=message):
        command.downgrade(config, BEFORE)
