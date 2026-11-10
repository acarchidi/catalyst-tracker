"""Plain-code parts of a catalyst's indication (ADR 0053, decision 2; owner decision 2026-11-10).

A first model picks the sentence that states which disease a catalyst concerns, and a second
model checks it (`catalyst.extract.disease`). Plain code does everything a model need not:
it offers the candidate sentences, and it holds every answer to the hard checks. A sentence is
offered only when it is the stored source's own text, visible to readers, names the drug, and
holds no invisible characters. An answer stands only when its disease is written in that
sentence as given, holds no brackets, and names exactly one MeSH disease (`catalyst.mesh`, with
the words after the term checked too). Words are compared as `normal_words` reads them.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

from catalyst.extract.agent import has_words
from catalyst.extract.injection import invisible_characters
from catalyst.extract.text import QuoteSpan, find_quote
from catalyst.mesh import Descriptor, MeshIndex, Miss, normal_words

# A sentence ends at a full stop, question or exclamation mark, or semicolon before a space, at a
# line break, or at a bullet; press releases list milestones as bullets with no full stop.
_BREAK = re.compile(r"(?<=[.!?;])\s+|[\n\u2028\u2029]+|[\u2022\u25cf\u25e6\u25aa]")
MAX_CANDIDATES = 20
MAX_SENTENCE_CHARS = 1000
MIN_DRUG_LETTERS = 3
COMMON = frozenset(
    {"the", "a", "an", "and", "or", "of", "in", "for", "with", "to", "is", "it", "its", "this", "that"}
)


class IndicationMiss(StrEnum):
    """Why an indication cannot stand (beyond the MeSH reasons in `Miss`)."""

    UNUSABLE_DRUG = "the drug's name is too short or too common to find in a sentence"
    NO_CANDIDATE = "no visible sentence of the source names the drug"
    NO_SENTENCE = "the reader found no sentence stating the disease"
    NOT_A_CANDIDATE = "the reader's sentence number is not one of the candidates"
    DISEASE_NOT_IN_SENTENCE = "the disease is not written in the sentence as given"
    BRACKETS = "the disease's words hold brackets"
    NOT_CONFIRMED = "the second reader did not confirm the sentence states the disease"


@dataclass(frozen=True)
class Found:
    """The sentence that states the indication, the disease's words in it, and the MeSH disease."""

    sentence: QuoteSpan
    words: str
    descriptor: Descriptor


def sentences(text: str) -> Iterator[tuple[int, int]]:
    """Yield each sentence of `text` as (start, end), trimmed, skipping empty ones."""
    start = 0
    for found in [*_BREAK.finditer(text), None]:
        end = found.start() if found else len(text)
        piece = text[start:end]
        if piece.strip():
            lead = len(piece) - len(piece.lstrip())
            yield start + lead, start + len(piece.rstrip())
        if found:
            start = found.end()


def usable_drug(drug: str) -> bool:
    """Say whether a drug name can be found in a sentence: three letters or digits, not only common words."""
    words = normal_words(drug)
    return sum(ch.isalnum() for ch in "".join(words)) >= MIN_DRUG_LETTERS and not set(words) <= COMMON


def candidates(text: str, visible: str, drug: str) -> list[QuoteSpan]:
    """Return the sentences a reader may choose from: visible, naming the drug, with no hidden characters."""
    drug_words = normal_words(drug)
    found: list[QuoteSpan] = []
    for start, end in sentences(text):
        sentence = text[start:end]
        if len(sentence) > MAX_SENTENCE_CHARS or invisible_characters(sentence):
            continue
        words = normal_words(sentence)
        if not any(words[i : i + len(drug_words)] == drug_words for i in range(len(words))):
            continue
        if find_quote(visible, sentence) is None:
            continue
        found.append(QuoteSpan(start=start, end=end, quote=sentence))
        if len(found) == MAX_CANDIDATES:
            break
    return found


def hard_check(sentence: QuoteSpan, disease: str, index: MeshIndex) -> Found | IndicationMiss | Miss:
    """Check a reader's disease against its sentence, with no model.

    The disease must be written in the sentence as given, hold no brackets, and name one MeSH disease.
    """
    if "(" in disease or ")" in disease or "[" in disease:
        return IndicationMiss.BRACKETS
    if not has_words(sentence.quote, disease):
        return IndicationMiss.DISEASE_NOT_IN_SENTENCE
    match = index.match(disease, tail=True)
    if isinstance(match, Miss):
        return match
    return Found(sentence, disease, match.descriptor)
