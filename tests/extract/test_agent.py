"""The extractor agent: the model proposes, code checks every quote and date (spec: Provenance)."""

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from catalyst.extract.agent import (
    EXTRACTOR_PROMPT,
    ExtractedCatalyst,
    Extraction,
    Problem,
    catalyst_claims,
    extract_catalysts,
    model_input,
)
from catalyst.llm.client import Completion, ModelClient
from catalyst.llm.config import ModelConfig, RoleConfig
from catalyst.llm.ledger import InMemoryLedger
from catalyst.llm.pricing import Price, TokenUsage
from catalyst.llm.prompts import Prompt, load_prompt
from catalyst.schemas.enums import CatalystType, ClaimSubject, DatePrecision

TEXT = (
    "Acme Bio Reports Third Quarter 2026 Results\n"
    "Topline data from the Phase 3 RAINIER trial of zentocel in MASH are expected in the\n"
    "second half of 2027.\n"
    "The FDA assigned a PDUFA target action date of March 14, 2027 for the NDA for zentocel.\n"
    "Enrollment in the BRIGHT trial was completed in the third quarter of 2026."
)
PUBLISHED = date(2026, 10, 14)
NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
CONFIG = ModelConfig(
    version="test",
    pricing={
        "claude-sonnet-5-5": Price(
            input=Decimal(2), cache_write=Decimal(3), cache_read=Decimal(0), output=Decimal(10)
        )
    },
    roles={
        "extractor": RoleConfig(
            provider="fake", model="claude-sonnet-5-5", max_tokens=1000, daily_cap_usd=Decimal(5)
        )
    },
)


def _item(**changes: object) -> dict[str, object]:
    item: dict[str, object] = {
        "catalyst_type": "topline_readout",
        "title": "Phase 3 RAINIER topline data for zentocel in MASH",
        "drug": "zentocel",
        "indication": "MASH",
        "date_text": "second half of 2027",
        "quote": "Topline data from the Phase 3 RAINIER trial of zentocel in MASH are expected in the "
        "second half of 2027.",
    }
    return item | changes


@dataclass
class _Fake:
    answer: dict[str, object]
    name: str = "fake"
    documents: list[str] = field(default_factory=list[str])

    def complete(
        self, *, model: str, prompt: Prompt, document: str, max_tokens: int, effort: str | None
    ) -> Completion:
        self.documents.append(document)
        return Completion(
            json.dumps(self.answer), model, "req-1", "end_turn", TokenUsage(input=900, output=200)
        )


def _extract(*items: dict[str, object]) -> tuple[_Fake, Extraction]:
    fake = _Fake({"catalysts": list(items)})
    client = ModelClient(CONFIG, {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    return fake, extract_catalysts(client, TEXT, PUBLISHED)


def _run(*items: dict[str, object]) -> tuple[_Fake, list[ExtractedCatalyst]]:
    fake, extraction = _extract(*items)
    return fake, list(extraction.catalysts)


def test_the_prompt_file_loads_and_lists_only_forward_looking_types() -> None:
    prompt = load_prompt(EXTRACTOR_PROMPT[0], EXTRACTOR_PROMPT[1])
    allowed = set(
        prompt.output_schema["properties"]["catalysts"]["items"]["properties"]["catalyst_type"]["enum"]
    )
    assert allowed <= {t.value for t in CatalystType}
    assert {"earnings", "offering", "complete_response_letter"}.isdisjoint(allowed)


def test_the_model_reads_the_publication_date_then_the_text() -> None:
    assert model_input(TEXT, PUBLISHED) == f"Publication date: 2026-10-14\n\n{TEXT}"


def test_a_checked_catalyst_carries_its_quote_span_and_window() -> None:
    fake, found = _run(_item())
    assert fake.documents == [model_input(TEXT, PUBLISHED)]
    (catalyst,) = found
    assert catalyst.problems == ()
    assert catalyst.catalyst_type is CatalystType.TOPLINE_READOUT
    assert catalyst.quote is not None
    # The model wrote the line break as a space; the stored quote keeps the source's own characters.
    assert TEXT[catalyst.quote.start : catalyst.quote.end] == catalyst.quote.quote
    assert "in the\nsecond half" in catalyst.quote.quote
    assert catalyst.window is not None
    assert (catalyst.window.start, catalyst.window.end) == (date(2027, 7, 1), date(2027, 12, 31))
    assert catalyst.window.precision is DatePrecision.HALF


def test_a_quote_the_model_changed_is_caught() -> None:
    (catalyst,) = _run(_item(quote="Topline data from RAINIER are expected in the second half of 2027."))[1]
    assert catalyst.problems == (Problem.QUOTE_NOT_FOUND,)
    assert catalyst.quote is None


def test_a_date_that_is_not_in_the_quote_is_caught() -> None:
    (catalyst,) = _run(_item(date_text="first half of 2027"))[1]
    assert Problem.DATE_NOT_IN_QUOTE in catalyst.problems
    assert catalyst.window is None


def test_date_words_must_be_whole_words_in_the_quote() -> None:
    quote = "Topline data from the Phase 3 RAINIER trial of zentocel in MASH are expected in the second"
    (catalyst,) = _run(_item(date_text="the sec", quote=quote))[1]
    assert catalyst.problems == (Problem.DATE_NOT_IN_QUOTE,)


def test_a_vague_date_gets_no_window() -> None:
    quote = "Topline data from the Phase 3 RAINIER trial of zentocel in MASH are expected"
    (catalyst,) = _run(_item(date_text="expected", quote=quote))[1]
    assert catalyst.problems == (Problem.NO_WINDOW,)


def test_a_date_already_past_is_caught() -> None:
    quote = "Enrollment in the BRIGHT trial was completed in the third quarter of 2026."
    item = _item(catalyst_type="enrollment_completion", date_text="third quarter of 2026", quote=quote)
    (catalyst,) = _run(item)[1]
    assert catalyst.problems == (Problem.DATE_PASSED,)


def test_regulator_dates_and_company_dates_are_labeled_apart() -> None:
    pdufa = _item(
        catalyst_type="pdufa_date",
        date_text="March 14, 2027",
        quote="The FDA assigned a PDUFA target action date of March 14, 2027 for the NDA for zentocel.",
    )
    readout, action = _run(_item(), pdufa)[1]
    assert readout.date_confidence.value == "company_confirmed"
    assert action.date_confidence.value == "regulator_scheduled"
    assert action.window is not None
    assert action.window.precision is DatePrecision.DAY


def test_claims_are_made_only_for_catalysts_whose_quote_was_found() -> None:
    extraction = _extract(_item(), _item(quote="Words that are not in the release."))[1]
    document_id = uuid4()
    claims = catalyst_claims(extraction, document_id, NOW)
    assert len(claims) == 1
    (claim,) = claims
    assert claim.source_document_id == document_id
    assert claim.subject_type is ClaimSubject.CATALYST
    assert claim.field.startswith("catalyst:")
    assert claim.agent == "extractor"
    assert (claim.prompt_id, claim.prompt_version) == EXTRACTOR_PROMPT
    assert (claim.provider, claim.model) == ("fake", "claude-sonnet-5-5")
    assert TEXT[claim.quote_start : claim.quote_end] == claim.quote
    assert claim.value == {
        "catalyst_type": "topline_readout",
        "title": "Phase 3 RAINIER topline data for zentocel in MASH",
        "drug": "zentocel",
        "indication": "MASH",
        "date_text": "second half of 2027",
        "window_start": "2027-07-01",
        "window_end": "2027-12-31",
        "window_precision": "half",
        "date_confidence": "company_confirmed",
        "problems": [],
        "text_version": "1",
    }


@pytest.mark.parametrize("answer", [{"catalysts": []}])
def test_a_release_with_no_events_gives_none(answer: dict[str, object]) -> None:
    fake = _Fake(answer)
    client = ModelClient(CONFIG, {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    assert extract_catalysts(client, TEXT, PUBLISHED).catalysts == ()
