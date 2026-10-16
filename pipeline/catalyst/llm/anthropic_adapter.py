"""Anthropic adapter for `ModelClient`: structured JSON output through the Messages API.

The key comes only from CATALYST_ANTHROPIC_API_KEY, so coding sessions never bill to it.
No server-side fallback is requested: a refusal is reported, never re-run on another model
(AGENTS.md: a role never switches to a pricier model).
"""

import copy
import os
from typing import Any

import anthropic
import httpx2

from catalyst.llm.client import Completion, ModelCallError, ModelRefusalError, ModelTruncatedError
from catalyst.llm.pricing import TokenUsage
from catalyst.llm.prompts import Prompt
from catalyst.settings import catalyst_anthropic_api_key

ANTHROPIC_API_URL = "https://api.anthropic.com"
TIMEOUT_SECONDS = 600.0


def api_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `schema` in the form the JSON-schema output format accepts.

    The SDK's converter moves unsupported limits (lengths, patterns, numeric bounds) into
    descriptions. `ModelClient` still validates every answer against the full schema.
    """
    return anthropic.transform_schema(copy.deepcopy(schema))


def _usage(usage: anthropic.types.Usage) -> TokenUsage:
    return TokenUsage(
        input=usage.input_tokens,
        output=usage.output_tokens,
        cache_read=usage.cache_read_input_tokens or 0,
        cache_write=usage.cache_creation_input_tokens or 0,
    )


class AnthropicAdapter:
    """Calls Claude models; implements `catalyst.llm.client.ProviderAdapter`."""

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        transport: httpx2.BaseTransport | None = None,
        max_retries: int = 2,
    ) -> None:
        """Create the adapter. `transport` is replaceable for tests.

        The API address is fixed, so the product key cannot follow an ANTHROPIC_BASE_URL set for
        some other tool; ANTHROPIC_CUSTOM_HEADERS would add headers to every request, so it is refused.
        """
        if os.environ.get("ANTHROPIC_CUSTOM_HEADERS"):
            raise RuntimeError("ANTHROPIC_CUSTOM_HEADERS is set; unset it before running product model calls")
        http_client = anthropic.DefaultHttpxClient(transport=transport) if transport else None
        self._client = anthropic.Anthropic(
            api_key=api_key,
            base_url=ANTHROPIC_API_URL,
            max_retries=max_retries,
            timeout=TIMEOUT_SECONDS,
            http_client=http_client,
        )

    def complete(
        self, *, model: str, prompt: Prompt, document: str, max_tokens: int, effort: str | None
    ) -> Completion:
        """Send the prompt's system text (cached) and the document; return the JSON answer text."""
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": api_schema(prompt.output_schema)}
        }
        if effort:
            output_config["effort"] = effort
        try:
            message = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": prompt.system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": document}],
                output_config=output_config,  # pyright: ignore[reportArgumentType]
            )
        except anthropic.APIStatusError as error:
            raise ModelCallError(f"Anthropic returned HTTP {error.status_code}: {error.message}") from error
        except anthropic.APIConnectionError as error:
            raise ModelCallError(f"could not reach Anthropic: {error}") from error
        usage = _usage(message.usage)
        if message.stop_reason == "refusal":
            raise ModelRefusalError(f"{model} declined the request", usage=usage)
        if message.stop_reason in ("max_tokens", "model_context_window_exceeded"):
            raise ModelTruncatedError(f"{model} stopped early: {message.stop_reason}", usage=usage)
        if message.stop_reason != "end_turn":
            raise ModelCallError(
                f"{model} stopped for an unexpected reason: {message.stop_reason}", usage=usage
            )
        text = "".join(block.text for block in message.content if block.type == "text")
        return Completion(
            text=text,
            model=message.model,
            request_id=message._request_id,  # pyright: ignore[reportPrivateUsage]
            stop_reason=message.stop_reason or "",
            usage=usage,
        )


def from_settings() -> AnthropicAdapter:
    """Build the adapter with the product's own key from the environment."""
    return AnthropicAdapter(api_key=catalyst_anthropic_api_key())
