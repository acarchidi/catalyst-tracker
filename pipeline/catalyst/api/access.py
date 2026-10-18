"""Free versus paid access (spec: Offering; ADR 0025).

Free callers get the next 30 days of the calendar, the summary fields, every source link, and
only the latest date change. Paid callers get the full horizon and the full change history.
Accounts come later; until then every caller is free unless code says otherwise.
"""

from datetime import date, timedelta
from enum import StrEnum

from catalyst.api.views import CatalystDetail, CatalystSummary, Shaped

FREE_HORIZON_DAYS = 30
NOTICE = (
    "Nothing here is financial advice: these are published facts with their sources, not a "
    "recommendation to buy, sell, or hold any security."
)


class Access(StrEnum):
    """What a caller may see."""

    FREE = "free"
    PAID = "paid"


def calendar_window(
    access: Access, today: date, start: date | None, end: date | None
) -> tuple[date, date | None] | None:
    """Return the date window the caller may see for a requested one, or None when it is empty.

    Both default to open: from today, with no end. A free window is cut to today through
    today + 30 days. An empty end means no end.
    """
    first = start or today
    last = end
    if access is Access.FREE:
        horizon = today + timedelta(days=FREE_HORIZON_DAYS)
        first = max(first, today)
        last = horizon if last is None else min(last, horizon)
    if last is not None and first > last:
        return None
    return first, last


def shape(detail: CatalystDetail, access: Access) -> Shaped:
    """Return the catalyst as `access` may see it: free callers get the summary fields only."""
    if access is Access.PAID:
        return detail
    return CatalystSummary.model_validate(
        {name: getattr(detail, name) for name in CatalystSummary.model_fields}
    )
