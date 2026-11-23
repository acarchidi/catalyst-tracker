"""A raised daily cap applies only on its dated New York days; outside them the usual cap does (ADR 0065)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine

from catalyst.alerts import spend_today
from catalyst.llm.client import ModelClient, SpendCapReachedError
from catalyst.llm.config import DEFAULT_CONFIG_PATH, ModelConfig, RaisedCap, RoleConfig, load_config
from catalyst.llm.ledger import InMemoryLedger
from catalyst.llm.pricing import Price
from catalyst.llm.prompts import Prompt

LAST_DAY = date(2026, 12, 8)
RAISED = RaisedCap(
    cap_usd=Decimal(8), first_day=date(2026, 11, 24), last_day=LAST_DAY, approved="owner, 2026-11-23"
)


def _role(*raised: RaisedCap) -> RoleConfig:
    return RoleConfig(
        provider="fake", model="m1", max_tokens=100, daily_cap_usd=Decimal(2), raised_caps=raised
    )


def test_the_raised_cap_holds_from_its_first_to_its_last_day_only() -> None:
    role = _role(RAISED)
    assert role.cap_on(date(2026, 11, 23)) == Decimal(2)
    assert role.cap_on(date(2026, 11, 24)) == Decimal(8)
    assert role.cap_on(LAST_DAY) == Decimal(8)
    assert role.cap_on(date(2026, 12, 9)) == Decimal(2)  # back to the usual cap with no change to the file
    assert _role().cap_on(LAST_DAY) == Decimal(2)


def test_a_raised_cap_must_be_positive_dated_in_order_and_not_overlap() -> None:
    with pytest.raises(ValueError, match="cap_usd"):
        RaisedCap(cap_usd=Decimal(0), first_day=LAST_DAY, last_day=LAST_DAY, approved="owner")
    with pytest.raises(ValueError, match="before"):
        RaisedCap(cap_usd=Decimal(8), first_day=LAST_DAY, last_day=date(2026, 11, 24), approved="owner")
    with pytest.raises(ValueError, match="approved"):
        RaisedCap(cap_usd=Decimal(8), first_day=LAST_DAY, last_day=LAST_DAY, approved="")
    later = RaisedCap(cap_usd=Decimal(5), first_day=LAST_DAY, last_day=date(2026, 12, 20), approved="owner")
    with pytest.raises(ValueError, match="overlap"):
        _role(RAISED, later)


def test_the_file_takes_a_dated_cap_written_as_toml_dates(tmp_path: Path) -> None:
    body = (
        'version = "t"\n[pricing.m1]\ninput = "1"\ncache_write = "1"\ncache_read = "0.1"\noutput = "5"\n'
        '[roles.extractor]\nprovider = "anthropic"\nmodel = "m1"\nmax_tokens = 100\ndaily_cap_usd = "2"\n'
        '[[roles.extractor.raised_caps]]\ncap_usd = "8"\nfirst_day = 2026-11-24\nlast_day = 2026-12-08\n'
        'approved = "owner"\n'
    )
    path = tmp_path / "models.toml"
    path.write_text(body)
    assert load_config(path).roles["extractor"].cap_on(LAST_DAY) == Decimal(8)


def test_the_committed_raise_is_what_the_owner_allowed_and_ends_on_2026_12_08() -> None:
    """Owner's written OK, 2026-11-23: extractor, and verifier if needed, at most $8 a day to 2026-12-08."""
    roles = load_config(DEFAULT_CONFIG_PATH).roles
    assert roles["extractor"].cap_on(date(2026, 11, 24)) == Decimal("8.00")
    assert roles["verifier"].cap_on(date(2026, 11, 24)) == Decimal("4.00")
    for name, role in roles.items():
        for raised in role.raised_caps:
            assert name in {"extractor", "verifier"}, name
            assert raised.cap_usd <= Decimal(8)
            assert raised.last_day <= LAST_DAY
        assert role.cap_on(date(2026, 12, 9)) == role.daily_cap_usd
    assert roles["extractor"].daily_cap_usd == Decimal("2.00")
    assert roles["verifier"].daily_cap_usd == Decimal("2.00")


class _Reached(Exception):
    """The call got past the cap check to the provider."""


class _Provider:
    name = "fake"

    def complete(self, **_kwargs: object) -> object:
        raise _Reached


def _client(day: datetime, spent: str) -> ModelClient:
    price = Price(input=Decimal(1), cache_write=Decimal(1), cache_read=Decimal(0), output=Decimal(1))
    config = ModelConfig(version="t", pricing={"m1": price}, roles={"extractor": _role(RAISED)})
    ledger = InMemoryLedger()
    ledger.add_spend("extractor", day, Decimal(spent))
    return ModelClient(config, {"fake": _Provider()}, ledger, now=lambda: day)  # pyright: ignore[reportArgumentType]


PROMPT = Prompt(
    id="p",
    version="1.0.0",
    purpose="test",
    eval_set="none",
    tested_models=["m1"],
    system="s",
    output_schema={"type": "object", "additionalProperties": False},
)


def test_the_client_stops_at_the_cap_of_the_calls_own_new_york_day() -> None:
    # 2026-12-09 03:00 UTC is still 2026-12-08 in New York: the raised cap applies.
    late_evening = datetime(2026, 12, 9, 3, tzinfo=UTC)
    with pytest.raises(_Reached):  # $3 spent is under that day's $8
        _client(late_evening, "3.00").run("extractor", PROMPT, "doc")
    next_day = datetime(2026, 12, 9, 15, tzinfo=UTC)
    with pytest.raises(SpendCapReachedError, match=r"\$2"):
        _client(next_day, "2.00").run("extractor", PROMPT, "doc")


def test_the_spending_alert_compares_with_the_days_own_cap(engine: Engine) -> None:
    config = load_config(DEFAULT_CONFIG_PATH)
    assert spend_today(engine, config, date(2026, 11, 30))["extractor"].cap == Decimal("8.00")
    assert spend_today(engine, config, date(2026, 12, 9))["extractor"].cap == Decimal("2.00")
