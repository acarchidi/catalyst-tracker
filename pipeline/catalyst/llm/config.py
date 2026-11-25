"""The versioned model config: provider, model, limits, and daily spend cap for each agent role.

Switching a role to another model or provider is a one-line change to config/models.toml,
reviewed like code (spec: Multi-provider model layer, item 3).
"""

import tomllib
from datetime import date
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from catalyst.llm.pricing import Price

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "models.toml"
PROVIDERS = frozenset({"anthropic", "fake"})
# The largest document each role may send, in estimated tokens, when config/models.toml sets no
# `max_input_tokens` for it (ADR 0038). Kept here so the limit needs no change to the owner's file;
# a value in the file wins. The largest release that ever gave a catalyst was 19,089 tokens.
# The analyst's is lower so a document under it always fits a fresh day of its $0.50 Opus cap
# (worst case about $0.48 at 30,000; about $0.53 at 40,000), and so cannot stop the sample
# check at the same record every day (ADR 0041).
DEFAULT_MAX_INPUT_TOKENS: dict[str, int] = {"extractor": 40_000, "analyst": 30_000}

# The owner allowed filings at most half of a raised cap, so the press-release backfill keeps the
# rest (owner, in writing, 2026-11-25; ADR 0081). A larger share in the file is refused.
FILINGS_SHARE_MOST = Decimal("0.5")

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class RaisedCap(BaseModel):
    """A higher daily cap the owner approved in writing, for the New York days from `first_day` to `last_day`.

    Outside those days the role's usual `daily_cap_usd` applies again, with no change to the file,
    so nobody has to remember to lower it (ADR 0065).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cap_usd: Decimal = Field(gt=0)
    first_day: date
    last_day: date
    approved: str = Field(min_length=1, description="Who approved it in writing, when, and why.")
    filings_share: Decimal = Field(
        default=Decimal(0),
        ge=0,
        le=FILINGS_SHARE_MOST,
        description="Most of this cap that 10-Q, 10-K and exhibit reads may use; 0: none (ADR 0081).",
    )
    filings_approved: str | None = Field(
        default=None, description="Who approved the filings' share in writing, when, and why."
    )

    @model_validator(mode="after")
    def _in_order(self) -> Self:
        if self.last_day < self.first_day:
            raise ValueError(
                f"a raised cap's last_day {self.last_day} is before its first_day {self.first_day}"
            )
        if self.filings_share > 0 and not self.filings_approved:
            raise ValueError("a filings_share needs filings_approved: who allowed it in writing, and when")
        return self


class RoleConfig(BaseModel):
    """How one agent role calls a model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model: str
    max_tokens: int = Field(gt=0)
    effort: Effort | None = None
    daily_cap_usd: Decimal = Field(gt=0, description="Spend limit per New York calendar day.")
    max_input_tokens: int | None = Field(
        default=None, gt=0, description="Largest document to send, in estimated tokens; None for the default."
    )
    raised_caps: tuple[RaisedCap, ...] = ()

    @model_validator(mode="after")
    def _raised_caps_apart(self) -> Self:
        ordered = sorted(self.raised_caps, key=lambda raised: raised.first_day)
        for earlier, later in pairwise(ordered):
            if later.first_day <= earlier.last_day:
                raise ValueError(
                    f"raised caps overlap: {earlier.first_day} to {earlier.last_day} and {later.first_day}"
                )
        return self

    def cap_on(self, day: date) -> Decimal:
        """Return the cap for the New York day `day`: a raised cap on its days, else the usual one."""
        for raised in self.raised_caps:
            if raised.first_day <= day <= raised.last_day:
                return raised.cap_usd
        return self.daily_cap_usd

    def filings_line(self, day: date) -> Decimal | None:
        """Return the filings' share of a raised cap on `day`, or None when no share applies that day."""
        for raised in self.raised_caps:
            if raised.first_day <= day <= raised.last_day and raised.filings_share > 0:
                return raised.cap_usd * raised.filings_share
        return None


class ModelConfig(BaseModel):
    """All roles and the prices of the models they use."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    pricing: dict[str, Price]
    roles: dict[str, RoleConfig]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for name, role in self.roles.items():
            if role.provider not in PROVIDERS:
                raise ValueError(f"role {name}: unknown provider {role.provider!r}")
            if role.model not in self.pricing:
                raise ValueError(f"role {name}: model {role.model!r} has no price in [pricing]")
        return self

    def max_input_tokens(self, role: str) -> int | None:
        """Return the largest document `role` may send, in estimated tokens, or None for no limit."""
        return self.roles[role].max_input_tokens or DEFAULT_MAX_INPUT_TOKENS.get(role)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> ModelConfig:
    """Read and validate the model config file."""
    with path.open("rb") as handle:
        return ModelConfig.model_validate(tomllib.load(handle))
