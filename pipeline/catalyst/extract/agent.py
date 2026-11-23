"""The catalyst extractor agent (spec: Agent architecture; ADR 0013).

The model reads one press release and proposes catalysts, each with a date phrase and a quote.
It has no tools and no database access. Plain code then checks each proposal: the quote must be
the source's own words, the date phrase must sit inside the quote, and code, not the model,
turns the phrase into a window. A proposal that fails a check keeps the reason, never a guess.

Text in the source may try to steer the model (ADR 0018, part 5c). The model reads only the
visible text; a quote must also be visible; no quote, date words, or name may hold invisible
characters; and every proposal from a source that addresses models is flagged for a person.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

from catalyst.extract.dates import DateWindow, parse_window, whole_date
from catalyst.extract.injection import hidden_letters, instruction_markers, invisible_characters
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
    QUOTE_HIDDEN = "the quote is text the page hides from readers"
    INVISIBLE_CHARACTERS = "the quote, date words, or names hold invisible or direction-changing characters"
    NON_ASCII_DIGITS = (
        "the quote or date words hold a digit from another script, which may read as another number"
    )
    SOURCE_HIDES_TEXT = "the source hides words from readers, so a person must read it"
    SOURCE_INSTRUCTS_MODELS = "the source holds text written to steer a model, so a person must read it"


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
    instructions: tuple[str, ...] = ()  # model-steering text found in the source, if any


def model_input(text: str, published: date) -> str:
    """Return what the model reads: the publication date, then the document text."""
    return f"Publication date: {published.isoformat()}\n\n{text}"


def has_words(quote: str, words: str) -> bool:
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


def check_proposal(
    item: dict[str, Any], text: str, published: date, visible: str | None = None
) -> ExtractedCatalyst:
    """Check one proposal as the model gave it: find its quote, its date words, and its window.

    `visible` is the text the model read (`visible_text`: the copy without what it hides from a
    reader, as a browser builds it); when given, the quote must be in it too.
    """
    catalyst_type = CatalystType(item["catalyst_type"])
    date_text = str(item["date_text"])
    problems: list[Problem] = []
    window: DateWindow | None = None
    span = find_quote(text, str(item["quote"]))
    if span is not None and visible is not None and find_quote(visible, span.quote) is None:
        problems.append(Problem.QUOTE_HIDDEN)
    names = (date_text, item["title"], item["drug"], item["indication"])
    if invisible_characters(span.quote if span else None, *names):
        problems.append(Problem.INVISIBLE_CHARACTERS)
    # The whole quote, not only the date words: a year beside "December 15" counts too (CP4, B1).
    # In the quote only true digits count (Bengali, fullwidth, Arabic-Indic), not the superscript
    # footnote markers filings use; the date-words rule holds a superscript beside year-less words.
    quote = span.quote if span else ""
    if any(ch.isdigit() and not ch.isascii() for ch in date_text) or any(
        ch.isdecimal() and not ch.isascii() for ch in quote
    ):
        problems.append(Problem.NON_ASCII_DIGITS)
    if span is None:
        problems.append(Problem.QUOTE_NOT_FOUND)
    else:
        date_text, window, found = read_date(span.quote, date_text, published)
        problems.extend(found)
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


def read_date(quote: str, date_text: str, published: date) -> tuple[str, DateWindow | None, list[Problem]]:
    """Read a proposal's date words in its quote: the words to keep, their window, and what is wrong.

    Words cut from a longer date in the quote are kept whole, in the quote's own characters, when
    code can tell which date is meant (`whole_date`; ADR 0061).
    """
    if not has_words(quote, date_text):
        return date_text, None, [Problem.DATE_NOT_IN_QUOTE]
    words = whole_date(quote, date_text, published)
    window = parse_window(words, published)
    if window is None:
        return words, None, [Problem.NO_WINDOW]
    return words, window, [Problem.DATE_PASSED] if window.end < published else []


_DATE_PROBLEMS = (Problem.NO_WINDOW.name.lower(), Problem.DATE_PASSED.name.lower())


def reread_date(value: Mapping[str, Any], quote: str, published: date) -> dict[str, Any] | None:
    """Return a stored claim's value with its date read by today's rules, or None when nothing changes.

    Only two kinds of claim are read again (ADR 0061): one stored with no window because its date
    words named none then, and one whose date words are cut from a longer date in its quote. A
    stored window is never replaced for the same words; `window_matches_words` checks that one.
    """
    words, listed = value.get("date_text"), value.get("problems")
    if not isinstance(words, str) or not isinstance(listed, list) or not has_words(quote, words):
        return None
    problems = [str(problem) for problem in cast(list[object], listed)]
    no_window = _DATE_PROBLEMS[0] in problems and not (value.get("window_start") or value.get("window_end"))
    kept, window, found = read_date(quote, words, published)
    if kept == words and not (no_window and window is not None):
        return None
    return dict(value) | {
        "date_text": kept,
        "window_start": window.start.isoformat() if window else None,
        "window_end": window.end.isoformat() if window else None,
        "window_precision": window.precision.value if window else None,
        "problems": [p for p in problems if p not in _DATE_PROBLEMS] + [p.name.lower() for p in found],
    }


def extract_catalysts(
    client: ModelClient,
    text: str,
    published: date,
    *,
    visible: str | None = None,
    differs: bool = False,
    role: str = ROLE,
    prompt_ref: tuple[str, str] = EXTRACTOR_PROMPT,
) -> Extraction:
    """Ask a reader for the catalysts in `text` (published on `published`) and check each one.

    `text` is the copy's `document_text`, where quotes are placed. `visible` is its
    `visible_text`: the reader sees only that, and every quote must be in it. None means the
    whole text is visible (plain text). `differs` says the visible text is not what main's
    strict reader gives (ADR 0044, decision 6): text that reader hides reached the model.
    The extractor is the default; the verifier's second reading uses its own role and prompt.
    When the text addresses models, or the page hides words from readers, every proposal is
    flagged, since no code can tell which of them the text changed.
    """
    prompt = load_prompt(*prompt_ref)
    shown = text if visible is None else visible
    result = client.run(role, prompt, model_input(shown, published))
    items: list[dict[str, Any]] = result.data["catalysts"]
    catalysts = tuple(check_proposal(item, text, published, visible) for item in items)
    # Both texts are scanned: what the reader saw could differ from the stored text (ADR 0044).
    instructions = instruction_markers(text)
    if visible is not None:
        instructions = tuple(dict.fromkeys((*instructions, *instruction_markers(visible))))
    flags = [Problem.SOURCE_INSTRUCTS_MODELS] if instructions else []
    # The page hides words; or the reader saw letters the stored text lacks, which no honest page does.
    if differs or (visible is not None and (hidden_letters(text, visible) or hidden_letters(visible, text))):
        flags.append(Problem.SOURCE_HIDES_TEXT)
    if flags:
        catalysts = tuple(replace(c, problems=(*c.problems, *flags)) for c in catalysts)
    return Extraction(
        catalysts=catalysts,
        provider=result.provider,
        model=result.model,
        prompt_id=result.prompt_id,
        prompt_version=result.prompt_version,
        instructions=instructions,
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
        # Stored claims keep short codes such as "quote_not_found" (format of PR #16).
        "problems": [problem.name.lower() for problem in catalyst.problems],
    }


def catalyst_claims(
    extraction: Extraction, source_document_id: UUID, extracted_at: datetime, *, agent: str = ROLE
) -> list[Claim]:
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
                agent=agent,
                prompt_id=extraction.prompt_id,
                prompt_version=extraction.prompt_version,
                provider=extraction.provider,
                model=extraction.model,
                extracted_at=extracted_at,
            )
        )
    return claims
