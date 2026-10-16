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
    CatalystStatus,
    CatalystType,
    Claim,
    ClaimSubject,
    Company,
    DateConfidence,
    DatePrecision,
    Drug,
    Entity,
    Indication,
    SourceDocument,
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
    properties = model.model_json_schema()["properties"]
    table = TABLES[model]
    allowed_lists = {c.name for c in table.constraints if str(c.name).endswith("_allowed")}
    for name, column in table.columns.items():
        has_allowed_list = f"ck_{table.name}_{name}_allowed" in allowed_lists
        if isinstance(column.type, String) and column.type.length and not has_allowed_list:
            limits = [properties[name].get("maxLength")] + [
                option.get("maxLength") for option in properties[name].get("anyOf", [])
            ]
            assert column.type.length in limits, f"{model.__name__}.{name}"


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
        url="https://www.sec.gov/Archives/edgar/data/875320/000087532026000256/ex-991_q22026.htm",
        publisher="SEC EDGAR",
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
        drug_id=drug.id,
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
    yield {"company": company.id, "catalyst": catalyst.id, "claim": claim.id}
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
        field="window_end",
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
