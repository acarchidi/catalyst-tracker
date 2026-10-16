"""Cost arithmetic, checked against Anthropic's published prices (pricing page, read 2026-10-16)."""

from decimal import Decimal

import pytest

from catalyst.llm.pricing import Price, TokenUsage, cost_usd

SONNET = Price(input=Decimal(2), cache_write=Decimal("2.50"), cache_read=Decimal("0.10"), output=Decimal(10))
HAIKU = Price(
    input=Decimal("0.10"),
    cache_write=Decimal("0.125"),
    cache_read=Decimal("0.01"),
    output=Decimal("0.50"),
    long_context_threshold=100_000,
    long_context=Price(
        input=Decimal("0.50"),
        cache_write=Decimal("0.625"),
        cache_read=Decimal("0.05"),
        output=Decimal("2.50"),
    ),
)


def test_one_million_of_each_token_kind_costs_the_list_prices() -> None:
    usage = TokenUsage(input=1_000_000, output=1_000_000, cache_read=1_000_000, cache_write=1_000_000)
    assert cost_usd(SONNET, usage) == Decimal("14.60")


def test_a_typical_extraction_call() -> None:
    # 12,000 input tokens and 800 output tokens on Sonnet 5.5: 0.024 + 0.008.
    assert cost_usd(SONNET, TokenUsage(input=12_000, output=800)) == Decimal("0.032")


def test_haiku_switches_to_long_context_prices_above_100k_prompt_tokens() -> None:
    short = TokenUsage(input=100_000, output=1_000)
    long = TokenUsage(input=60_000, cache_read=40_001, output=1_000)
    assert cost_usd(HAIKU, short) == Decimal("0.0105")
    # Cache reads count toward the prompt length, so this request pays long-context prices.
    assert cost_usd(HAIKU, long) == Decimal("0.03450005")


def test_zero_usage_costs_nothing() -> None:
    assert cost_usd(SONNET, TokenUsage()) == 0


@pytest.mark.parametrize("field", ["input", "output", "cache_read", "cache_write"])
def test_negative_token_counts_are_refused(field: str) -> None:
    with pytest.raises(ValueError, match="negative"):
        TokenUsage(**{field: -1})
