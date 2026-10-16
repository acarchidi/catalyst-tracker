"""The versioned model config: provider, model, limits, and daily spend cap for each agent role.

Switching a role to another model or provider is a one-line change to config/models.toml,
reviewed like code (spec: Multi-provider model layer, item 3).
"""

import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from catalyst.llm.pricing import Price

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "models.toml"
PROVIDERS = frozenset({"anthropic", "fake"})

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class RoleConfig(BaseModel):
    """How one agent role calls a model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model: str
    max_tokens: int = Field(gt=0)
    effort: Effort | None = None
    daily_cap_usd: Decimal = Field(gt=0, description="Spend limit per New York calendar day.")


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


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> ModelConfig:
    """Read and validate the model config file."""
    with path.open("rb") as handle:
        return ModelConfig.model_validate(tomllib.load(handle))
