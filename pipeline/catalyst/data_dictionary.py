"""Generate the plain-English data dictionary (docs/data-dictionary.md) from the Pydantic models.

Run `uv run python -m catalyst.data_dictionary` after changing a model. A test fails while the
committed file is out of date.
"""

import sys
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Numeric, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeEngine

from catalyst.db.tables import TABLES, resolve_field
from catalyst.schemas import ALL_MODELS, Entity

OUTPUT = Path(__file__).resolve().parents[2] / "docs" / "data-dictionary.md"


def _plain_type(model: type[Entity], name: str) -> str:
    """Describe a column's type in plain words."""
    column = TABLES[model].columns[name]
    if column.foreign_keys:
        return f"link to `{next(iter(column.foreign_keys)).column.table.name}`"
    annotation, _, _ = resolve_field(model.model_fields[name])
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return "one of: " + ", ".join(f"`{member.value}`" for member in annotation)
    kind: TypeEngine[Any] = column.type
    if isinstance(kind, String) and kind.length:
        return f"text, up to {kind.length} characters"
    words: list[tuple[type[TypeEngine[Any]], str]] = [
        (Text, "text"),
        (Uuid, "identifier"),
        (DateTime, "date and time (with time zone)"),
        (Date, "date"),
        (BigInteger, "whole number"),
        (Numeric, "decimal number"),
        (Boolean, "yes or no"),
        (JSONB, "structured value (JSON)"),
    ]
    return next(word for cls, word in words if isinstance(kind, cls))


def _rules(model: type[Entity]) -> list[str]:
    lines = [f"- Unique: {', '.join(f'`{c}`' for c in group)}" for group in model.__unique__]
    lines += [
        f"- Unique, counting empty as a value: {', '.join(f'`{c}`' for c in group)}"
        for group in model.__unique_nulls_equal__
    ]
    lines += [f"- Database check `{name}`: `{sql}`" for name, sql in model.__checks__.items()]
    return lines


def render() -> str:
    """Return the data dictionary as Markdown."""
    out = [
        "# Data dictionary",
        "",
        "Generated from `pipeline/catalyst/schemas` by `uv run python -m catalyst.data_dictionary`.",
        "Do not edit by hand. Every table has an `id` (internal identifier) as its first field.",
        "",
    ]
    for model in ALL_MODELS:
        table = TABLES[model]
        doc = " ".join((model.__doc__ or "").split())
        out += [
            f"## `{table.name}`",
            "",
            doc,
            "",
            "| Field | Type | Required | Meaning |",
            "| --- | --- | --- | --- |",
        ]
        for name, field in model.model_fields.items():
            required = "no" if table.columns[name].nullable else "yes"
            out.append(f"| `{name}` | {_plain_type(model, name)} | {required} | {field.description} |")
        rules = _rules(model)
        out += ["", "Rules:", "", *rules] if rules else []
        out.append("")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    """Write the file, or with `--check` report whether it is current."""
    text = render()
    if "--check" in argv:
        current = OUTPUT.read_text() if OUTPUT.exists() else ""
        return 0 if current == text else 1
    OUTPUT.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
