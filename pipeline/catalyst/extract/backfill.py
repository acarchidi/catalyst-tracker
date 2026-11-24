"""Whether the press-release backfill may spend today, and how much is left to read (ADR 0065).

Plain code, no model call. The backfill's model reads run only while all of these hold:
the day is not past `BACKFILL_LAST_DAY`, when the promotional credit expires; every model call
since `CREDIT_FIRST_DAY`, of any role and any job, plus the most one day of backfill can spend,
stays within `CREDIT_USD`, so the credit is never overspent into paid money; and some stored press
release since `BACKFILL_SINCE` is still unread. The daily caps in config/models.toml still apply to
every call through `ModelClient`; this only decides whether to start.
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from catalyst.collectors.sec.backfill import BACKFILL_LAST_DAY, BACKFILL_SINCE
from catalyst.db.tables import TABLES
from catalyst.extract.run import waiting_documents
from catalyst.schemas import ModelCall

NEW_YORK = ZoneInfo("America/New_York")
# The owner's promotional API credit (2026-11-23): about $96, expiring after BACKFILL_LAST_DAY.
CREDIT_USD = Decimal("96.00")
CREDIT_FIRST_DAY = date(2026, 11, 24)  # the backfill's first day; spend from then is counted
# The most one New York day can add: the extractor's and verifier's raised daily caps ($8 and $4)
# plus every other role's cap ($2.00), config/models.toml, ADR 0065. The backfill runs only if a
# whole such day still fits the credit, and its run starts and ends inside one New York day.
DAY_MOST_USD = Decimal("14.00")


@dataclass(frozen=True)
class BackfillStatus:
    """Whether the backfill's model reads may run today, and why not if they may not."""

    active: bool
    reason: str | None
    documents_waiting: int
    spent_since_start_usd: Decimal

    def summary(self) -> dict[str, Any]:
        """Return the status as plain JSON for the job summary."""
        return {
            "active": self.active,
            "reason": self.reason,
            "documents_waiting": self.documents_waiting,
            "spent_since_start_usd": str(self.spent_since_start_usd.quantize(Decimal("0.01"))),
            "credit_usd": str(CREDIT_USD),
            "last_day": BACKFILL_LAST_DAY.isoformat(),
        }


def spent_since(session: Session, day: date) -> Decimal:
    """Total cost of every model call from New York midnight of `day` on, all roles together."""
    calls = TABLES[ModelCall]
    start = datetime.combine(day, time(0), NEW_YORK)
    query = select(func.coalesce(func.sum(calls.c.cost_usd), 0)).where(calls.c.called_at >= start)
    return Decimal(session.execute(query).scalar_one())


def credit_covers_a_day(session: Session, today: date) -> bool:
    """Say whether the credit still pays for one more whole day at the raised caps (ADR 0065, 0066).

    The filing reader (ADR 0066) spends past a role's usual cap, up to its raised one, only while
    this holds, so the raised caps never spend paid money on filings.
    """
    return today <= BACKFILL_LAST_DAY and spent_since(session, CREDIT_FIRST_DAY) + DAY_MOST_USD <= CREDIT_USD


def backfill_status(session: Session, today: date) -> BackfillStatus:
    """Decide whether the backfill's model reads may run on `today` (a New York date)."""
    waiting = waiting_documents(session, since=BACKFILL_SINCE)
    spent = spent_since(session, CREDIT_FIRST_DAY)
    reason: str | None = None
    if today > BACKFILL_LAST_DAY:
        reason = f"the backfill ended on {BACKFILL_LAST_DAY.isoformat()}, when the credit expired"
    elif spent + DAY_MOST_USD > CREDIT_USD:
        reason = (
            f"${spent:.2f} of the ${CREDIT_USD} credit is spent; another day could pass it, so the backfill "
            "stops here"
        )
    elif waiting == 0:
        reason = "nothing is left to read"
    return BackfillStatus(reason is None, reason, waiting, spent)
