"""The gold set itself: every entry must be checkable against its recorded press release."""

import hashlib
import json
from dataclasses import dataclass, field

import pytest

from catalyst.extract.agent import EXTRACTOR_PROMPT
from catalyst.extract.evaluate import EVALS, GoldDocument, evaluate, load_gold
from catalyst.extract.text import document_text, find_quote
from catalyst.llm.client import Completion, ModelClient
from catalyst.llm.config import load_config
from catalyst.llm.ledger import InMemoryLedger
from catalyst.llm.pricing import TokenUsage
from catalyst.llm.prompts import Prompt, load_prompt

GOLD = load_gold(EVALS / "gold.jsonl")


def _text(document: GoldDocument) -> str:
    return document_text((EVALS / "documents" / f"{document.sha256}.htm").read_bytes(), document.content_type)


def test_the_gold_set_has_documents_with_and_without_events() -> None:
    assert len(GOLD) == 12
    assert sum(len(d.catalysts) for d in GOLD) == 20
    assert sum(not d.catalysts for d in GOLD) == 6


@pytest.mark.parametrize("document", GOLD, ids=[d.sha256[:12] for d in GOLD])
def test_each_copy_is_the_document_its_name_says(document: GoldDocument) -> None:
    content = (EVALS / "documents" / f"{document.sha256}.htm").read_bytes()
    assert hashlib.sha256(content).hexdigest() == document.sha256


@pytest.mark.parametrize("document", GOLD, ids=[d.sha256[:12] for d in GOLD])
def test_each_gold_quote_is_in_its_document_and_holds_its_date_words(document: GoldDocument) -> None:
    text = _text(document)
    for event in document.catalysts:
        span = find_quote(text, event.quote)
        assert span is not None, event.quote
        assert find_quote(span.quote, event.date_text) is not None, event.date_text


def test_every_gold_type_is_one_the_prompt_may_answer() -> None:
    prompt = load_prompt(*EXTRACTOR_PROMPT)
    allowed = set(
        prompt.output_schema["properties"]["catalysts"]["items"]["properties"]["catalyst_type"]["enum"]
    )
    assert {e.catalyst_type.value for d in GOLD for e in d.catalysts} <= allowed


@dataclass
class _Echo:
    """A stand-in model that answers with the gold events themselves, in order."""

    answers: list[str]
    name: str = "anthropic"
    calls: list[str] = field(default_factory=list[str])

    def complete(
        self, *, model: str, prompt: Prompt, document: str, max_tokens: int, effort: str | None
    ) -> Completion:
        self.calls.append(document)
        return Completion(
            self.answers[len(self.calls) - 1], model, "r", "end_turn", TokenUsage(input=10, output=10)
        )


def test_answering_with_the_gold_events_scores_one_everywhere() -> None:
    answers = [
        json.dumps(
            {
                "catalysts": [
                    {
                        "catalyst_type": e.catalyst_type.value,
                        "title": "t",
                        "drug": e.drug,
                        "indication": None,
                        "date_text": e.date_text,
                        "quote": e.quote,
                    }
                    for e in d.catalysts
                ]
            }
        )
        for d in GOLD
    ]
    echo = _Echo(answers)
    config = load_config()
    client = ModelClient(config, {"anthropic": echo}, InMemoryLedger())
    scores, per_document = evaluate(client, GOLD, EVALS / "documents")
    assert len(echo.calls) == len(GOLD)
    assert len(per_document) == len(GOLD)
    assert scores.recall == scores.precision == scores.window == scores.drug == 1.0
