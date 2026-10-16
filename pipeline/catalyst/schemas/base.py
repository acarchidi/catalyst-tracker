"""Shared base for stored entities, plus the markers that shape their database tables.

Tables are generated from these models (`catalyst.db.tables`); see ADR 0003.
"""

from dataclasses import dataclass
from typing import ClassVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class References:
    """Marks a field as a foreign key to another table's `id`."""

    table: str


class Entity(BaseModel):
    """A stored record. Subclasses set `__tablename__` and may add constraints.

    `__unique__` lists column groups that must be unique; empty values never clash.
    `__unique_nulls_equal__` lists groups where empty counts as a value, so two rows that
    differ only by both being empty clash. `__checks__` maps a name to a SQL condition that
    the database enforces in addition to the Pydantic rules. `__partial_unique__` maps an index
    name to columns that must be unique among rows matching a SQL condition.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    __tablename__: ClassVar[str]
    __unique__: ClassVar[tuple[tuple[str, ...], ...]] = ()
    __unique_nulls_equal__: ClassVar[tuple[tuple[str, ...], ...]] = ()
    __checks__: ClassVar[dict[str, str]] = {}
    __partial_unique__: ClassVar[dict[str, tuple[tuple[str, ...], str]]] = {}

    id: UUID = Field(default_factory=uuid4, description="Internal identifier.")
