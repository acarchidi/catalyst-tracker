"""Token prices and cost arithmetic. Prices live in config/models.toml, in US dollars per million tokens."""

from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

PER_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class TokenUsage:
    """Tokens billed for one call, by kind."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def __post_init__(self) -> None:
        """Refuse negative counts."""
        if min(self.input, self.output, self.cache_read, self.cache_write) < 0:
            raise ValueError("token counts cannot be negative")

    @property
    def prompt_tokens(self) -> int:
        """All input-side tokens, which is what long-context price tiers count."""
        return self.input + self.cache_read + self.cache_write


class Price(BaseModel):
    """Prices for one model, in dollars per million tokens."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input: Decimal = Field(ge=0)
    cache_write: Decimal = Field(ge=0, description="5-minute cache writes.")
    cache_read: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)
    long_context_threshold: int | None = Field(
        default=None, description="Prompts over this many tokens pay `long_context` prices."
    )
    long_context: "Price | None" = None


def cost_usd(price: Price, usage: TokenUsage) -> Decimal:
    """Return the dollar cost of `usage` at `price`, applying a long-context tier when one applies."""
    if (
        price.long_context
        and price.long_context_threshold
        and usage.prompt_tokens > price.long_context_threshold
    ):
        price = price.long_context
    total = (
        usage.input * price.input
        + usage.output * price.output
        + usage.cache_read * price.cache_read
        + usage.cache_write * price.cache_write
    )
    return (total / PER_MILLION).normalize()
