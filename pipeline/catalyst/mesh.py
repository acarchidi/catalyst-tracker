"""Match an indication's words to one MeSH disease, by plain code (ADR 0053, decision 2).

The index is built from NLM's MeSH descriptor file (`desc{year}.gz`), stored like any source with
its hash. Only diseases are kept: descriptors in tree C (diseases) or F03 (mental disorders),
less the broadest ones ("Disease", "Syndrome", "Neoplasms", "Infections", and any descriptor at the
top of a tree), which would match a generic word and name no disease. Every term of a kept
descriptor is indexed, in its own order and, for an inverted term such as "Dermatitis, Atopic",
in reading order too. Terms MeSH tags as abbreviations or acronyms, and terms that pair a short
form with its expansion in brackets, are left out, because short forms such as "DOES" or "MS"
are also ordinary words.

Text and terms are read the same way: accents dropped, lower case, brackets and possessives
removed, a hyphen read as a space, and "non" joined to the word after it ("non-small cell" and
"non small cell" both read "nonsmall cell", as MeSH also spells it).

A text matches when its whole words hold disease terms that all belong to one descriptor, and
each term stands at the start of the text or another term, or after words that only grade or
stage a disease ("advanced", "relapsed", "moderate", "stage III"), name people ("patients"), or
join words ("of", "with", "and"); the check walks back over all of them. Longer
terms are found first and a term inside a longer one is not counted again. A term after any
other word is a narrower disease MeSH may not have ("solid tumors", "KCNT1+ epilepsy",
"noncirrhotic MASH with liver fibrosis"), and a term after "not", "no", or "without" is its
opposite; a term followed by a cause ("pruritus associated with PBC") is narrower too. All mean
no match. No term, or terms of
two or more diseases, also mean no match, never a guess. There are no synonyms beyond MeSH's own.
"""

from __future__ import annotations

import gzip
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from io import BytesIO
from xml.etree.ElementTree import Element

from defusedxml.ElementTree import iterparse

DISEASE_TREES = ("C", "F03")
MIN_TERM_LETTERS = 4
MAX_TERM_WORDS = 12
# The broadest disease descriptors: a generic word, not a disease (review of PR 123, finding 4).
BROAD = frozenset(
    {
        "D004194",  # Disease
        "D013577",  # Syndrome
        "D035583",  # Rare Diseases
        "D002908",  # Chronic Disease
        "D000208",  # Acute Disease
        "D003141",  # Communicable Diseases
        "D014947",  # Wounds and Injuries
        "D010335",  # Pathologic Processes
        "D020969",  # Disease Attributes
        "D012008",  # Recurrence
        "D018450",  # Disease Progression
        "D009362",  # Neoplasm Metastasis
        "D003643",  # Death
        "D007249",  # Inflammation
        "D005355",  # Fibrosis
    }
)
SHORT_FORM_TAGS = frozenset({"ABB", "ACR"})  # MeSH's tags for abbreviations and acronyms
# Words that only grade, stage, or time a disease, or join words; a term may follow them.
QUALIFIERS = frozenset(
    {
        "advanced",
        "metastatic",
        "relapsed",
        "refractory",
        "recurrent",
        "resistant",
        "untreated",
        "naive",
        "newly",
        "diagnosed",
        "unresectable",
        "resectable",
        "locally",
        "early",
        "late",
        "mild",
        "moderate",
        "severe",
        "active",
        "symptomatic",
        "asymptomatic",
        "stage",
        "i",
        "ii",
        "iii",
        "iv",
        "first",
        "second",
        "third",
        "fourth",
        "line",
        "frontline",
        "1l",
        "2l",
        "3l",
        "4l",
        "adult",
        "adults",
        "pediatric",
        "previously",
        "treated",
        "prior",
        "to",
        "of",
        "with",
        "in",
        "for",
        "and",
        "or",
        "after",
        "following",
        "including",
    }
)
NEGATIONS = frozenset({"not", "no", "without"})
# After a term, these words name a cause: "pruritus associated with PBC" is not plain pruritus.
CAUSES = frozenset({"associated", "due", "secondary", "related", "caused", "induced"})
PEOPLE = frozenset({"patients", "people", "participants", "subjects", "children", "infants", "volunteers"})
_LETTERS = str.maketrans(
    {"\u03b1": "alpha ", "\u03b2": "beta ", "\u03b3": "gamma ", "\u03ba": "kappa ", "\u00df": "ss"}
)
_WORD = re.compile(r"[0-9a-z]+(?:'[0-9a-z]+)*")
_BRACKETS = re.compile(r"\([^()]*\)|\[[^\[\]]*\]")
_POSSESSIVE = re.compile(r"'s\b|s'(?=\s|$)")
_NON = re.compile(r"\bnon[\s-]+(?=[0-9a-z])")
_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'"})  # curly apostrophes read as straight ones


@dataclass(frozen=True, order=True)
class Descriptor:
    """One MeSH descriptor: its identifier (D followed by digits) and its preferred name."""

    ui: str
    name: str


@dataclass(frozen=True)
class Match:
    """The one disease a text names, and the text's words that named it, as `normal_words` reads them.

    The words are for checking against a quote read the same way; they are not text to print.
    """

    descriptor: Descriptor
    words: tuple[str, ...]


class Miss(StrEnum):
    """Why a text has no match."""

    NO_DISEASE = "no MeSH disease term in the text"
    SEVERAL_DISEASES = "terms of more than one MeSH disease in the text"
    NARROWER = "a word before the disease term narrows it to a disease MeSH may not have"
    NEGATED = "the disease term follows a word that negates it"
    CAUSED = "a cause after the disease term narrows it to a disease MeSH may not have"


def normal_words(text: str) -> tuple[str, ...]:
    """Return the text's words for matching (see the module notes)."""
    text = text.translate(_LETTERS)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().translate(_QUOTES)
    text = _BRACKETS.sub(" ", text)
    text = _POSSESSIVE.sub("", text)
    text = _NON.sub("non", text).replace("-", " ")
    return tuple(_WORD.findall(text))


def _orders(term: str) -> Iterator[tuple[str, ...]]:
    """Yield a term's words as written and, when inverted ("Leukemia, Myeloid, Acute"), reversed.

    A term with brackets is a short form with its expansion ("DOES (Disorders of Excessive
    Somnolence)"); the expansion is a term of its own, and the short form is also a word, so the
    bracketed term is skipped.
    """
    if "(" in term or "[" in term:
        return
    yield normal_words(term)
    parts = [part.strip() for part in term.split(",")]
    if len(parts) > 1 and all(parts):
        yield normal_words(" ".join(reversed(parts)))


def _letters(words: tuple[str, ...]) -> int:
    return sum(ch.isalpha() for word in words for ch in word)


@dataclass
class MeshIndex:
    """Disease terms by their words."""

    terms: dict[tuple[str, ...], frozenset[Descriptor]] = field(
        default_factory=lambda: dict[tuple[str, ...], frozenset[Descriptor]]()
    )

    @property
    def descriptors(self) -> frozenset[Descriptor]:
        """Every disease descriptor in the index."""
        return frozenset(d for found in self.terms.values() for d in found)

    @classmethod
    def from_xml(cls, content: bytes) -> MeshIndex:
        """Build the index from a MeSH descriptor file, plain or gzip-compressed."""
        found: dict[tuple[str, ...], set[Descriptor]] = {}
        for record in _records(content):
            trees = [node.text or "" for node in record.iter("TreeNumber")]
            ui = record.findtext("DescriptorUI", "").strip()
            disease = [tree for tree in trees if tree.startswith(DISEASE_TREES)]
            if not disease or ui in BROAD or any("." not in tree for tree in disease):
                continue  # not a disease, or too broad to name one
            descriptor = Descriptor(ui=ui, name=record.findtext("DescriptorName/String", "").strip())
            for term in record.iter("Term"):
                if term.get("LexicalTag") in SHORT_FORM_TAGS:
                    continue
                for words in _orders(term.findtext("String", "")):
                    if words and len(words) <= MAX_TERM_WORDS and _letters(words) >= MIN_TERM_LETTERS:
                        found.setdefault(words, set()).add(descriptor)
        return cls({words: frozenset(descriptors) for words, descriptors in found.items()})

    def match(self, text: str) -> Match | Miss:
        """Return the one disease the text names, or why there is none."""
        words = normal_words(text)
        taken = [False] * len(words)
        descriptors: set[Descriptor] = set()
        named: list[str] = []
        spans: list[tuple[int, int]] = []
        for size in range(min(MAX_TERM_WORDS, len(words)), 0, -1):
            for start in range(len(words) - size + 1):
                span = words[start : start + size]
                if span in self.terms and not any(taken[start : start + size]):
                    taken[start : start + size] = [True] * size
                    descriptors |= self.terms[span]
                    named.extend(span)
                    spans.append((start, size))
        if not descriptors:
            return Miss.NO_DISEASE
        for start, size in spans:
            if miss := _context(words, taken, start, start + size):
                return miss
        if len(descriptors) > 1:
            return Miss.SEVERAL_DISEASES
        return Match(descriptor=next(iter(descriptors)), words=tuple(named))


def _context(words: tuple[str, ...], taken: list[bool], start: int, end: int) -> Miss | None:
    """Check the words around one term; a narrowing or negating word gives a miss.

    Back from the term, every word up to the start of the text or another term must grade,
    stage, join, or name people; "not", "no", or "without" on the way negates the term. After the
    term, a word that names a cause ("associated", "due") must lead only to grading or joining
    words or another term: "pruritus associated with PBC" is a narrower disease.
    """
    at = start - 1
    while at >= 0 and not taken[at]:
        if words[at] in NEGATIONS:
            return Miss.NEGATED
        if words[at] not in QUALIFIERS and words[at] not in PEOPLE:
            return Miss.NARROWER
        at -= 1
    after = end
    while after < len(words) and not taken[after] and words[after] in QUALIFIERS - CAUSES:
        after += 1
    if after < len(words) and not taken[after] and words[after] in CAUSES:
        rest = after + 1
        while rest < len(words) and not taken[rest]:
            if words[rest] not in QUALIFIERS and words[rest] not in CAUSES:
                return Miss.CAUSED
            rest += 1
    return None


def _records(content: bytes) -> Iterator[Element]:
    """Yield each descriptor record, freeing each once read.

    The file is NLM's, fetched over https and stored with its hash; it is still read with
    defusedxml, which refuses entity tricks. A gzip file is read as a stream.
    """
    stream = gzip.GzipFile(fileobj=BytesIO(content)) if content[:2] == b"\x1f\x8b" else BytesIO(content)
    for _, element in iterparse(stream, events=("end",)):
        if element.tag == "DescriptorRecord":
            yield element
            element.clear()
