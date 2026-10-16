"""The source-document record enforces deep links, real hashes, and time zones (spec: Provenance)."""

from datetime import UTC, datetime
from typing import Any

import pytest
from annotated_types import MaxLen
from pydantic import ValidationError

from catalyst.db.models import SourceDocumentRow
from catalyst.schemas import SourceDocument

VALID: dict[str, Any] = {
    "url": "https://www.sec.gov/Archives/edgar/data/875320/000087532024000001/ex99-1.htm",
    "publisher": "SEC EDGAR",
    "document_type": "8-K EX-99.1",
    "published_at": datetime(2024, 10, 30, 20, 5, tzinfo=UTC),
    "retrieved_at": datetime(2024, 10, 31, 12, 0, tzinfo=UTC),
    "sha256": "a" * 64,
    "snapshot_key": "sha256/aa/" + "a" * 64,
    "content_type": "text/html",
}


def test_valid_record_gets_an_id() -> None:
    doc = SourceDocument.model_validate(VALID)
    assert doc.id is not None


@pytest.mark.parametrize(
    "url",
    [
        "https://www.sec.gov",
        "https://www.sec.gov/",
        "http://www.sec.gov/Archives/edgar/data/1/2/x.htm",
        "ftp://example.org/file",
        "not a url",
    ],
)
def test_homepages_and_non_https_links_are_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        SourceDocument.model_validate({**VALID, "url": url})


@pytest.mark.parametrize("sha", ["", "a" * 63, "A" * 64, "g" * 64])
def test_sha256_must_be_64_lowercase_hex(sha: str) -> None:
    with pytest.raises(ValidationError):
        SourceDocument.model_validate({**VALID, "sha256": sha})


@pytest.mark.parametrize("field", ["published_at", "retrieved_at"])
def test_times_without_time_zone_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        SourceDocument.model_validate({**VALID, field: datetime(2024, 10, 30, 20, 5)})  # noqa: DTZ001


def test_every_field_has_a_description() -> None:
    for name, field in SourceDocument.model_fields.items():
        assert field.description, f"{name} has no description"


def test_database_table_matches_the_pydantic_model() -> None:
    columns = SourceDocumentRow.__table__.columns
    assert set(columns.keys()) == set(SourceDocument.model_fields)
    for name, field in SourceDocument.model_fields.items():
        column = columns[name]
        assert not column.nullable, f"{name}: model requires a value, so the column must be NOT NULL"
        model_max = next((m.max_length for m in field.metadata if isinstance(m, MaxLen)), None)
        column_length: int | None = getattr(column.type, "length", None)
        if column_length is not None:
            assert model_max == column_length, (
                f"{name}: model allows {model_max} chars, column {column_length}"
            )


def test_longest_valid_values_fit_the_columns() -> None:
    longest = {"publisher": "x" * 200, "document_type": "x" * 100, "content_type": "x" * 200}
    SourceDocument.model_validate({**VALID, **longest})
    with pytest.raises(ValidationError):
        SourceDocument.model_validate({**VALID, "document_type": "x" * 101})
