"""The one inline XBRL header of an SEC filing: data tags the SEC has filers hide (ADR 0066).

Every inline XBRL 10-Q and 10-K carries an `ix:header` inside a `display:none` block. It holds
the filing's data tags (contexts, units, hidden facts), not words for a reader, so the hiding
check (ADR 0018, part 5c) leaves it out. Anything else a page hides still holds every claim.

Plain code finds the header as a parser reads the page, so header words in a comment or a script
are no header. It is left out only when all of these hold (Opus review of PR #150, B1):

- the page has exactly one `ix:header` element, closed;
- an element around it is hidden by its own `display:none` style;
- every element inside it is a data element (`ix:`, `xbrli:`, `link:`, `xbrldi:`), and no text
  sits directly in it.

Otherwise nothing is left out, and the page's hidden words hold its claims as on any page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

HEADER = "ix:header"
_DATA = ("ix:", "xbrli:", "link:", "xbrldi:")
_HIDES = re.compile(r"(?<![\w-])display\s*:\s*none", re.IGNORECASE)
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)


@dataclass
class _Header:
    start: int
    hidden: bool
    clean: bool = True
    end: int | None = None


class _Finder(HTMLParser):
    """Find every `ix:header` element, where it starts and ends, and whether it may be left out."""

    def __init__(self, raw: str) -> None:
        super().__init__(convert_charrefs=True)
        self._raw = raw
        self._lines = [0]
        self._lines.extend(index + 1 for index, ch in enumerate(raw) if ch == "\n")
        self._open: list[tuple[str, bool]] = []  # tag, and whether its own style hides it
        self.headers: list[_Header] = []
        self._inside: _Header | None = None

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._lines[line - 1] + column

    def _data_element(self, tag: str) -> None:
        if self._inside is not None and not tag.startswith(_DATA):
            self._inside.clean = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Open an element; note a header and whether it is hidden."""
        if tag == HEADER:
            if self._inside is not None:
                self._inside.clean = False
            self._inside = _Header(self._offset(), any(hides for _, hides in self._open))
            self.headers.append(self._inside)
        else:
            self._data_element(tag)
        if tag not in _VOID:
            style = " ".join(value or "" for name, value in attrs if name == "style")
            self._open.append((tag, _HIDES.search(style) is not None))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Check a self-closed element, which opens and closes at once; a self-closed header is none."""
        self._data_element(tag)

    def handle_endtag(self, tag: str) -> None:
        """Close back to the element this end tag names, if it is open."""
        if tag == HEADER and self._inside is not None:
            close = self._raw.find(">", self._offset())
            self._inside.end = close + 1 if close >= 0 else None
            self._inside = None
        if any(name == tag for name, _ in self._open):
            while self._open and self._open.pop()[0] != tag:
                pass

    def handle_data(self, data: str) -> None:
        """Text right inside the header, not in a data element, is not data."""
        if self._inside is not None and self._open and self._open[-1][0] == HEADER and data.strip():
            self._inside.clean = False


def without_header(content: bytes) -> bytes:
    """Return the copy without its one real inline XBRL header, or unchanged when it has none."""
    raw = content.decode("utf-8", errors="replace")
    finder = _Finder(raw)
    finder.feed(raw)
    finder.close()
    if len(finder.headers) != 1:
        return content
    header = finder.headers[0]
    if not (header.hidden and header.clean and header.end is not None):
        return content
    return (raw[: header.start] + raw[header.end :]).encode()
