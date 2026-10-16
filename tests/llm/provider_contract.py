"""The contract every provider adapter must pass.

Spec: "Adding a provider means writing one adapter and passing its contract tests; no business
code changes."

A provider's test module subclasses `ProviderContract` and supplies a scripted backend.
"""

import json
from abc import ABC, abstractmethod
from typing import Any

import pytest

from catalyst.llm.client import (
    Completion,
    ModelCallError,
    ModelRefusalError,
    ModelTruncatedError,
    ProviderAdapter,
)
from catalyst.llm.prompts import Prompt

PROMPT = Prompt(
    id="demo",
    version="1.0.0",
    purpose="Say whether a document mentions a PDUFA date.",
    eval_set="tests/evals/demo.jsonl",
    tested_models=["claude-sonnet-5-5"],
    system="Answer only from the document.",
    output_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["mentions_pdufa", "quote"],
        "properties": {
            "mentions_pdufa": {"type": "boolean"},
            "quote": {"type": "string", "minLength": 1, "maxLength": 500},
        },
    },
)
DOCUMENT = "Vertex expects an FDA decision; povetacicept PDUFA date November 30th."
ANSWER = {"mentions_pdufa": True, "quote": "povetacicept PDUFA date November 30th"}


class ProviderContract(ABC):
    """Behaviour every adapter must show. Subclasses script the provider's replies."""

    model = "claude-sonnet-5-5"

    @abstractmethod
    def adapter(self, replies: list[tuple[int, dict[str, Any]]]) -> ProviderAdapter:
        """Build an adapter whose backend answers with `replies` (HTTP status, body) in order."""

    @abstractmethod
    def success_body(self, text: str, model: str) -> dict[str, Any]:
        """Return a provider response whose answer is `text`."""

    @abstractmethod
    def refusal_body(self) -> dict[str, Any]:
        """Return a provider response that declines the request."""

    @abstractmethod
    def truncated_body(self) -> dict[str, Any]:
        """Return a provider response cut off by the token limit."""

    @abstractmethod
    def sent_requests(self) -> list[dict[str, Any]]:
        """Return the JSON bodies the adapter sent, in order."""

    def _complete(self, adapter: ProviderAdapter) -> Completion:
        return adapter.complete(
            model=self.model, prompt=PROMPT, document=DOCUMENT, max_tokens=1000, effort="low"
        )

    def test_success_returns_text_model_usage_and_request_id(self) -> None:
        completion = self._complete(self.adapter([(200, self.success_body(json.dumps(ANSWER), self.model))]))
        assert json.loads(completion.text) == ANSWER
        assert completion.model == self.model
        assert completion.usage.input > 0
        assert completion.usage.output > 0
        assert completion.request_id

    def test_the_requested_model_and_document_are_sent(self) -> None:
        self._complete(self.adapter([(200, self.success_body(json.dumps(ANSWER), self.model))]))
        sent = self.sent_requests()
        assert len(sent) == 1
        assert sent[0]["model"] == self.model
        assert DOCUMENT in json.dumps(sent[0])

    def test_no_fallback_to_another_model_is_requested(self) -> None:
        self._complete(self.adapter([(200, self.success_body(json.dumps(ANSWER), self.model))]))
        body = json.dumps(self.sent_requests()[0])
        assert "fallback" not in body

    def test_a_refusal_raises_with_usage(self) -> None:
        with pytest.raises(ModelRefusalError) as caught:
            self._complete(self.adapter([(200, self.refusal_body())]))
        assert caught.value.usage.input > 0

    def test_a_truncated_answer_raises(self) -> None:
        with pytest.raises(ModelTruncatedError):
            self._complete(self.adapter([(200, self.truncated_body())]))

    @pytest.mark.parametrize("status", [400, 401, 404])
    def test_client_errors_raise_without_retrying(self, status: int) -> None:
        with pytest.raises(ModelCallError):
            self._complete(
                self.adapter([(status, {"type": "error", "error": {"type": "x", "message": "no"}})])
            )
        assert len(self.sent_requests()) == 1

    def test_server_errors_raise_after_bounded_retries(self) -> None:
        error = (529, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
        with pytest.raises(ModelCallError):
            self._complete(self.adapter([error] * 10))
        assert 1 <= len(self.sent_requests()) <= 3
