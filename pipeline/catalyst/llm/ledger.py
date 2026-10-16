"""Where model calls are recorded and daily spend is summed, per role, per New York calendar day."""

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, func, insert, select

from catalyst.db.tables import TABLES
from catalyst.schemas import ModelCall

NEW_YORK = ZoneInfo("America/New_York")
_MODEL_CALL = TABLES[ModelCall]


def new_york_day(moment: datetime) -> date:
    """Return the New York calendar date of `moment`; caps reset at New York midnight."""
    return moment.astimezone(NEW_YORK).date()


class SpendLedger(Protocol):
    """Records calls and reports what a role has spent on a given day."""

    def spent(self, role: str, day: date) -> Decimal:
        """Total cost of the role's calls on `day` (New York time)."""
        ...

    def record(self, call: ModelCall) -> None:
        """Store one call."""
        ...


class InMemoryLedger:
    """Ledger for tests and dry runs."""

    def __init__(self) -> None:
        """Start empty."""
        self.calls: list[ModelCall] = []
        self._extra: defaultdict[tuple[str, date], Decimal] = defaultdict(Decimal)

    def add_spend(self, role: str, moment: datetime, amount: Decimal) -> None:
        """Pretend `role` already spent `amount` at `moment`."""
        self._extra[(role, new_york_day(moment))] += amount

    def spent(self, role: str, day: date) -> Decimal:
        """Sum recorded and pretended spend."""
        recorded = sum(
            (c.cost_usd for c in self.calls if c.role == role and new_york_day(c.called_at) == day),
            Decimal(0),
        )
        return recorded + self._extra[(role, day)]

    def record(self, call: ModelCall) -> None:
        """Keep the call in memory."""
        self.calls.append(call)


class DatabaseLedger:
    """Ledger backed by the `model_call` table, so every job shares one daily total per role."""

    def __init__(self, engine: Engine) -> None:
        """Use `engine` for reads and writes."""
        self._engine = engine

    def spent(self, role: str, day: date) -> Decimal:
        """Sum the role's call costs between New York midnights."""
        start = datetime.combine(day, time(0), NEW_YORK)
        end = datetime.combine(day + timedelta(days=1), time(0), NEW_YORK)
        query = select(func.coalesce(func.sum(_MODEL_CALL.c.cost_usd), 0)).where(
            _MODEL_CALL.c.role == role, _MODEL_CALL.c.called_at >= start, _MODEL_CALL.c.called_at < end
        )
        with self._engine.connect() as connection:
            return Decimal(connection.execute(query).scalar_one())

    def record(self, call: ModelCall) -> None:
        """Insert the call."""
        with self._engine.begin() as connection:
            connection.execute(insert(_MODEL_CALL).values(**call.model_dump()))
