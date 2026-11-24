"""Which parts of a long filing a reader reads, and how they are cut to size (ADR 0066).

A 10-K or 10-Q is far longer than any press release, and most of it (financial statements, risk
factors, exhibits lists) never states a dated future event. Plain code finds the sections where
companies write about their programs and plans: Business (the pipeline) in a 10-K, Management's
discussion and analysis (with its recent developments) in both, and Other information. Each is
cut into parts no longer than `PART_CHARS`, at line ends, or at sentence ends inside a line too
long for one part. A document with none of these sections (an 8-K exhibit, or a filing laid out
in a way the headings below do not find) is read whole, in parts.

Parts are slices of the document's visible text, so a quote a reader copies from a part is the
document's own words, and its place is found in the whole document as for any claim. A part is
known by the SHA-256 of its words: the same words are never read twice by the same reader.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from itertools import pairwise

# About 20,000 tokens at 4 characters a token: about the largest press release that ever gave a
# catalyst (19,089 tokens, ADR 0038), and half the extractor's 40,000-token input limit.
PART_CHARS = 80_000
FILING_FORMS = frozenset({"10-Q", "10-K"})
WHOLE = "Whole document"
BUSINESS = "Business"
MDA = "Management's discussion and analysis"
OTHER = "Other information"
# A heading line: "Item 7.", "ITEM 1A. RISK FACTORS", "Part I, Item 2 - Management's ...".
_HEADING = re.compile(
    r"^(?:part\s+[ivx]+\W{0,3}\s*)?item\s+(?P<number>\d{1,2}[a-d]?)\b\s*[.:\-\u2013\u2014]?\s*(?P<title>.*)$",
    re.IGNORECASE,
)
_HEADING_CHARS = 200  # a longer line is running text that happens to start with "Item"
_PAGE_NUMBER = re.compile(r"[\s.]*\d{1,3}$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Part:
    """One part of a document for a reader: where it is in the visible text, and its words."""

    section: str
    index: int
    count: int
    start: int
    end: int
    text: str

    @property
    def sha256(self) -> str:
        """Return the SHA-256 of the part's words, which name the part."""
        return hashlib.sha256(self.text.encode()).hexdigest()


@dataclass(frozen=True)
class _Heading:
    line: int
    kind: str | None  # one of the sections read, or None for a section that is not


def _lines(text: str) -> list[tuple[int, int]]:
    """Return each line's start and end in `text`."""
    spans: list[tuple[int, int]] = []
    start = 0
    for line in text.split("\n"):
        spans.append((start, start + len(line)))
        start += len(line) + 1
    return spans


def _kind(title: str) -> str | None:
    words = " ".join(title.replace("\u2019", "'").lower().split())
    words = _PAGE_NUMBER.sub("", words).strip(" .:")
    if "discussion and analysis" in words:
        return MDA
    if words in {"business", "business overview", "our business", "description of business"}:
        return BUSINESS
    if words == "other information":
        return OTHER
    return None


def _headings(text: str, lines: list[tuple[int, int]]) -> list[_Heading]:
    found: list[_Heading] = []
    for number, (start, end) in enumerate(lines):
        line = text[start:end].strip()
        match = _HEADING.match(line) if len(line) <= _HEADING_CHARS else None
        if match is None:
            continue
        title = match.group("title").strip()
        if sum(ch.isalpha() for ch in title) < 3 and number + 1 < len(lines):  # "Item 2." alone on its line
            following = text[lines[number + 1][0] : lines[number + 1][1]].strip()
            title = following if len(following) <= _HEADING_CHARS else ""
        found.append(_Heading(number, _kind(title)))
    return found


def sections(text: str, document_type: str) -> list[tuple[str, int, int]]:
    """Return the sections of `text` to read, as (name, start, end), in the document's order.

    Only a 10-Q or 10-K is cut into sections; anything else, or a filing with none of the sections
    read, is one section, the whole document. A table of contents names every section too, so for
    each kind the longest of its occurrences is the section itself.
    """
    if document_type not in FILING_FORMS:
        return [(WHOLE, 0, len(text))]
    lines = _lines(text)
    headings = _headings(text, lines)
    best: dict[str, tuple[int, int]] = {}
    for number, here in enumerate(headings):
        following = headings[number + 1] if number + 1 < len(headings) else None
        if here.kind is None:
            continue
        start = lines[here.line][0]
        end = lines[following.line][0] if following is not None else len(text)
        if end - start > (best[here.kind][1] - best[here.kind][0] if here.kind in best else 0):
            best[here.kind] = (start, end)
    if not best:
        return [(WHOLE, 0, len(text))]
    return sorted(((kind, start, end) for kind, (start, end) in best.items()), key=lambda found: found[1])


def _atoms(text: str, start: int, end: int, limit: int) -> list[tuple[int, int]]:
    """Return the pieces a part may be cut between: lines, sentences of a long line, or slices."""
    atoms: list[tuple[int, int]] = []
    for line_start, line_end in _lines(text[start:end]):
        a, b = start + line_start, start + line_end
        if b - a <= limit:
            atoms.append((a, b))
            continue
        cuts = [a, *(found.end() for found in _SENTENCE_END.finditer(text, a, b)), b]
        for s, e in pairwise(cuts):
            atoms.extend((x, min(x + limit, e)) for x in range(s, e, limit))
    return [(a, b) for a, b in atoms if text[a:b].strip()]


def _cut(text: str, start: int, end: int, limit: int) -> list[tuple[int, int]]:
    """Cut one section into parts of at most `limit` characters, of about equal size."""
    target = (end - start) / max(1, math.ceil((end - start) / limit))
    spans: list[tuple[int, int]] = []
    current: tuple[int, int] | None = None
    for a, b in _atoms(text, start, end, limit):
        if current is None:
            current = (a, b)
        elif b - current[0] > limit or current[1] - current[0] >= target:
            spans.append(current)
            current = (a, b)
        else:
            current = (current[0], b)
    if current is not None:
        spans.append(current)
    return spans


def document_parts(text: str, document_type: str, limit: int = PART_CHARS) -> list[Part]:
    """Return every part of `text` (a document's visible text) a reader must read, in order."""
    spans = [
        (name, a, b)
        for name, start, end in sections(text, document_type)
        for a, b in _cut(text, start, end, limit)
    ]
    return [Part(name, index, len(spans), a, b, text[a:b]) for index, (name, a, b) in enumerate(spans)]
