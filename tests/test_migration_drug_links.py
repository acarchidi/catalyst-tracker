"""The several-drugs migration never drops a stored drug link silently, in either direction (ADR 0016)."""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from conftest import alembic_config, truncate_all

BEFORE = "c14b85b5863c"
COMPANY, DRUG, CATALYST = uuid4(), uuid4(), uuid4()


@pytest.fixture
def config(engine: Engine, database_url: str) -> Iterator[Config]:
    """Alembic config for the test database; leaves it empty and at the latest revision."""
    migrations = alembic_config(database_url)
    yield migrations
    command.upgrade(migrations, "head")
    truncate_all(engine)


def _company_and_drug(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO company (id, cik, name) VALUES (:id, 1636282, 'Spyre')"), {"id": COMPANY}
        )
        connection.execute(
            text("INSERT INTO drug (id, company_id, preferred_name) VALUES (:id, :c, 'SPY001')"),
            {"id": DRUG, "c": COMPANY},
        )


_CATALYST_COLUMNS = (
    "id, catalyst_type, company_id, title, window_start, window_end, window_precision, "
    "window_raw_text, date_confidence, status"
)
_CATALYST_VALUES = (
    ":id, 'topline_readout', :c, 'SKYLINE Part B', '2027-01-01', '2027-12-31', 'year', "
    "'2027', 'company_confirmed', 'upcoming'"
)
_INSERT_CATALYST = text(f"INSERT INTO catalyst ({_CATALYST_COLUMNS}) VALUES ({_CATALYST_VALUES})")  # noqa: S608
_INSERT_CATALYST_WITH_DRUG = text(
    f"INSERT INTO catalyst ({_CATALYST_COLUMNS}, drug_id) VALUES ({_CATALYST_VALUES}, :d)"  # noqa: S608
)


def _catalyst(engine: Engine, drug_column: bool) -> None:
    with engine.begin() as connection:
        connection.execute(
            _INSERT_CATALYST_WITH_DRUG if drug_column else _INSERT_CATALYST,
            {"id": CATALYST, "c": COMPANY, "d": DRUG},
        )


def test_upgrade_refuses_while_a_catalyst_still_names_a_drug(config: Config, engine: Engine) -> None:
    command.downgrade(config, BEFORE)
    _company_and_drug(engine)
    _catalyst(engine, drug_column=True)
    with pytest.raises(RuntimeError, match=r"catalyst\.drug_id"):
        command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(text("UPDATE catalyst SET drug_id = NULL"))


def test_downgrade_refuses_while_drug_links_exist(config: Config, engine: Engine) -> None:
    _company_and_drug(engine)
    _catalyst(engine, drug_column=False)
    with engine.begin() as connection:
        source = uuid4()
        connection.execute(
            text(
                "INSERT INTO source_document (id, source_key, url, publisher, source_tier, document_type, "
                "published_at, retrieved_at, sha256, snapshot_key, content_type) VALUES (:id, 'k', "
                "'https://www.sec.gov/x', 'SEC EDGAR', 'regulator_or_sec', '8-K EX-99.1', now(), now(), "
                ":h, :s, 'text/html')"
            ),
            {"id": source, "h": "a" * 64, "s": "sha256/aa/" + "a" * 64},
        )
        claim = uuid4()
        connection.execute(
            text(
                "INSERT INTO claim (id, source_document_id, subject_type, field, value, quote, "
                "quote_start, quote_end, agent, prompt_id, prompt_version, provider, model, extracted_at) "
                "VALUES (:id, :s, 'catalyst', 'drugs', '\"SPY001\"', 'SPY001', 0, 6, 'extractor', 'p', "
                "'1', 'anthropic', 'm', now())"
            ),
            {"id": claim, "s": source},
        )
        connection.execute(
            text(
                "INSERT INTO catalyst_drug (id, catalyst_id, drug_id, role, claim_id) "
                "VALUES (:id, :cat, :d, 'alone', :claim)"
            ),
            {"id": uuid4(), "cat": CATALYST, "d": DRUG, "claim": claim},
        )
    with pytest.raises(RuntimeError, match="catalyst_drug has links"):
        command.downgrade(config, BEFORE)
