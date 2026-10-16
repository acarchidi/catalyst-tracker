"""The Anthropic adapter against the provider contract, with API-shaped responses.

Response bodies follow the Messages API documentation. A recorded live response replaces them
once the owner has created CATALYST_ANTHROPIC_API_KEY (docs/setup.md Part 2).
"""

import json
from typing import Any

import httpx2
import pytest

from catalyst.llm.anthropic_adapter import AnthropicAdapter, api_schema
from catalyst.llm.client import ProviderAdapter
from llm.provider_contract import PROMPT, ProviderContract

USAGE = {
    "input_tokens": 412,
    "output_tokens": 31,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 290,
}


def _message(
    content: list[dict[str, Any]], stop_reason: str, model: str = "claude-sonnet-5-5"
) -> dict[str, Any]:
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": USAGE,
    }


class TestAnthropicContract(ProviderContract):
    """Run the shared contract against the Anthropic adapter."""

    def adapter(self, replies: list[tuple[int, dict[str, Any]]]) -> ProviderAdapter:
        self._sent: list[dict[str, Any]] = []
        self._headers: list[httpx2.Headers] = []
        queue = list(replies)

        def handler(request: httpx2.Request) -> httpx2.Response:
            self._sent.append(json.loads(request.content))
            self._headers.append(request.headers)
            status, body = queue.pop(0)
            # retry-after-ms keeps the SDK's retry backoff out of test time.
            return httpx2.Response(
                status, json=body, headers={"request-id": "req_test_1", "retry-after-ms": "1"}
            )

        return AnthropicAdapter(
            api_key="test-key",
            transport=httpx2.MockTransport(handler),
            max_retries=2,
        )

    def success_body(self, text: str, model: str) -> dict[str, Any]:
        return _message([{"type": "text", "text": text}], "end_turn", model)

    def refusal_body(self) -> dict[str, Any]:
        body = _message([], "refusal")
        body["stop_details"] = {"type": "refusal", "category": None, "explanation": "declined"}
        return body

    def truncated_body(self) -> dict[str, Any]:
        return _message([{"type": "text", "text": '{"mentions_pdufa": tr'}], "max_tokens")

    def sent_requests(self) -> list[dict[str, Any]]:
        return self._sent

    def test_the_api_key_comes_from_the_constructor_only(self) -> None:
        self.adapter([(200, self.success_body("{}", self.model))]).complete(
            model=self.model, prompt=PROMPT, document="d", max_tokens=10, effort=None
        )
        assert self._headers[0]["x-api-key"] == "test-key"

    def test_the_output_schema_and_cached_system_prompt_are_sent(self) -> None:
        self.adapter([(200, self.success_body("{}", self.model))]).complete(
            model=self.model, prompt=PROMPT, document="d", max_tokens=10, effort="low"
        )
        sent = self._sent[0]
        assert sent["output_config"]["format"] == {
            "type": "json_schema",
            "schema": api_schema(PROMPT.output_schema),
        }
        assert sent["output_config"]["effort"] == "low"
        assert sent["system"][0]["text"] == PROMPT.system
        assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_api_schema_drops_constraints_the_api_does_not_accept() -> None:
    schema = api_schema(PROMPT.output_schema)
    assert schema["properties"]["quote"] == {"type": "string"}
    assert schema["additionalProperties"] is False
    assert PROMPT.output_schema["properties"]["quote"]["minLength"] == 1  # original untouched


def test_a_missing_api_key_is_a_setup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from catalyst.llm.anthropic_adapter import from_settings
    from catalyst.settings import MissingSettingError

    monkeypatch.delenv("CATALYST_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-used")
    with pytest.raises(MissingSettingError, match="CATALYST_ANTHROPIC_API_KEY"):
        from_settings()
