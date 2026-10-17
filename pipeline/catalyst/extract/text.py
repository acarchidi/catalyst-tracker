"""Plain text of a stored copy, and exact quotes within it (spec: Provenance; ADR 0012).

Claims store a quote and its character offsets in this text, so the rules here are versioned:
any change to them is a new `TEXT_VERSION`, and older offsets are re-checked against it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

TEXT_VERSION = "1"

# Tags whose text is never part of the document's words.
_HIDDEN = frozenset({"head", "title", "style", "script", "noscript", "template"})
# Tags that start a new line. Table cells are separated by a space instead.
_BLOCKS = frozenset(
    {
        "address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "h1", "h2", "h3", "h4",
        "h5", "h6", "hr", "li", "ol", "p", "pre", "section", "table", "tr", "ul",
    }
)  # fmt: skip
_CELLS = frozenset({"td", "th"})
# SEC filings wrap each document in SGML headers (<TYPE>, <FILENAME>, ...) before <TEXT>.
_SEC_TEXT = re.compile(r"<TEXT>(.*?)(?:</TEXT>|\Z)", re.DOTALL | re.IGNORECASE)
_SPACES = re.compile(r"[^\S\n]+")


class _Words(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN:
            self._hidden += 1
        elif tag in _BLOCKS:
            self.parts.append("\n")
        elif tag in _CELLS:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN:
            self._hidden = max(0, self._hidden - 1)
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)


def _tidy(text: str) -> str:
    """One space between words, one line per block, no blank lines, no edge spaces."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    lines = (_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def document_text(content: bytes, content_type: str) -> str:
    """Return the words of a stored copy as plain text, in reading order."""
    raw = content.decode("utf-8", errors="replace")
    if "html" not in content_type.lower():
        return _tidy(raw)
    wrapped = _SEC_TEXT.search(raw)
    parser = _Words()
    parser.feed(wrapped.group(1) if wrapped else raw)
    parser.close()
    return _tidy("".join(parser.parts))


@dataclass(frozen=True)
class QuoteSpan:
    """Where a quote sits in the text, and the text's own words at that place."""

    start: int
    end: int
    quote: str


def find_quote(text: str, quote: str) -> QuoteSpan | None:
    """Find `quote` in `text`, word for word and case for case.

    Only the spacing may differ (a model may write a line break as a space). The span returned
    always holds the text's own characters, so a stored quote is always the source's words.
    """
    words = quote.split()
    if not words:
        return None
    exact = text.find(quote)
    if exact >= 0:
        return QuoteSpan(exact, exact + len(quote), quote)
    match = re.search(r"\s+".join(map(re.escape, words)), text)
    if match is None:
        return None
    return QuoteSpan(match.start(), match.end(), match.group(0))
