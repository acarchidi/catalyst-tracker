"""Database tables generated from the Pydantic models in `catalyst.schemas` (ADR 0003).

Each model field becomes a column: its type, NOT NULL, length limit, allowed values, foreign
key, and comment (the field description) all come from the model. No table is written by hand.
"""

import hashlib
import types
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Union, get_args, get_origin
from uuid import UUID

from annotated_types import MaxLen
from pydantic import AwareDatetime, JsonValue
from pydantic.fields import FieldInfo
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeEngine

from catalyst.schemas import ALL_MODELS, Entity, References

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


def resolve_field(field: FieldInfo) -> tuple[Any, bool, list[Any]]:
    """Return the field's base type, whether it may be empty, and all its constraint markers.

    Pydantic flattens `Annotated` metadata for required fields but not inside `X | None`,
    so markers are collected from both places.
    """
    annotation: Any = field.annotation
    markers: list[Any] = list(field.metadata)
    nullable = False
    if get_origin(annotation) in (Union, types.UnionType):
        args = get_args(annotation)
        inner = [a for a in args if a is not type(None)]
        nullable = len(inner) < len(args)
        if len(inner) == 1:
            annotation = inner[0]
    if hasattr(annotation, "__metadata__"):
        for marker in annotation.__metadata__:
            markers.extend(marker.metadata if isinstance(marker, FieldInfo) else [marker])
        annotation = annotation.__origin__
    return annotation, nullable, markers


def _column_type(annotation: Any, markers: list[Any]) -> TypeEngine[Any]:
    """Map a Python type to a Postgres column type."""
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return String(max(len(member.value) for member in annotation))
    simple: dict[Any, TypeEngine[Any]] = {
        UUID: Uuid(),
        AwareDatetime: DateTime(timezone=True),
        datetime: DateTime(timezone=True),
        date: Date(),
        bool: Boolean(),
        int: BigInteger(),
        Decimal: Numeric(14, 8),
    }
    if annotation in simple:
        return simple[annotation]
    if annotation is str:
        length = next((m.max_length for m in markers if isinstance(m, MaxLen)), None)
        return String(length) if length else Text()
    if annotation is JsonValue or get_origin(annotation) in (dict, list):
        return JSONB(none_as_null=True)
    raise TypeError(f"no column type for {annotation!r}; add it to catalyst.db.tables")


def _column(name: str, field: FieldInfo) -> Column[Any]:
    annotation, nullable, markers = resolve_field(field)
    reference = next((m for m in markers if isinstance(m, References)), None)
    foreign_key = [ForeignKey(f"{reference.table}.id", ondelete="RESTRICT")] if reference else []
    return Column(
        name,
        _column_type(annotation, markers),
        *foreign_key,
        primary_key=name == "id",
        nullable=nullable,
        comment=field.description,
    )


def _check(name: str, sql: str) -> CheckConstraint:
    """Build a CHECK constraint whose name ends in a hash of its SQL.

    Alembic cannot compare CHECK constraints, so a changed rule gets a new name; a test then
    finds any rule that exists in the models but not in the migrated database, or the reverse.
    """
    digest = hashlib.sha256(sql.encode()).hexdigest()[:8]
    return CheckConstraint(sql, name=f"{name}_{digest}")


def _enum_checks(model: type[Entity]) -> list[CheckConstraint]:
    checks: list[CheckConstraint] = []
    for name, field in model.model_fields.items():
        annotation, _, _ = resolve_field(field)
        if isinstance(annotation, type) and issubclass(annotation, StrEnum):
            allowed = ", ".join(f"'{member.value}'" for member in annotation)
            checks.append(_check(f"{name}_allowed", f"{name} IN ({allowed})"))
    return checks


def build_table(model: type[Entity], target: MetaData) -> Table:
    """Create the table for `model` in `target`."""
    columns = [_column(name, field) for name, field in model.model_fields.items()]
    uniques = [UniqueConstraint(*group) for group in model.__unique__]
    uniques += [
        UniqueConstraint(*group, postgresql_nulls_not_distinct=True) for group in model.__unique_nulls_equal__
    ]
    checks = [_check(name, sql) for name, sql in model.__checks__.items()]
    table = Table(
        model.__tablename__,
        target,
        *columns,
        *uniques,
        *checks,
        *_enum_checks(model),
        comment=(model.__doc__ or "").strip().splitlines()[0],
    )
    for index_name, (columns, where) in model.__partial_unique__.items():
        Index(index_name, *(table.c[c] for c in columns), unique=True, postgresql_where=text(where))
    return table


TABLES: dict[type[Entity], Table] = {model: build_table(model, metadata) for model in ALL_MODELS}
