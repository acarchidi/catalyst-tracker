"""Find the sentence that states a catalyst's indication, by plain code (ADR 0053, decision 2 as amended).

The extractor reads an indication for each catalyst, but nothing checks it, so none is
published. This module checks it, with no model call. The indication must name one MeSH disease
(`catalyst.mesh`), and one visible sentence of the stored source must state that the drug is
aimed at that disease. A sentence that only mentions both is not enough (Opus review of PR 124),
so a sentence counts only when all of these hold:

- it names the drug, and no other code-named drug or drug the caller lists;
- it holds no word that negates or reports failure ("not", "no", "without", "failed",
  "excluded", "unlike", ...);
- every disease term in it belongs to the one disease, so another disease never borrows it;
- the disease is the target: after any grading words ("relapsed", "moderate", "advanced") it
  follows "in", "for", "of", "with", or "against", and that word follows a word that names the
  program ("trial", "study", "data", "treatment", "patients", a phase, the drug itself, ...);
- the disease is not given as a cause ("due to", "associated", "-related", "secondary").

The printed words are the sentence's own, with no brackets in them, so the published value
stands on a verbatim quote (spec: Provenance). Words are compared as `normal_words` reads them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum

from catalyst.extract.injection import invisible_characters
from catalyst.extract.text import QuoteSpan, find_quote
from catalyst.mesh import CAUSES, PEOPLE, QUALIFIERS, Descriptor, MeshIndex, Miss, normal_words

# A sentence ends at a full stop, question or exclamation mark, or semicolon before a space, at a
# line break, or at a bullet; press releases list milestones as bullets with no full stop.
_BREAK = re.compile(r"(?<=[.!?;])\s+|[\n\u2028\u2029]+|[•●◦▪]")
_WORD_EDGE = re.compile(r"[0-9A-Za-zÀ-ɏͰ-Ͽ]+")
_CODE_NAME = re.compile(r"[A-Z]{1,6}[\s\-\u2010-\u2015]?\d{2,6}[A-Z]?(?!\w)")
NEGATIONS = frozenset(
    {
        "not",
        "no",
        "without",
        "never",
        "failed",
        "fail",
        "fails",
        "excluded",
        "exclude",
        "excluding",
        "unlike",
        "except",
        "discontinued",
        "terminated",
        "halted",
        "stopped",
    }
)
# Words that only grade or stage the disease; they may stand between the anchor and the disease.
GRADES = QUALIFIERS - {"in", "for", "of", "with", "and", "or", "to", "including", "after", "following"}
ANCHORS = frozenset({"in", "for", "of", "with", "against"})
# Words that name the program the disease is the target of, before the anchor.
PROGRAM = frozenset(
    {
        "trial",
        "trials",
        "study",
        "studies",
        "substudy",
        "data",
        "results",
        "readout",
        "readouts",
        "development",
        "developed",
        "developing",
        "program",
        "programme",
        "programs",
        "cohort",
        "cohorts",
        "therapy",
        "treatment",
        "treatments",
        "treating",
        "treat",
        "patients",
        "participants",
        "approval",
        "approved",
        "indicated",
        "indication",
        "evaluated",
        "evaluating",
        "investigated",
        "investigating",
        "tested",
        "testing",
        "designation",
        "submission",
        "nda",
        "bla",
        "monotherapy",
        "monotherapies",
        "combination",
        "combinations",
        "regimen",
        "maa",
        "phase",
        "1",
        "2",
        "3",
        "4",
        "1b",
        "2a",
        "2b",
        "1a",
        "candidate",
        "agent",
    }
)
# A clause ends at these, so another drug's disease in a second clause is never borrowed.
_CLAUSE = re.compile(r",\s+(?:and|but|while|whereas|yet)\b|\s(?:while|whereas)\s", re.IGNORECASE)
MIN_DRUG_LETTERS = 3
COMMON = frozenset(
    {"the", "a", "an", "and", "or", "of", "in", "for", "with", "to", "is", "it", "its", "this", "that"}
)
MAX_EXTRA_WORDS = 4  # the printed stretch may hold at most this many words more than the target


class IndicationMiss(StrEnum):
    """Why an indication has no sentence to stand on (beyond the MeSH reasons in `Miss`)."""

    NO_INDICATION = "the catalyst states no indication"
    UNUSABLE_DRUG = "the drug's name is too short or too common to find in a sentence"
    NO_SENTENCE = "no sentence names both the drug and the disease"
    NEGATED = "the only sentence naming both negates or reports failure"
    OTHER_DISEASE = "the only sentence naming both also names another disease"
    OTHER_DRUG = "the only sentence naming both also names another drug"
    NOT_TARGETED = "the only sentence naming both does not say the drug is aimed at the disease"
    CAUSE = "the only sentence naming both gives the disease as a cause of another condition"
    HIDDEN = "the only sentence naming both is text the page hides from readers"
    INVISIBLE_CHARACTERS = "the sentence holds invisible or direction-changing characters"


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


def clauses(text: str) -> Iterator[tuple[int, int]]:
    """Yield each clause of each sentence of `text` as (start, end), trimmed (see `_CLAUSE`)."""
    for start, end in sentences(text):
        at = start
        for cut in [*_CLAUSE.finditer(text, start, end), None]:
            stop = cut.start() if cut else end
            piece = text[at:stop]
            if piece.strip():
                lead = len(piece) - len(piece.lstrip())
                yield at + lead, at + len(piece.rstrip())
            if cut:
                at = cut.end()


def _at(words: tuple[str, ...], part: tuple[str, ...]) -> list[int]:
    size = len(part)
    return [i for i in range(len(words) - size + 1) if size and words[i : i + size] == part]


def _own_words(sentence: str, target: tuple[str, ...]) -> str | None:
    """Return the shortest stretch of `sentence`, word start to word end, that reads as `target`.

    Only stretches of at most `MAX_EXTRA_WORDS` more words than the target are tried, so a long
    sentence costs little (review of PR 124).
    """
    edges = list(_WORD_EDGE.finditer(sentence))
    limit = len(target) + MAX_EXTRA_WORDS
    best: str | None = None
    for i, first in enumerate(edges):
        for last in edges[i : i + limit]:
            stretch = sentence[first.start() : last.end()]
            if normal_words(stretch) == target and (best is None or len(stretch) < len(best)):
                best = stretch
                break
    return best


def _targeted(
    words: tuple[str, ...], start: int, end: int, drug_words: tuple[str, ...]
) -> IndicationMiss | None:
    """Say why the disease at words[start:end] is not this sentence's target, or None when it is."""
    if end < len(words) and words[end] in CAUSES:
        return IndicationMiss.CAUSE
    at = start - 1
    while at >= 0 and (words[at] in GRADES or words[at] in PEOPLE - {"patients", "participants"}):
        at -= 1
    if (at >= 0 and words[at] in CAUSES) or (at >= 1 and words[at] == "to" and words[at - 1] in CAUSES):
        return IndicationMiss.CAUSE
    if at < 1 or words[at] not in ANCHORS:
        return IndicationMiss.NOT_TARGETED
    left = words[at - 1]
    if left in PROGRAM or left in drug_words:
        return None
    return IndicationMiss.NOT_TARGETED


def _check(
    sentence: str,
    drug: str,
    target: tuple[str, ...],
    descriptor: Descriptor,
    index: MeshIndex,
    others: list[str],
) -> IndicationMiss | str:
    """Return the printed words when the sentence states the indication, else why it does not."""
    words = normal_words(sentence)
    drug_words = normal_words(drug)
    if NEGATIONS & set(words):
        return IndicationMiss.NEGATED
    drug_key = "".join(drug_words)
    for code in _CODE_NAME.finditer(sentence):
        if "".join(normal_words(code.group())) not in drug_key:
            return IndicationMiss.OTHER_DRUG
    if any(_at(words, normal_words(other)) for other in others if normal_words(other) != drug_words):
        return IndicationMiss.OTHER_DRUG
    terms = index.terms_in(words)
    if any(found != frozenset({descriptor}) for _, _, found in terms):
        return IndicationMiss.OTHER_DISEASE
    reasons = [_targeted(words, start, start + len(target), drug_words) for start in _at(words, target)]
    if None not in reasons:
        return next((r for r in reasons if r is not None), IndicationMiss.NOT_TARGETED)
    own = _own_words(sentence, target)
    if own is None or "(" in own or ")" in own:
        return IndicationMiss.NOT_TARGETED
    return own


def indication_sentence(
    text: str, visible: str, drug: str, indication: str, index: MeshIndex, other_drugs: Iterable[str] = ()
) -> Found | IndicationMiss | Miss:
    """Return the first visible sentence of `text` stating that `drug` is aimed at `indication`'s disease.

    `text` is the stored copy's text, where quotes are placed; `visible` is what readers see.
    `other_drugs` are drug names on file; a sentence naming one of them never backs this drug.
    The MeSH match comes first: an indication that names no single MeSH disease is never published.
    """
    if not normal_words(indication):
        return IndicationMiss.NO_INDICATION
    match = index.match(indication, tail=True)
    if isinstance(match, Miss):
        return match
    drug_words = normal_words(drug)
    if sum(ch.isalpha() for ch in "".join(drug_words)) < MIN_DRUG_LETTERS or set(drug_words) <= COMMON:
        return IndicationMiss.UNUSABLE_DRUG
    others = list(other_drugs)
    first_reason: IndicationMiss | None = None
    for start, end in clauses(text):
        sentence = text[start:end]
        words = normal_words(sentence)
        if not (_at(words, drug_words) and _at(words, match.words)):
            continue
        if invisible_characters(sentence):
            return IndicationMiss.INVISIBLE_CHARACTERS
        checked = _check(sentence, drug, match.words, match.descriptor, index, others)
        if isinstance(checked, IndicationMiss):
            first_reason = first_reason or checked
            continue
        if find_quote(visible, sentence) is None:
            first_reason = first_reason or IndicationMiss.HIDDEN
            continue
        return Found(QuoteSpan(start=start, end=end, quote=sentence), checked, match.descriptor)
    return first_reason or IndicationMiss.NO_SENTENCE
