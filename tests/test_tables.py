"""Generated tables match their models, and the database itself enforces the spec's rules.

The database checks matter because a bug in Python code, or a manual fix, must not be able to
store a value the models would reject.
"""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import Engine, String, Table, insert, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from catalyst.db.tables import TABLES, build_table
from catalyst.schemas import (
    ALL_MODELS,
    Catalyst,
    CatalystChange,
    CatalystDrug,
    CatalystField,
    CatalystStatus,
    CatalystType,
    Claim,
    ClaimSubject,
    Company,
    CompanyTicker,
    DateConfidence,
    DatePrecision,
    Drug,
    DrugComponent,
    DrugRole,
    Entity,
    Indication,
    SourceDocument,
    SourceTier,
)
from conftest import truncate_all

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def test_every_model_has_exactly_one_table() -> None:
    assert set(TABLES) == set(ALL_MODELS)
    assert len({table.name for table in TABLES.values()}) == len(ALL_MODELS)


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
def test_columns_match_fields(model: type[Entity]) -> None:
    table = TABLES[model]
    assert list(table.columns.keys()) == list(model.model_fields)
    schema = model.model_json_schema()
    required = set(schema.get("required", []))
    for name, field in model.model_fields.items():
        column = table.columns[name]
        assert column.comment == field.description
        may_be_empty = name not in required and field.default is None
        assert column.nullable == may_be_empty, f"{model.__name__}.{name}: nullable={column.nullable}"


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
def test_text_column_lengths_match_the_model_limits(model: type[Entity]) -> None:
    # Pydantic's own JSON schema is the independent source for each field's length limit.
    properties = model.model_json_schema()["properties"]
    for name, column in TABLES[model].columns.items():
        options = [properties[name], *properties[name].get("anyOf", [])]
        limits = {option["maxLength"] for option in options if "maxLength" in option}
        if limits:
            assert isinstance(column.type, String), f"{model.__name__}.{name} should be limited text"
            assert {column.type.length} == limits, f"{model.__name__}.{name}"


# Written from the spec's Data model, not from the generator.
EXPECTED_FOREIGN_KEYS = {
    ("company_ticker", "company_id", "company"),
    ("company_name", "company_id", "company"),
    ("drug", "company_id", "company"),
    ("drug_name", "drug_id", "drug"),
    ("drug_target", "drug_id", "drug"),
    ("drug_target", "target_id", "target"),
    ("catalyst", "company_id", "company"),
    ("catalyst", "indication_id", "indication"),
    ("catalyst_trial", "catalyst_id", "catalyst"),
    ("catalyst_trial", "trial_id", "trial"),
    ("claim", "source_document_id", "source_document"),
    ("catalyst_change", "catalyst_id", "catalyst"),
    ("catalyst_change", "claim_id", "claim"),
    ("outcome", "catalyst_id", "catalyst"),
    ("outcome", "resolution_claim_id", "claim"),
    ("company_document", "company_id", "company"),
    ("company_document", "source_document_id", "source_document"),
    ("catalyst_drug", "catalyst_id", "catalyst"),
    ("catalyst_drug", "drug_id", "drug"),
    ("catalyst_drug", "claim_id", "claim"),
    ("drug_component", "combination_id", "drug"),
    ("drug_component", "component_id", "drug"),
    ("drug_component", "claim_id", "claim"),
    ("entity_match", "claim_id", "claim"),
}


def test_foreign_keys_are_exactly_the_expected_links() -> None:
    found = {
        (table.name, fk.parent.name, fk.column.table.name)
        for table in TABLES.values()
        for fk in table.foreign_keys
    }
    assert found == EXPECTED_FOREIGN_KEYS


def test_unsupported_field_types_are_refused() -> None:
    class Odd(Entity):
        """A model with a type the generator does not know."""

        __tablename__ = "odd"
        amount: complex

    from sqlalchemy import MetaData

    with pytest.raises(TypeError, match="no column type"):
        build_table(Odd, MetaData())


# --- Database-level enforcement (needs the disposable test database) ---


def _insert(engine: Engine, model: BaseModel, table: Table) -> UUID:
    with engine.begin() as connection:
        connection.execute(insert(table).values(**model.model_dump()))
    return model.id  # type: ignore[attr-defined]


def _raw_insert(engine: Engine, table: Table, values: dict[str, Any]) -> None:
    with engine.begin() as connection:
        connection.execute(insert(table).values(id=uuid4(), **values))


@pytest.fixture
def chain(engine: Engine) -> Iterator[dict[str, UUID]]:
    """Store a linked company, drug, indication, source document, catalyst, and claim."""
    company = Company(cik=875320, name="Vertex Pharmaceuticals Inc", sic_code="2834")
    drug = Drug(company_id=company.id, preferred_name="povetacicept")
    indication = Indication(mesh_id="D005922", name="IgA nephropathy")
    source = SourceDocument(
        source_key="sec:0000875320-26-000256:ex-991_q22026.htm",
        url="https://www.sec.gov/Archives/edgar/data/875320/000087532026000256/ex-991_q22026.htm",
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type="8-K EX-99.1",
        published_at=datetime(2026, 8, 3, 20, 9, 43, tzinfo=UTC),
        retrieved_at=NOW,
        sha256="e" * 64,
        snapshot_key="sha256/ee/" + "e" * 64,
        content_type="text/html",
    )
    catalyst = Catalyst(
        catalyst_type=CatalystType.PDUFA_DATE,
        company_id=company.id,
        indication_id=indication.id,
        title="FDA decision on povetacicept",
        window_start=date(2026, 11, 30),
        window_end=date(2026, 11, 30),
        window_precision=DatePrecision.DAY,
        window_raw_text="PDUFA date November 30th",
        date_confidence=DateConfidence.COMPANY_CONFIRMED,
        status=CatalystStatus.UPCOMING,
    )
    claim = Claim(
        source_document_id=source.id,
        subject_type=ClaimSubject.CATALYST,
        subject_id=catalyst.id,
        field="window",
        value={"raw": "November 30th"},
        quote="povetacicept PDUFA date November 30th",
        quote_start=120,
        quote_end=157,
        agent="extractor",
        prompt_id="extract_catalysts",
        prompt_version="1.0.0",
        provider="anthropic",
        model="example-model",
        extracted_at=NOW,
    )
    for record in (company, drug, indication, source, catalyst, claim):
        _insert(engine, record, TABLES[type(record)])
    yield {"company": company.id, "catalyst": catalyst.id, "claim": claim.id, "drug": drug.id}
    truncate_all(engine)


def test_a_linked_chain_of_records_stores(chain: dict[str, UUID], engine: Engine) -> None:
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM catalyst WHERE company_id = :c"), {"c": chain["company"]}
        )
        assert count.scalar_one() == 1


def test_a_catalyst_for_an_unknown_company_is_refused(chain: dict[str, UUID], engine: Engine) -> None:
    del chain
    with pytest.raises(IntegrityError, match="fk_catalyst_company_id_company"):
        _raw_insert(
            engine,
            TABLES[Catalyst],
            {
                "catalyst_type": "pdufa_date",
                "company_id": uuid4(),
                "title": "x",
                "window_start": date(2026, 11, 30),
                "window_end": date(2026, 11, 30),
                "window_precision": "day",
                "window_raw_text": "x",
                "date_confidence": "inferred",
                "status": "upcoming",
            },
        )


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"catalyst_type": "rumor"}, "ck_catalyst_catalyst_type_allowed"),
        ({"window_start": date(2027, 1, 1), "window_precision": "year"}, "ck_catalyst_window_order"),
        ({"window_precision": "quarter"}, "ck_catalyst_day_is_single_day"),
    ],
)
def test_the_database_rejects_values_the_models_reject(
    chain: dict[str, UUID], engine: Engine, overrides: dict[str, Any], constraint: str
) -> None:
    values = {
        "catalyst_type": "pdufa_date",
        "company_id": chain["company"],
        "title": "x",
        "window_start": date(2026, 11, 30),
        "window_end": date(2026, 11, 30),
        "window_precision": "day",
        "window_raw_text": "x",
        "date_confidence": "inferred",
        "status": "upcoming",
    }
    with pytest.raises(IntegrityError, match=constraint):
        _raw_insert(engine, TABLES[Catalyst], {**values, **overrides})


def test_a_blank_quote_is_refused_by_the_database(chain: dict[str, UUID], engine: Engine) -> None:
    with engine.connect() as connection:
        source_id = connection.execute(text("SELECT id FROM source_document")).scalar_one()
    with pytest.raises(IntegrityError, match="ck_claim_quote_not_blank"):
        _raw_insert(
            engine,
            TABLES[Claim],
            {
                "source_document_id": source_id,
                "subject_type": "catalyst",
                "field": "window",
                "value": {},
                "quote": "   ",
                "quote_start": 0,
                "quote_end": 3,
                "agent": "extractor",
                "prompt_id": "p",
                "prompt_version": "1",
                "provider": "anthropic",
                "model": "m",
                "extracted_at": NOW,
            },
        )


def _change(chain: dict[str, UUID]) -> CatalystChange:
    return CatalystChange(
        catalyst_id=chain["catalyst"],
        field=CatalystField.WINDOW_END,
        old_value=None,
        new_value="2026-11-30",
        claim_id=chain["claim"],
        learned_at=NOW,
    )


def test_change_history_accepts_new_rows(chain: dict[str, UUID], engine: Engine) -> None:
    _insert(engine, _change(chain), TABLES[CatalystChange])


@pytest.mark.parametrize(
    "statement",
    ["UPDATE catalyst_change SET field = 'status'", "DELETE FROM catalyst_change"],
)
def test_change_history_cannot_be_edited_or_deleted(
    chain: dict[str, UUID], engine: Engine, statement: str
) -> None:
    _insert(engine, _change(chain), TABLES[CatalystChange])
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as connection:
        connection.execute(text(statement))


def test_every_check_in_the_models_exists_in_the_migrated_database(engine: Engine) -> None:
    # Alembic's drift check cannot see CHECK constraints; their names carry a hash of their SQL.
    expected = {
        str(c.name) for table in TABLES.values() for c in table.constraints if str(c.name).startswith("ck_")
    }
    with engine.connect() as connection:
        query = text(
            "SELECT c.conname FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE c.contype = 'c' AND n.nspname = 'public'"
        )
        found = set(connection.execute(query).scalars())
    assert expected <= found, f"missing from migrations: {sorted(expected - found)}"
    assert found <= expected, f"in the database but not the models: {sorted(found - expected)}"


def test_the_same_claim_cannot_be_stored_twice(chain: dict[str, UUID], engine: Engine) -> None:
    with engine.connect() as connection:
        row = dict(connection.execute(text("SELECT * FROM claim")).mappings().one())
    row["id"] = uuid4()
    with pytest.raises(IntegrityError, match="uq_claim"), engine.begin() as connection:
        connection.execute(insert(TABLES[Claim]).values(**row))


def test_a_ticker_can_be_current_for_only_one_company(chain: dict[str, UUID], engine: Engine) -> None:
    other = Company(cik=1682852, name="Moderna Inc")
    _insert(engine, other, TABLES[Company])
    for company_id in (chain["company"], other.id):
        values = {"company_id": company_id, "ticker": "ABCD", "valid_from": date(2020, 1, 1)}
        if company_id == other.id:
            with pytest.raises(IntegrityError, match="ix_company_ticker_current"):
                _raw_insert(engine, TABLES[CompanyTicker], values)
        else:
            _raw_insert(engine, TABLES[CompanyTicker], values)


def test_an_empty_old_value_is_stored_as_sql_null(chain: dict[str, UUID], engine: Engine) -> None:
    _insert(engine, _change(chain), TABLES[CatalystChange])
    with engine.connect() as connection:
        empty = connection.execute(text("SELECT count(*) FROM catalyst_change WHERE old_value IS NULL"))
        assert empty.scalar_one() == 1


def test_every_table_is_closed_to_supabase_public_api_roles(engine: Engine) -> None:
    # Supabase serves tables in the public schema to anyone holding the project's publishable
    # key unless row-level security is on. With it on and no policies, only the server's
    # own connection (table owner, or the secret key) can read or write (ADR 0011).
    with engine.connect() as connection:
        open_tables = (
            connection.execute(
                text(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relrowsecurity"
                )
            )
            .scalars()
            .all()
        )
    assert open_tables == []


# --- Several drugs per catalyst (owner ruling 2026-10-17, rule 3; ADR 0016) ---


def _drugs_claim(engine: Engine, chain: dict[str, UUID]) -> UUID:
    """Store a claim stating the catalyst's drugs, beside the fixture's window claim."""
    with engine.connect() as connection:
        row = dict(
            connection.execute(text("SELECT * FROM claim WHERE id = :c"), {"c": chain["claim"]})
            .mappings()
            .one()
        )
    row |= {"id": uuid4(), "field": "drugs", "value": ["povetacicept"], "quote": "povetacicept"}
    row |= {"quote_start": 120, "quote_end": 132}
    with engine.begin() as connection:
        connection.execute(insert(TABLES[Claim]).values(**row))
    return row["id"]


def _second_drug(engine: Engine, chain: dict[str, UUID], name: str) -> UUID:
    drug = Drug(company_id=chain["company"], preferred_name=name)
    return _insert(engine, drug, TABLES[Drug])


def test_one_catalyst_links_several_drugs_alone_and_in_combination(
    chain: dict[str, UUID], engine: Engine
) -> None:
    claim = _drugs_claim(engine, chain)
    other = _second_drug(engine, chain, "SPY002")
    combo = _second_drug(engine, chain, "SPY120")
    links = [
        (chain["drug"], DrugRole.ALONE),
        (chain["drug"], DrugRole.IN_COMBINATION),
        (other, DrugRole.ALONE),
        (combo, DrugRole.IN_COMBINATION),
    ]
    for drug_id, role in links:
        link = CatalystDrug(catalyst_id=chain["catalyst"], drug_id=drug_id, role=role, claim_id=claim)
        _insert(engine, link, TABLES[CatalystDrug])
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM catalyst_drug WHERE catalyst_id = :c"), {"c": chain["catalyst"]}
        )
        assert count.scalar_one() == len(links)


def test_the_same_drug_link_cannot_be_stored_twice(chain: dict[str, UUID], engine: Engine) -> None:
    claim = _drugs_claim(engine, chain)
    link = {
        "catalyst_id": chain["catalyst"],
        "drug_id": chain["drug"],
        "role": "alone",
        "claim_id": claim,
    }
    _raw_insert(engine, TABLES[CatalystDrug], link)
    with pytest.raises(IntegrityError, match="uq_catalyst_drug"):
        _raw_insert(engine, TABLES[CatalystDrug], link)


@pytest.mark.parametrize("table", ["catalyst_drug", "drug_component"])
def test_a_drug_link_needs_a_stored_claim(chain: dict[str, UUID], engine: Engine, table: str) -> None:
    other = _second_drug(engine, chain, "SPY002")
    values = (
        {"catalyst_id": chain["catalyst"], "drug_id": other, "role": "alone"}
        if table == "catalyst_drug"
        else {"combination_id": chain["drug"], "component_id": other}
    )
    model = CatalystDrug if table == "catalyst_drug" else DrugComponent
    with pytest.raises(IntegrityError, match=f"fk_{table}_claim_id_claim"):
        _raw_insert(engine, TABLES[model], {**values, "claim_id": uuid4()})


def test_a_combination_lists_its_parts_but_never_itself(chain: dict[str, UUID], engine: Engine) -> None:
    claim = _drugs_claim(engine, chain)
    part = _second_drug(engine, chain, "SPY002")
    combo = _second_drug(engine, chain, "SPY120")
    part_link = DrugComponent(combination_id=combo, component_id=part, claim_id=claim)
    _insert(engine, part_link, TABLES[DrugComponent])
    with pytest.raises(IntegrityError, match="not_itself"):
        _raw_insert(
            engine,
            TABLES[DrugComponent],
            {"combination_id": combo, "component_id": combo, "claim_id": claim},
        )


def test_the_model_refuses_a_combination_of_itself() -> None:
    drug = uuid4()
    with pytest.raises(ValueError, match="cannot be its own component"):
        DrugComponent(combination_id=drug, component_id=drug, claim_id=uuid4())
