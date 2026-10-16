"""The database ledger: every job shares one daily total per role, reset at New York midnight."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine

from catalyst.llm.ledger import DatabaseLedger, new_york_day
from catalyst.schemas import ModelCall, ModelCallStatus
from conftest import truncate_all


def _call(role: str, at: datetime, cost: str) -> ModelCall:
    return ModelCall(
        role=role,
        provider="anthropic",
        model="claude-sonnet-5-5",
        prompt_id="demo",
        prompt_version="1.0.0",
        input_tokens=1000,
        output_tokens=100,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=Decimal(cost),
        latency_ms=1200,
        request_id="req_1",
        status=ModelCallStatus.OK,
        called_at=at,
        config_version="test-1",
    )


@pytest.fixture
def ledger(engine: Engine) -> Iterator[DatabaseLedger]:
    yield DatabaseLedger(engine)
    truncate_all(engine)


def test_new_york_day_follows_daylight_saving() -> None:
    assert new_york_day(datetime(2026, 10, 16, 3, 59, tzinfo=UTC)) == date(2026, 10, 15)  # EDT, UTC-4
    assert new_york_day(datetime(2026, 12, 16, 4, 59, tzinfo=UTC)) == date(2026, 12, 15)  # EST, UTC-5
    assert new_york_day(datetime(2026, 12, 16, 5, 0, tzinfo=UTC)) == date(2026, 12, 16)


def test_spend_sums_one_role_on_one_new_york_day(ledger: DatabaseLedger) -> None:
    ledger.record(_call("extractor", datetime(2026, 10, 16, 4, 0, tzinfo=UTC), "0.25"))  # 00:00 New York
    ledger.record(_call("extractor", datetime(2026, 10, 17, 3, 59, tzinfo=UTC), "0.50"))  # 23:59 New York
    ledger.record(_call("extractor", datetime(2026, 10, 16, 3, 59, tzinfo=UTC), "9"))  # previous day
    ledger.record(_call("analyst", datetime(2026, 10, 16, 12, 0, tzinfo=UTC), "7"))  # other role
    assert ledger.spent("extractor", date(2026, 10, 16)) == Decimal("0.75")


def test_no_spend_is_zero(ledger: DatabaseLedger) -> None:
    assert ledger.spent("triage", date(2026, 10, 16)) == 0


def test_costs_keep_eight_decimal_places(ledger: DatabaseLedger) -> None:
    ledger.record(_call("triage", datetime(2026, 10, 16, 12, 0, tzinfo=UTC), "0.00000105"))
    assert ledger.spent("triage", date(2026, 10, 16)) == Decimal("0.00000105")
