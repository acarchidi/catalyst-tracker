"""The catalyst extractor agent (spec: Agent architecture; ADR 0013).

The model reads one press release and proposes catalysts, each with a date phrase and a quote.
It has no tools and no database access. Plain code then checks each proposal: the quote must be
the source's own words, the date phrase must sit inside the quote, and code, not the model,
turns the phrase into a window. A proposal that fails a check keeps the reason, never a guess.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from catalyst.extract.dates import DateWindow, parse_window
from catalyst.extract.text import TEXT_VERSION, QuoteSpan, find_quote
from catalyst.llm.client import ModelClient
from catalyst.llm.prompts import load_prompt
from catalyst.schemas import Claim
from catalyst.schemas.enums import CatalystType, ClaimSubject, DateConfidence

ROLE = "extractor"
EXTRACTOR_PROMPT = ("extract_catalysts", "1.0.0")
# Dates an agency sets (spec: date confidence labels); the rest are the company's own plans.
_REGULATOR_SET = frozenset({CatalystType.PDUFA_DATE, CatalystType.ADVISORY_COMMITTEE})


class Problem(StrEnum):
    """Why a proposed catalyst cannot publish as it stands."""

    QUOTE_NOT_FOUND = "the quote is not in the source"
    DATE_NOT_IN_QUOTE = "the date words are not in the quote"
    NO_WINDOW = "the date words name no window"
    DATE_PASSED = "the window ended before the document was published"


@dataclass(frozen=True)
class ExtractedCatalyst:
    """One proposed catalyst after the code checks."""

    catalyst_type: CatalystType
    title: str
    drug: str | None
    indication: str | None
    date_text: str
    quote: QuoteSpan | None
    window: DateWindow | None
    date_confidence: DateConfidence
    problems: tuple[Problem, ...]


@dataclass(frozen=True)
class Extraction:
    """Everything one model call proposed for one document, and which model made it."""

    catalysts: tuple[ExtractedCatalyst, ...]
    provider: str
    model: str
    prompt_id: str
    prompt_version: str


def model_input(text: str, published: date) -> str:
    """Return what the model reads: the publication date, then the document text."""
    return f"Publication date: {published.isoformat()}\n\n{text}"


def _has_words(quote: str, words: str) -> bool:
    """Return whether `words` appear in `quote` as whole words, in order; only spacing may differ."""
    parts = words.split()
    if not parts:
        return False
    return re.search(r"(?<!\w)" + r"\s+".join(map(re.escape, parts)) + r"(?!\w)", quote) is not None


def claim_field(value: dict[str, Any]) -> str:
    """Return the claim's field: "catalyst:" plus a short fingerprint of the proposed event.

    Two events stated in one sentence share a quote, so the field tells their claims apart on
    the claim table's unique key (review of PR #16). The same proposal always gets the same
    field, so a re-run still stores it once.
    """
    event = {key: value[key] for key in ("catalyst_type", "title", "drug", "indication", "date_text")}
    digest = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()[:16]
    return f"catalyst:{digest}"


def _checked(item: dict[str, Any], text: str, published: date) -> ExtractedCatalyst:
    catalyst_type = CatalystType(item["catalyst_type"])
    date_text = str(item["date_text"])
    problems: list[Problem] = []
    window: DateWindow | None = None
    span = find_quote(text, str(item["quote"]))
    if span is None:
        problems.append(Problem.QUOTE_NOT_FOUND)
    elif not _has_words(span.quote, date_text):
        problems.append(Problem.DATE_NOT_IN_QUOTE)
    else:
        window = parse_window(date_text, published)
        if window is None:
            problems.append(Problem.NO_WINDOW)
        elif window.end < published:
            problems.append(Problem.DATE_PASSED)
    regulator = catalyst_type in _REGULATOR_SET
    return ExtractedCatalyst(
        catalyst_type=catalyst_type,
        title=str(item["title"]),
        drug=item["drug"],
        indication=item["indication"],
        date_text=date_text,
        quote=span,
        window=window,
        date_confidence=DateConfidence.REGULATOR_SCHEDULED if regulator else DateConfidence.COMPANY_CONFIRMED,
        problems=tuple(problems),
    )


def extract_catalysts(client: ModelClient, text: str, published: date) -> Extraction:
    """Ask the extractor for the catalysts in `text` (published on `published`) and check each one."""
    prompt = load_prompt(*EXTRACTOR_PROMPT)
    result = client.run(ROLE, prompt, model_input(text, published))
    items: list[dict[str, Any]] = result.data["catalysts"]
    return Extraction(
        catalysts=tuple(_checked(item, text, published) for item in items),
        provider=result.provider,
        model=result.model,
        prompt_id=result.prompt_id,
        prompt_version=result.prompt_version,
    )


def proposal_value(catalyst: ExtractedCatalyst) -> dict[str, Any]:
    """Return a checked proposal as JSON: the claim's value, and what the job reports."""
    window = catalyst.window
    return {
        "catalyst_type": catalyst.catalyst_type.value,
        "title": catalyst.title,
        "drug": catalyst.drug,
        "indication": catalyst.indication,
        "date_text": catalyst.date_text,
        "window_start": window.start.isoformat() if window else None,
        "window_end": window.end.isoformat() if window else None,
        "window_precision": window.precision.value if window else None,
        "date_confidence": catalyst.date_confidence.value,
        "problems": [problem.value for problem in catalyst.problems],
    }


def catalyst_claims(extraction: Extraction, source_document_id: UUID, extracted_at: datetime) -> list[Claim]:
    """Return one claim per proposed catalyst whose quote was found in the source.

    Proposals whose quote is not the source's words make no claim: a claim always carries a
    verbatim quote (spec: Provenance). Other problems travel with the claim to review.
    """
    claims: list[Claim] = []
    for catalyst in extraction.catalysts:
        if catalyst.quote is None:
            continue
        value = proposal_value(catalyst) | {"text_version": TEXT_VERSION}
        claims.append(
            Claim(
                source_document_id=source_document_id,
                subject_type=ClaimSubject.CATALYST,
                field=claim_field(value),
                value=value,
                quote=catalyst.quote.quote,
                quote_start=catalyst.quote.start,
                quote_end=catalyst.quote.end,
                agent=ROLE,
                prompt_id=extraction.prompt_id,
                prompt_version=extraction.prompt_version,
                provider=extraction.provider,
                model=extraction.model,
                extracted_at=extracted_at,
            )
        )
    return claims
