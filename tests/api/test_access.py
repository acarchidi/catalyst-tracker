"""Free versus paid access (spec: Offering), judged against the spec, not the code.

Free: the next 30 days of the calendar, the summary fields, every source link, and only the
latest date change. Paid: the full horizon and the full change history.
"""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from hypothesis import given
from hypothesis import strategies as st

from catalyst.api.access import FREE_HORIZON_DAYS, NOTICE, Access, calendar_window, shape
from catalyst.api.views import (
    CatalystDetail,
    CatalystSummary,
    ChangeView,
    CompanyView,
    SourceView,
    WindowView,
)
from catalyst.schemas import CatalystStatus, CatalystType, DateConfidence, DatePrecision, SourceTier

TODAY = date(2026, 10, 17)
dates = st.dates(min_value=date(2020, 1, 1), max_value=date(2035, 12, 31))


def _source(quote: str) -> SourceView:
    when = datetime(2026, 10, 1, tzinfo=UTC)
    return SourceView(
        claim_id=uuid4(),
        url="https://www.sec.gov/Archives/edgar/data/1/ex99-1.htm",
        publisher="SEC EDGAR",
        document_type="8-K EX-99.1",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        published_at=when,
        retrieved_at=when,
        quote=quote,
        fields=["window_start"],
    )


def _detail() -> CatalystDetail:
    change = ChangeView(
        field="window_start",
        old_value="2026-07-01",
        new_value="2026-10-01",
        learned_at=datetime(2026, 10, 1, tzinfo=UTC),
        is_correction=False,
        source=_source("topline data in the fourth quarter"),
    )
    return CatalystDetail(
        id=uuid4(),
        catalyst_type=CatalystType.TOPLINE_READOUT,
        title="Phase 3 topline data",
        company=CompanyView(id=uuid4(), name="Example Bio", cik=1, ticker="EXB"),
        indication=None,
        drugs=[],
        window=WindowView(
            start=date(2026, 10, 1), end=date(2026, 12, 31), precision=DatePrecision.QUARTER, raw_text="4Q26"
        ),
        date_confidence=DateConfidence.COMPANY_CONFIRMED,
        status=CatalystStatus.SLIPPED,
        latest_date_change=[change],
        sources=[_source("topline data in the fourth quarter"), _source("a second source")],
        history=[change, change],
    )


@given(start=st.none() | dates, end=st.none() | dates)
def test_free_calendar_never_reaches_past_the_next_30_days(start: date | None, end: date | None) -> None:
    window = calendar_window(Access.FREE, TODAY, start, end)
    if window is None:
        return
    first, last = window
    assert last is not None
    assert TODAY <= first <= last <= TODAY + timedelta(days=FREE_HORIZON_DAYS)


@given(start=dates, end=dates)
def test_paid_calendar_gets_the_window_it_asks_for(start: date, end: date) -> None:
    window = calendar_window(Access.PAID, TODAY, start, end)
    assert window == ((start, end) if start <= end else None)


def test_default_windows() -> None:
    assert calendar_window(Access.FREE, TODAY, None, None) == (TODAY, TODAY + timedelta(days=30))
    assert calendar_window(Access.PAID, TODAY, None, None) == (TODAY, None)


def test_free_window_wholly_outside_the_horizon_is_empty() -> None:
    assert calendar_window(Access.FREE, TODAY, date(2027, 3, 1), date(2027, 6, 30)) is None
    assert calendar_window(Access.FREE, TODAY, date(2025, 1, 1), date(2025, 12, 31)) is None


def test_free_never_sees_paid_fields() -> None:
    detail = _detail()
    free = shape(detail, Access.FREE)
    dumped = free.model_dump(mode="json")
    assert type(free) is CatalystSummary
    assert "history" not in dumped
    assert set(dumped) == set(CatalystSummary.model_fields)


def test_free_keeps_every_source_link_and_the_latest_date_change() -> None:
    detail = _detail()
    free = shape(detail, Access.FREE)
    assert free.sources == detail.sources
    assert free.latest_date_change == detail.latest_date_change


def test_paid_sees_the_full_history() -> None:
    detail = _detail()
    paid = shape(detail, Access.PAID)
    assert isinstance(paid, CatalystDetail)
    assert len(paid.model_dump(mode="json")["history"]) == 2


def test_paid_only_fields_are_exactly_what_the_summary_lacks() -> None:
    # A new paid field must be added to the detail, never to the summary.
    assert set(CatalystDetail.model_fields) - set(CatalystSummary.model_fields) == {"history"}


def test_notice_says_nothing_is_financial_advice() -> None:
    assert "not financial advice" in NOTICE.lower() or "nothing here is financial advice" in NOTICE.lower()
