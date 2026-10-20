"""`ModelClient`: the only way any agent calls a model (AGENTS.md: Model calls).

One call: check the role's daily spend cap, call the role's configured model through its
provider adapter, record the call and its cost, then return the output only if it validates
against the prompt's JSON Schema. The client never retries on another model.
"""

import json
import logging
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from jsonschema import Draft202012Validator

from catalyst.llm.config import ModelConfig
from catalyst.llm.ledger import SpendLedger, new_york_day
from catalyst.llm.pricing import TokenUsage, cost_usd
from catalyst.llm.prompts import Prompt
from catalyst.logs import log_alert, log_event
from catalyst.schemas import ModelCall, ModelCallStatus

CHARS_PER_TOKEN_ESTIMATE = 3  # deliberately low, so the worst-case estimate is high
# For a role's input limit: an ordinary estimate for English text, so the limit means what it says.
# The cap check above stays on its own, more cautious estimate (ADR 0038).
CHARS_PER_INPUT_TOKEN = 4

_log = logging.getLogger(__name__)


class ModelError(Exception):
    """Base class for model-call failures. `usage` is what the provider billed, if known."""

    def __init__(self, message: str, usage: TokenUsage | None = None) -> None:
        """Keep the billed usage so the failed call can still be costed."""
        super().__init__(message)
        self.usage = usage or TokenUsage()


class ModelCallError(ModelError):
    """The provider returned an error or could not be reached."""


class ModelRefusalError(ModelError):
    """The model declined the request. The claim goes to the review queue; no other model is tried."""


class ModelTruncatedError(ModelError):
    """The answer hit the token limit before it was complete."""


class ModelOutputInvalidError(ModelError):
    """The answer was not JSON, or did not match the prompt's output schema."""


class ModelSwitchedError(ModelError):
    """The provider answered with a different model than the one configured."""


class InputTooLargeError(ModelError):
    """The document is over the role's input limit, so it was never sent and nothing was billed."""

    def __init__(self, role: str, estimated_tokens: int, limit: int) -> None:
        """Keep the estimate and the limit, for the job's log."""
        super().__init__(f"{role}: the document is about {estimated_tokens} tokens; the limit is {limit}")
        self.estimated_tokens = estimated_tokens
        self.limit = limit


class SpendCapReachedError(Exception):
    """The role has reached its daily spend cap. It stops; it never moves to another model."""


@dataclass(frozen=True)
class Completion:
    """What a provider adapter returns: raw text plus billing facts."""

    text: str
    model: str
    request_id: str | None
    stop_reason: str
    usage: TokenUsage


class ProviderAdapter(Protocol):
    """One model provider. Adding a provider means one adapter that passes the contract tests."""

    name: str

    def complete(
        self, *, model: str, prompt: Prompt, document: str, max_tokens: int, effort: str | None
    ) -> Completion:
        """Send one prompt and document; return the answer text, or raise a `ModelError`."""
        ...


@dataclass(frozen=True)
class ModelResult:
    """A validated answer and everything needed to trace it (spec: Traceability)."""

    data: dict[str, Any]
    provider: str
    model: str
    prompt_id: str
    prompt_version: str
    usage: TokenUsage
    cost_usd: Decimal
    latency_ms: int
    request_id: str | None
    call_id: UUID


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ModelClient:
    """Calls models for agent roles under the rules in config/models.toml."""

    def __init__(
        self,
        config: ModelConfig,
        adapters: Mapping[str, ProviderAdapter],
        ledger: SpendLedger,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Create a client; `now` is replaceable for tests."""
        self._config = config
        self._adapters = adapters
        self._ledger = ledger
        self._now = now

    def run(self, role: str, prompt: Prompt, document: str) -> ModelResult:
        """Run `prompt` on `document` as `role` and return the validated answer."""
        settings = self._config.roles[role]
        limit = self._config.max_input_tokens(role)
        estimated = math.ceil(len(document) / CHARS_PER_INPUT_TOKEN)
        if limit is not None and estimated > limit:
            raise InputTooLargeError(role, estimated, limit)
        started_at = self._now()
        worst_case = self._check_cap(role, prompt, document, started_at)
        adapter = self._adapters[settings.provider]
        started = time.monotonic()

        def record(
            model: str, usage: TokenUsage, request_id: str | None, status: ModelCallStatus
        ) -> ModelCall:
            return self._record(
                role, settings.provider, model, prompt, usage, started, started_at, request_id, status
            )

        try:
            completion = adapter.complete(
                model=settings.model,
                prompt=prompt,
                document=document,
                max_tokens=settings.max_tokens,
                effort=settings.effort,
            )
        except ModelError as error:
            record(settings.model, error.usage, None, _status(error))
            raise
        except BaseException:
            # The request may have been billed; record the worst case so the cap still counts it.
            self._record(role, settings.provider, settings.model, prompt, TokenUsage(), started, started_at,
                         None, ModelCallStatus.ERROR, cost=worst_case)  # fmt: skip
            raise
        if completion.model != settings.model:
            record(completion.model, completion.usage, completion.request_id, ModelCallStatus.MODEL_SWITCHED)
            raise ModelSwitchedError(f"{role}: asked for {settings.model}, got {completion.model}")
        try:
            data = _validated(completion.text, prompt)
        except ModelOutputInvalidError:
            record(completion.model, completion.usage, completion.request_id, ModelCallStatus.INVALID_OUTPUT)
            raise
        call = record(completion.model, completion.usage, completion.request_id, ModelCallStatus.OK)
        return ModelResult(
            data=data,
            provider=settings.provider,
            model=completion.model,
            prompt_id=prompt.id,
            prompt_version=prompt.version,
            usage=completion.usage,
            cost_usd=call.cost_usd,
            latency_ms=call.latency_ms,
            request_id=completion.request_id,
            call_id=call.id,
        )

    def model_for(self, role: str) -> str:
        """Return the model `role` is configured to call."""
        return self._config.roles[role].model

    def _check_cap(self, role: str, prompt: Prompt, document: str, at: datetime) -> Decimal:
        """Stop if the next call could take the role past its cap; return that call's worst-case cost."""
        settings = self._config.roles[role]
        characters = len(prompt.system) + len(document) + len(json.dumps(prompt.output_schema))
        worst_case = cost_usd(
            self._config.pricing[settings.model],
            TokenUsage(input=math.ceil(characters / CHARS_PER_TOKEN_ESTIMATE), output=settings.max_tokens),
        )
        spent = self._ledger.spent(role, new_york_day(at))
        if spent + worst_case > settings.daily_cap_usd:
            cap = settings.daily_cap_usd
            log_alert(_log, "spend_cap_reached", role=role, spent_usd=str(spent), cap_usd=str(cap))
            raise SpendCapReachedError(
                f"{role} has spent ${spent} of its ${cap} daily cap; the next call could cost "
                f"${worst_case}. Stopping until tomorrow (New York time)."
            )
        return worst_case

    def _cost(self, model: str, usage: TokenUsage) -> Decimal:
        """Cost at the model's price; a model with no configured price is costed at the highest one."""
        price = self._config.pricing.get(model)
        if price:
            return cost_usd(price, usage)
        return max((cost_usd(p, usage) for p in self._config.pricing.values()), default=Decimal(0))

    def _record(
        self,
        role: str,
        provider: str,
        model: str,
        prompt: Prompt,
        usage: TokenUsage,
        started: float,
        started_at: datetime,
        request_id: str | None,
        status: ModelCallStatus,
        cost: Decimal | None = None,
    ) -> ModelCall:
        if cost is None:
            cost = self._cost(model, usage)
        call = ModelCall(
            role=role,
            provider=provider,
            model=model,
            prompt_id=prompt.id,
            prompt_version=prompt.version,
            input_tokens=usage.input,
            output_tokens=usage.output,
            cache_read_tokens=usage.cache_read,
            cache_write_tokens=usage.cache_write,
            cost_usd=cost,
            latency_ms=int((time.monotonic() - started) * 1000),
            request_id=request_id,
            status=status,
            called_at=started_at,
            config_version=self._config.version,
        )
        self._ledger.record(call)
        log_event(_log, "model_call", **call.model_dump(mode="json", exclude={"id"}))
        return call


def _status(error: ModelError) -> ModelCallStatus:
    statuses: dict[type[ModelError], ModelCallStatus] = {
        ModelRefusalError: ModelCallStatus.REFUSED,
        ModelTruncatedError: ModelCallStatus.TRUNCATED,
        ModelOutputInvalidError: ModelCallStatus.INVALID_OUTPUT,
    }
    return statuses.get(type(error), ModelCallStatus.ERROR)


def _validated(text: str, prompt: Prompt) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ModelOutputInvalidError(f"answer is not JSON: {error.msg}") from error
    validator = Draft202012Validator(prompt.output_schema)
    errors = sorted(validator.iter_errors(data), key=str)  # pyright: ignore[reportUnknownMemberType]
    if errors:
        raise ModelOutputInvalidError(f"answer breaks the output schema: {errors[0].message}")
    if not isinstance(data, dict):
        raise ModelOutputInvalidError("answer must be a JSON object")
    return data  # pyright: ignore[reportUnknownVariableType]
