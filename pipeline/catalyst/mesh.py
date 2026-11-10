"""Match an indication's words to one MeSH disease, by plain code (ADR 0053, decision 2).

The index is built from NLM's MeSH descriptor file (`desc{year}.gz`), stored like any source with
its hash. Only diseases are kept: descriptors in tree C (diseases) or F03 (mental disorders).
Every term of a descriptor is indexed, in its own order and, for an inverted term such as
"Dermatitis, Atopic", in reading order too ("atopic dermatitis").

A text matches when, read as whole words, it holds disease terms that all belong to one
descriptor. Longer terms are found first and a term inside a longer one is not counted again, so
"acute myeloid leukemia" is one disease, not also "leukemia". No term means no match; terms of
two or more diseases mean no match, never a guess. There are no synonyms beyond MeSH's own terms,
and a term of three letters or fewer (an abbreviation such as "MS") never matches alone.
"""

from __future__ import annotations

import gzip
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from io import BytesIO
from xml.etree.ElementTree import Element

from defusedxml.ElementTree import iterparse

DISEASE_TREES = ("C", "F03")
MIN_TERM_LETTERS = 4
MAX_TERM_WORDS = 12
_WORD = re.compile(r"[0-9a-z]+(?:['-][0-9a-z]+)*")
_BRACKETS = re.compile(r"\([^()]*\)|\[[^\[\]]*\]")
_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'"})  # curly apostrophes read as straight ones
_POSSESSIVE = re.compile(r"'s\b|s'(?=\s|$)")


@dataclass(frozen=True, order=True)
class Descriptor:
    """One MeSH descriptor: its identifier (D followed by digits) and its preferred name."""

    ui: str
    name: str


@dataclass(frozen=True)
class Match:
    """The one disease a text names, and the words of the text that named it."""

    descriptor: Descriptor
    words: tuple[str, ...]


class Miss(StrEnum):
    """Why a text has no match."""

    NO_DISEASE = "no MeSH disease term in the text"
    SEVERAL_DISEASES = "terms of more than one MeSH disease in the text"


def normal_words(text: str) -> tuple[str, ...]:
    """Return the text's words for matching: lower case, brackets and possessives dropped."""
    text = text.lower().translate(_QUOTES)
    text = _BRACKETS.sub(" ", text)
    text = _POSSESSIVE.sub("", text)
    return tuple(_WORD.findall(text))


def _orders(term: str) -> Iterator[tuple[str, ...]]:
    """Yield a term's words as written and, when inverted ("Leukemia, Myeloid, Acute"), reversed."""
    yield normal_words(term)
    parts = [part.strip() for part in term.split(",")]
    if len(parts) > 1 and all(parts):
        yield normal_words(" ".join(reversed(parts)))


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
            if not any(tree.startswith(DISEASE_TREES) for tree in trees):
                continue
            descriptor = Descriptor(
                ui=record.findtext("DescriptorUI", "").strip(),
                name=record.findtext("DescriptorName/String", "").strip(),
            )
            for term in record.iter("Term"):
                for words in _orders(term.findtext("String", "")):
                    if words and len(words) <= MAX_TERM_WORDS and sum(map(len, words)) >= MIN_TERM_LETTERS:
                        found.setdefault(words, set()).add(descriptor)
        return cls({words: frozenset(descriptors) for words, descriptors in found.items()})

    def match(self, text: str) -> Match | Miss:
        """Return the one disease the text names, or why there is none."""
        words = normal_words(text)
        taken = [False] * len(words)
        descriptors: set[Descriptor] = set()
        named: list[str] = []
        for size in range(min(MAX_TERM_WORDS, len(words)), 0, -1):
            for start in range(len(words) - size + 1):
                span = words[start : start + size]
                if span in self.terms and not any(taken[start : start + size]):
                    taken[start : start + size] = [True] * size
                    descriptors |= self.terms[span]
                    named.extend(span)
        if not descriptors:
            return Miss.NO_DISEASE
        if len(descriptors) > 1:
            return Miss.SEVERAL_DISEASES
        return Match(descriptor=next(iter(descriptors)), words=tuple(named))


def _records(content: bytes) -> Iterator[Element]:
    """Yield each descriptor record, freeing each once read.

    The file is NLM's, fetched over https and stored with its hash; it is still read with
    defusedxml, which refuses entity tricks.
    """
    stream = gzip.GzipFile(fileobj=BytesIO(content)) if content[:2] == b"\x1f\x8b" else BytesIO(content)
    for _, element in iterparse(stream, events=("end",)):
        if element.tag == "DescriptorRecord":
            yield element
            element.clear()
