"""Find the sentence that backs a catalyst's indication, by plain code (ADR 0053, decision 2 as amended).

The extractor reads an indication for each catalyst, but nothing checks it, so none is
published. This module checks it, with no model call. The indication must name one MeSH disease
(`catalyst.mesh`), and one visible sentence of the stored source must name both the catalyst's
drug and that disease's words. The printed words are the sentence's own, so the published value
stands on a verbatim quote (spec: Provenance). Words are compared as `normal_words` reads them.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

from catalyst.extract.injection import invisible_characters
from catalyst.extract.text import QuoteSpan, find_quote
from catalyst.mesh import Descriptor, MeshIndex, Miss, normal_words

# A sentence ends at a full stop, question or exclamation mark, or semicolon before a space, at a
# line break, or at a bullet; press releases list milestones as bullets with no full stop.
_BREAK = re.compile(r"(?<=[.!?;])\s+|\n+|[•●◦▪]")
_WORD_EDGE = re.compile(r"[0-9A-Za-zÀ-ɏͰ-Ͽ]+")


class IndicationMiss(StrEnum):
    """Why an indication has no sentence to stand on (beyond the MeSH reasons in `Miss`)."""

    NO_INDICATION = "the catalyst states no indication"
    NO_SENTENCE = "no sentence names both the drug and the disease"
    HIDDEN = "the only sentence naming both is text the page hides from readers"
    INVISIBLE_CHARACTERS = "the sentence holds invisible or direction-changing characters"


@dataclass(frozen=True)
class Found:
    """The sentence that backs the indication, the disease's words in it, and the MeSH disease."""

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


def _contains(words: tuple[str, ...], part: tuple[str, ...]) -> bool:
    size = len(part)
    return size > 0 and any(words[i : i + size] == part for i in range(len(words) - size + 1))


def _own_words(sentence: str, target: tuple[str, ...]) -> str | None:
    """Return the shortest stretch of `sentence`, from word start to word end, that reads as `target`."""
    edges = list(_WORD_EDGE.finditer(sentence))
    best: str | None = None
    for i, first in enumerate(edges):
        for last in edges[i:]:
            stretch = sentence[first.start() : last.end()]
            if normal_words(stretch) == target and (best is None or len(stretch) < len(best)):
                best = stretch
    return best


def indication_sentence(
    text: str, visible: str, drug: str, indication: str, index: MeshIndex
) -> Found | IndicationMiss | Miss:
    """Return the first visible sentence of `text` naming `drug` and `indication`'s MeSH disease.

    `text` is the stored copy's text, where quotes are placed; `visible` is what readers see. The
    MeSH match comes first: an indication that names no single MeSH disease is never published.
    """
    if not normal_words(indication):
        return IndicationMiss.NO_INDICATION
    match = index.match(indication)
    if isinstance(match, Miss):
        return match
    drug_words = normal_words(drug)
    hidden = False
    for start, end in sentences(text):
        sentence = text[start:end]
        words = normal_words(sentence)
        if not (_contains(words, drug_words) and _contains(words, match.words)):
            continue
        own = _own_words(sentence, match.words)
        if own is None:
            continue
        if invisible_characters(sentence):
            return IndicationMiss.INVISIBLE_CHARACTERS
        if find_quote(visible, sentence) is None:
            hidden = True
            continue
        return Found(QuoteSpan(start=start, end=end, quote=sentence), own, match.descriptor)
    return IndicationMiss.HIDDEN if hidden else IndicationMiss.NO_SENTENCE
