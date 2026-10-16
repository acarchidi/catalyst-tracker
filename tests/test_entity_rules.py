"""Validation rules for every entity, written from docs/spec.md (Data model, Provenance).

These tests were written before the models. Each rule names the spec line it enforces.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from catalyst.schemas import (
    ALL_MODELS,
    Catalyst,
    CatalystChange,
    CatalystStatus,
    CatalystType,
    Claim,
    ClaimSubject,
    Company,
    CompanyName,
    CompanyTicker,
    DateConfidence,
    DatePrecision,
    Drug,
    Indication,
    Modality,
    Outcome,
    OutcomeResult,
    Target,
    Trial,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def _catalyst(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "catalyst_type": CatalystType.PDUFA_DATE,
        "company_id": uuid4(),
        "title": "FDA decision on povetacicept",
        "window_start": date(2026, 11, 30),
        "window_end": date(2026, 11, 30),
        "window_precision": DatePrecision.DAY,
        "window_raw_text": "PDUFA date November 30th",
        "date_confidence": DateConfidence.COMPANY_CONFIRMED,
        "status": CatalystStatus.UPCOMING,
    }
    return {**base, **overrides}


def _claim(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source_document_id": uuid4(),
        "subject_type": ClaimSubject.CATALYST,
        "field": "window",
        "value": {"raw": "November 30th"},
        "quote": "povetacicept PDUFA date November 30th",
        "quote_start": 120,
        "quote_end": 157,
        "agent": "extractor",
        "prompt_id": "extract_catalysts",
        "prompt_version": "1.0.0",
        "provider": "anthropic",
        "model": "example-model",
        "extracted_at": NOW,
    }
    return {**base, **overrides}


# --- Spec: "Every data field has a description in its Pydantic model." ---


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
def test_every_field_of_every_model_has_a_description(model: type[BaseModel]) -> None:
    for name, field in model.model_fields.items():
        assert field.description, f"{model.__name__}.{name} has no description"


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
def test_every_model_has_a_docstring(model: type[BaseModel]) -> None:
    assert model.__doc__ is not None
    assert model.__doc__.strip()


# --- Spec: catalyst dates are windows with start, end, precision, raw text, confidence. ---


@given(
    start=st.dates(min_value=date(2000, 1, 1), max_value=date(2040, 12, 31)),
    days=st.integers(min_value=1, max_value=3650),
    precision=st.sampled_from([p for p in DatePrecision if p is not DatePrecision.DAY]),
)
def test_any_forward_window_is_valid(start: date, days: int, precision: DatePrecision) -> None:
    end = start + timedelta(days=days)
    catalyst = Catalyst.model_validate(
        _catalyst(window_start=start, window_end=end, window_precision=precision)
    )
    assert catalyst.window_start <= catalyst.window_end


@given(
    end=st.dates(min_value=date(2000, 1, 1), max_value=date(2040, 12, 31)),
    days=st.integers(min_value=1, max_value=3650),
)
def test_a_window_that_ends_before_it_starts_is_rejected(end: date, days: int) -> None:
    with pytest.raises(ValidationError, match="window_start"):
        Catalyst.model_validate(
            _catalyst(
                window_start=end + timedelta(days=days), window_end=end, window_precision=DatePrecision.YEAR
            )
        )


def test_day_precision_means_a_single_day() -> None:
    with pytest.raises(ValidationError, match="day"):
        Catalyst.model_validate(_catalyst(window_start=date(2026, 11, 1), window_end=date(2026, 11, 30)))


def test_wider_precision_cannot_be_a_single_day() -> None:
    with pytest.raises(ValidationError, match="precision"):
        Catalyst.model_validate(_catalyst(window_precision=DatePrecision.QUARTER))


@pytest.mark.parametrize("raw", ["", "   "])
def test_the_raw_date_text_as_written_is_required(raw: str) -> None:
    with pytest.raises(ValidationError):
        Catalyst.model_validate(_catalyst(window_raw_text=raw))


def test_the_four_confidence_labels_match_the_spec() -> None:
    assert {c.value for c in DateConfidence} == {
        "company_confirmed",
        "regulator_scheduled",
        "registry_estimate",
        "inferred",
    }


def test_the_precision_levels_match_the_spec() -> None:
    assert {p.value for p in DatePrecision} == {"day", "month", "quarter", "half", "year"}


def test_the_modality_list_matches_the_spec() -> None:
    assert len(Modality) == 13
    assert {"small_molecule", "antibody", "adc", "radiopharmaceutical", "other"} <= {
        m.value for m in Modality
    }


# --- Spec: Provenance. Every claim carries a verbatim quote and its location. ---


def test_a_complete_claim_is_valid() -> None:
    assert Claim.model_validate(_claim()).quote.startswith("povetacicept")


@pytest.mark.parametrize("quote", ["", "   "])
def test_a_claim_without_a_quote_is_rejected(quote: str) -> None:
    with pytest.raises(ValidationError):
        Claim.model_validate(_claim(quote=quote))


@pytest.mark.parametrize(("start", "end"), [(10, 10), (10, 5), (-1, 5)])
def test_a_claim_location_must_be_a_forward_span(start: int, end: int) -> None:
    with pytest.raises(ValidationError):
        Claim.model_validate(_claim(quote_start=start, quote_end=end))


def test_claims_are_short_verification_quotes_not_republished_text() -> None:
    with pytest.raises(ValidationError):
        Claim.model_validate(_claim(quote="x" * 1001, quote_end=120 + 1001))


@pytest.mark.parametrize("missing", ["agent", "prompt_id", "prompt_version", "provider", "model"])
def test_a_claim_records_who_extracted_it(missing: str) -> None:
    data = _claim()
    del data[missing]
    with pytest.raises(ValidationError):
        Claim.model_validate(data)


# --- Spec: Change history records old value, new value, the claim, and when we learned it. ---


def test_a_change_must_change_something() -> None:
    with pytest.raises(ValidationError, match="new_value"):
        CatalystChange.model_validate(
            {
                "catalyst_id": uuid4(),
                "field": "window_end",
                "old_value": "2026-12-31",
                "new_value": "2026-12-31",
                "claim_id": uuid4(),
                "learned_at": NOW,
            }
        )


def test_a_change_requires_the_claim_that_caused_it() -> None:
    with pytest.raises(ValidationError):
        CatalystChange.model_validate(
            {"catalyst_id": uuid4(), "field": "status", "new_value": "slipped", "learned_at": NOW}
        )


def test_a_first_value_has_no_old_value() -> None:
    change = CatalystChange.model_validate(
        {
            "catalyst_id": uuid4(),
            "field": "status",
            "new_value": "upcoming",
            "claim_id": uuid4(),
            "learned_at": NOW,
        }
    )
    assert change.old_value is None
    assert change.is_correction is False


# --- Spec: Outcomes record result, key numbers, and the resolution source. ---


def test_an_advisory_committee_outcome_needs_both_vote_counts() -> None:
    with pytest.raises(ValidationError, match="vote"):
        Outcome.model_validate(
            {
                "catalyst_id": uuid4(),
                "result": OutcomeResult.ADVISORY_VOTE,
                "votes_for": 10,
                "resolution_claim_id": uuid4(),
                "resolved_on": date(2026, 6, 1),
            }
        )


def test_vote_counts_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        Outcome.model_validate(
            {
                "catalyst_id": uuid4(),
                "result": OutcomeResult.ADVISORY_VOTE,
                "votes_for": -1,
                "votes_against": 3,
                "resolution_claim_id": uuid4(),
                "resolved_on": date(2026, 6, 1),
            }
        )


# --- Spec: canonical identifiers and cross-references (Data model table). ---

IDENTIFIER_CASES: list[tuple[type[BaseModel], dict[str, Any], str, list[object], list[object]]] = [
    (Company, {"name": "Vertex Pharmaceuticals Inc"}, "cik", [875320, 1, 9999999999], [0, -5, 10**10]),
    (Company, {"name": "Vertex", "cik": 875320}, "sic_code", ["2834", None], ["283", "28345", "ABCD"]),
    (Trial, {}, "nct_id", ["NCT04567890"], ["NCT123", "nct04567890", "04567890"]),
    (
        Trial,
        {"nct_id": "NCT04567890"},
        "eu_ct_number",
        ["2023-501234-12-00", "2015-001234-56", None],
        ["2023-1"],
    ),
    (
        Indication,
        {"name": "IgA nephropathy"},
        "mesh_id",
        ["D005922", "C537218"],
        ["d005922", "M123456", "D12"],
    ),
    (
        Indication,
        {"name": "IgA nephropathy", "mesh_id": "D005922"},
        "icd10_code",
        ["N02.8", "C34", "C34.90", None],
        ["34.9", "C3"],
    ),
    (
        Indication,
        {"name": "x", "mesh_id": "D005922"},
        "efo_id",
        ["EFO_0004194", "MONDO_0005148", None],
        ["EFO-1"],
    ),
    (Drug, {"preferred_name": "povetacicept"}, "unii", ["ABC1234567", None], ["abc1234567", "ABC123"]),
    (Drug, {"preferred_name": "povetacicept"}, "chembl_id", ["CHEMBL25", None], ["chembl25", "CHEMBL"]),
    (Target, {}, "hgnc_symbol", ["EGFR", "HLA-A", "C1orf112"], ["", "-EGFR", "EG FR"]),
]


@pytest.mark.parametrize(
    ("model", "base", "field", "good", "bad"), IDENTIFIER_CASES, ids=lambda c: str(c)[:30]
)
def test_identifier_formats(
    model: type[BaseModel], base: dict[str, Any], field: str, good: list[object], bad: list[object]
) -> None:
    for value in good:
        model.model_validate({**base, field: value})
    for value in bad:
        with pytest.raises(ValidationError):
            model.model_validate({**base, field: value})


@pytest.mark.parametrize("ticker", ["VRTX", "BRK.B", "ABC-WS"])
def test_valid_tickers(ticker: str) -> None:
    CompanyTicker.model_validate({"company_id": uuid4(), "ticker": ticker, "valid_from": date(2020, 1, 1)})


@pytest.mark.parametrize("ticker", ["vrtx", "", "TOOLONGTICKER", "V RTX"])
def test_invalid_tickers(ticker: str) -> None:
    with pytest.raises(ValidationError):
        CompanyTicker.model_validate(
            {"company_id": uuid4(), "ticker": ticker, "valid_from": date(2020, 1, 1)}
        )


@pytest.mark.parametrize("model", [CompanyTicker, CompanyName])
def test_validity_periods_cannot_end_before_they_start(model: type[BaseModel]) -> None:
    data: dict[str, Any] = {
        "company_id": uuid4(),
        "valid_from": date(2024, 1, 1),
        "valid_to": date(2023, 1, 1),
        "ticker": "VRTX",
        "name": "Vertex",
    }
    allowed = set(model.model_fields)
    with pytest.raises(ValidationError, match="valid_to"):
        model.model_validate({k: v for k, v in data.items() if k in allowed})
