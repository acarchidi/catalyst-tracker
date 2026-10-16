"""ModelClient: spend caps, no model switching, validated output, every call logged with its cost.

Spec rules tested here (Multi-provider model layer; Model assignments, Cost guardrails):
- each role has a daily spend cap; at the cap it stops and alerts, and never switches model;
- a call returns validated JSON plus provider, model, cost, and latency;
- every call, including failed ones, is recorded with its cost.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from catalyst.llm.client import (
    Completion,
    ModelClient,
    ModelOutputInvalidError,
    ModelRefusalError,
    ModelSwitchedError,
    SpendCapReachedError,
)
from catalyst.llm.config import ModelConfig
from catalyst.llm.ledger import InMemoryLedger
from catalyst.llm.pricing import TokenUsage
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
        "properties": {"mentions_pdufa": {"type": "boolean"}, "quote": {"type": "string", "minLength": 1}},
    },
)
CONFIG = ModelConfig.model_validate(
    {
        "version": "test-1",
        "pricing": {
            "claude-sonnet-5-5": {"input": "2", "cache_write": "2.50", "cache_read": "0.10", "output": "10"},
            "claude-opus-5-5": {"input": "4", "cache_write": "5", "cache_read": "0.20", "output": "20"},
        },
        "roles": {
            "extractor": {
                "provider": "fake",
                "model": "claude-sonnet-5-5",
                "max_tokens": 1000,
                "daily_cap_usd": "1.00",
            }
        },
    }
)
NOW = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
GOOD_JSON = json.dumps({"mentions_pdufa": True, "quote": "PDUFA date November 30th"})


@dataclass
class FakeAdapter:
    """Returns scripted completions and records which models it was asked for."""

    name: str = "fake"
    replies: list[Completion | Exception] = field(default_factory=list[Completion | Exception])
    models_requested: list[str] = field(default_factory=list[str])

    def complete(
        self, *, model: str, prompt: Prompt, document: str, max_tokens: int, effort: str | None
    ) -> Completion:
        del prompt, document, max_tokens, effort
        self.models_requested.append(model)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _completion(text: str = GOOD_JSON, model: str = "claude-sonnet-5-5", **usage: int) -> Completion:
    return Completion(
        text=text,
        model=model,
        request_id="req_1",
        stop_reason="end_turn",
        usage=TokenUsage(**({"input": 10_000, "output": 500} | usage)),
    )


def _client(adapter: FakeAdapter, ledger: InMemoryLedger | None = None) -> tuple[ModelClient, InMemoryLedger]:
    ledger = ledger or InMemoryLedger()
    return ModelClient(CONFIG, {"fake": adapter}, ledger, now=lambda: NOW), ledger


def test_a_call_returns_validated_json_with_provider_model_cost_and_latency() -> None:
    client, _ = _client(FakeAdapter(replies=[_completion()]))
    result = client.run("extractor", PROMPT, "…povetacicept PDUFA date November 30th…")
    assert result.data == {"mentions_pdufa": True, "quote": "PDUFA date November 30th"}
    assert result.provider == "fake"
    assert result.model == "claude-sonnet-5-5"
    assert result.prompt_id == "demo"
    assert result.prompt_version == "1.0.0"
    assert result.cost_usd == Decimal("0.025")  # 10,000 x $2 + 500 x $10, per million
    assert result.latency_ms >= 0


def test_every_call_is_recorded_with_its_cost(caplog: pytest.LogCaptureFixture) -> None:
    client, ledger = _client(FakeAdapter(replies=[_completion()]))
    with caplog.at_level(logging.INFO, logger="catalyst"):
        client.run("extractor", PROMPT, "doc")
    assert [call.status for call in ledger.calls] == ["ok"]
    assert ledger.calls[0].cost_usd == Decimal("0.025")
    assert any(r.getMessage() == "model_call" for r in caplog.records)


def test_output_that_breaks_the_schema_is_refused_but_still_costed() -> None:
    bad = json.dumps({"mentions_pdufa": True, "quote": ""})  # minLength is checked here, not by the API
    client, ledger = _client(FakeAdapter(replies=[_completion(bad)]))
    with pytest.raises(ModelOutputInvalidError):
        client.run("extractor", PROMPT, "doc")
    assert [call.status for call in ledger.calls] == ["invalid_output"]
    assert ledger.calls[0].cost_usd > 0


def test_output_that_is_not_json_is_refused() -> None:
    client, ledger = _client(FakeAdapter(replies=[_completion("I think yes.")]))
    with pytest.raises(ModelOutputInvalidError):
        client.run("extractor", PROMPT, "doc")
    assert ledger.calls[0].status == "invalid_output"


def test_a_refusal_is_recorded_and_raised_never_retried_on_another_model() -> None:
    adapter = FakeAdapter(replies=[ModelRefusalError("declined", usage=TokenUsage(input=100, output=5))])
    client, ledger = _client(adapter)
    with pytest.raises(ModelRefusalError):
        client.run("extractor", PROMPT, "doc")
    assert adapter.models_requested == ["claude-sonnet-5-5"]
    assert ledger.calls[0].status == "refused"
    assert ledger.calls[0].cost_usd == Decimal("0.00025")


def test_a_reply_from_a_different_model_is_rejected() -> None:
    client, ledger = _client(FakeAdapter(replies=[_completion(model="claude-opus-5-5")]))
    with pytest.raises(ModelSwitchedError):
        client.run("extractor", PROMPT, "doc")
    assert ledger.calls[0].status == "model_switched"
    assert ledger.calls[0].model == "claude-opus-5-5"
    assert ledger.calls[0].cost_usd == Decimal("0.05")  # costed at the price of the model that answered


def test_a_role_at_its_cap_stops_before_calling_and_alerts(caplog: pytest.LogCaptureFixture) -> None:
    adapter = FakeAdapter(replies=[_completion()])
    ledger = InMemoryLedger()
    ledger.add_spend("extractor", NOW, Decimal("0.99"))
    client, _ = _client(adapter, ledger)
    with (
        caplog.at_level(logging.ERROR, logger="catalyst"),
        pytest.raises(SpendCapReachedError, match="extractor"),
    ):
        client.run("extractor", PROMPT, "doc")
    assert adapter.models_requested == []
    assert any(r.getMessage() == "spend_cap_reached" and r.levelno == logging.ERROR for r in caplog.records)


def test_the_cap_check_counts_the_worst_case_cost_of_the_next_call() -> None:
    # 1000 output tokens x $10/M = $0.01 worst case output, plus the input estimate.
    ledger = InMemoryLedger()
    ledger.add_spend("extractor", NOW, Decimal("0.985"))
    client, _ = _client(FakeAdapter(replies=[_completion()]), ledger)
    with pytest.raises(SpendCapReachedError):
        client.run("extractor", PROMPT, "x" * 9_000)


def test_spend_from_yesterday_new_york_time_does_not_count() -> None:
    ledger = InMemoryLedger()
    # 03:59 UTC on Oct 16 is 23:59 on Oct 15 in New York.
    ledger.add_spend("extractor", datetime(2026, 10, 16, 3, 59, tzinfo=UTC), Decimal("5"))
    client, _ = _client(FakeAdapter(replies=[_completion()]), ledger)
    assert client.run("extractor", PROMPT, "doc").data["mentions_pdufa"] is True


def test_caps_are_per_role() -> None:
    ledger = InMemoryLedger()
    ledger.add_spend("analyst", NOW, Decimal("500"))
    client, _ = _client(FakeAdapter(replies=[_completion()]), ledger)
    client.run("extractor", PROMPT, "doc")


def test_an_unknown_role_is_refused() -> None:
    client, _ = _client(FakeAdapter())
    with pytest.raises(KeyError, match="no_such_role"):
        client.run("no_such_role", PROMPT, "doc")
