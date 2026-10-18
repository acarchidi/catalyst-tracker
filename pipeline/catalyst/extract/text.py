"""Plain text of a stored copy, and exact quotes within it (spec: Provenance; ADR 0012).

Claims store a quote and its character offsets in this text, so the rules here are versioned:
any change to them is a new `TEXT_VERSION`, and older offsets are re-checked against it.

`document_text` drops comments, scripts, styles, the head, and every attribute (alt text,
titles), but keeps text that markup hides. `visible_text` also drops that hidden text: text an
inline style or a stylesheet rule hides, places off the page, or clips to nothing.
It is what a model reads, and every quote must be found in it too; it places no offsets, so it
has no version of its own (ADR 0018, part 5c).
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
# Elements that never have an end tag, so they never open a hidden part.
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
# Styles that hide an element's text: display:none, visibility:hidden, opacity 0, a font too small
# to read (1px or 1pt and under), a place far off the page (1,000px or more to the left or top), or
# a box of no height or width that clips what overflows it. Colour alone does not hide: white text
# on a dark background is ordinary. Filings use small negative offsets (-18pt indents).
_NO_DISPLAY = re.compile(r"display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse)", re.IGNORECASE)
_OPACITY = re.compile(r"(?<![\w-])opacity\s*:\s*(?P<value>[\d.]+)", re.IGNORECASE)
_FONT_SIZE = re.compile(r"font-size\s*:\s*(?P<value>[\d.]+)\s*(?P<unit>px|pt|em|rem|%)?", re.IGNORECASE)
_TINY = {"px": 1.0, "pt": 1.0, "em": 0.1, "rem": 0.1, "%": 10.0, "": 0.0}
_OFFSET = re.compile(
    r"(?<![\w-])(?:left|top|margin-left|margin-top|text-indent)\s*:\s*-\s*(?P<value>[\d.]+)\s*"
    r"(?P<unit>px|pt|em|rem|in|cm|mm)?",
    re.IGNORECASE,
)
_PIXELS = {"px": 1.0, "": 1.0, "pt": 4 / 3, "em": 16.0, "rem": 16.0, "in": 96.0, "cm": 37.8, "mm": 3.78}
OFF_PAGE_PIXELS = 1_000
_NO_SIZE = re.compile(
    r"(?<![\w-])(?:max-)?(?:height|width)\s*:\s*0(?:\.0*)?\s*(?:px|pt|em|rem|%)?\s*(?:;|$)", re.IGNORECASE
)
_CLIPPED = re.compile(r"overflow(?:-[xy])?\s*:\s*(?:hidden|clip)", re.IGNORECASE)
# Stylesheet rules: "selector, selector { declarations }". Rules inside @media blocks are read too.
_STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.DOTALL | re.IGNORECASE)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_COMPOUND = re.compile(r"^(?P<tag>[a-z][\w-]*|\*)?(?P<rest>(?:[.#][\w-]+)*)$", re.IGNORECASE)
# SEC filings wrap each document in SGML headers (<TYPE>, <FILENAME>, ...) before <TEXT>.
_SEC_TEXT = re.compile(r"<TEXT>(.*?)(?:</TEXT>|\Z)", re.DOTALL | re.IGNORECASE)
_SPACES = re.compile(r"[^\S\n]+")


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _style_hides(style: str) -> bool:
    """Say whether a list of style declarations hides the text it applies to."""
    if _NO_DISPLAY.search(style):
        return True
    for found in _OPACITY.finditer(style):
        value = _number(found.group("value"))
        if value is not None and value < 0.1:
            return True
    for found in _FONT_SIZE.finditer(style):
        value = _number(found.group("value"))
        if value is not None and value <= _TINY[(found.group("unit") or "").lower()]:
            return True
    for found in _OFFSET.finditer(style):
        value = _number(found.group("value"))
        if value is not None and value * _PIXELS[(found.group("unit") or "").lower()] >= OFF_PAGE_PIXELS:
            return True
    return bool(_NO_SIZE.search(style) and _CLIPPED.search(style))


@dataclass(frozen=True)
class _Selector:
    """The last part of a CSS selector: a tag, classes, and an id, each optional."""

    tag: str | None
    classes: frozenset[str]
    id: str | None

    def matches(self, tag: str, classes: frozenset[str], element_id: str | None) -> bool:
        return (
            (self.tag is None or self.tag == tag)
            and self.classes <= classes
            and (self.id is None or self.id == element_id)
        )


def hiding_selectors(raw: str) -> tuple[_Selector, ...]:
    """Return every stylesheet selector whose rule hides text.

    A selector with a pseudo-class or an attribute test (":hover", "[lang]") is left out: it
    hides only sometimes, or what code cannot tell.
    """
    found: list[_Selector] = []
    for block in _STYLE_BLOCK.findall(raw):
        for selectors, declarations in _RULE.findall(_CSS_COMMENT.sub("", block)):
            if not _style_hides(declarations):
                continue
            for selector in selectors.split(","):
                last = re.split(r"[\s>+~]+", selector.strip())[-1]
                shape = _COMPOUND.match(last) if last and not last.startswith("@") else None
                if shape is None:
                    continue
                rest = shape.group("rest")
                tag = shape.group("tag")
                found.append(
                    _Selector(
                        tag=None if tag in (None, "*") else tag.lower(),
                        classes=frozenset(re.findall(r"\.([\w-]+)", rest)),
                        id=next(iter(re.findall(r"#([\w-]+)", rest)), None),
                    )
                )
    return tuple(found)


def _hides(tag: str, attrs: list[tuple[str, str | None]], rules: tuple[_Selector, ...] = ()) -> bool:
    """Say whether an element's attributes, or a stylesheet rule that fits it, hide its text."""
    if any(name == "hidden" for name, _ in attrs):
        return True
    style = " ".join(value or "" for name, value in attrs if name == "style")
    if _style_hides(style):
        return True
    if rules:
        classes = frozenset(" ".join(value or "" for name, value in attrs if name == "class").split())
        element_id = next((value for name, value in attrs if name == "id"), None)
        return any(rule.matches(tag, classes, element_id) for rule in rules)
    return False


class _Words(HTMLParser):
    def __init__(self, *, drop_hidden: bool = False, rules: tuple[_Selector, ...] = ()) -> None:
        super().__init__(convert_charrefs=True)
        self._rules = rules
        self.parts: list[str] = []
        self._hidden = 0
        self._drop_hidden = drop_hidden
        self._open: list[tuple[str, bool]] = []  # open elements, and whether each hides its text
        self._hiding = 0

    def _enter(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self._drop_hidden or tag in _VOID:
            return
        hidden = _hides(tag, attrs, self._rules)
        self._open.append((tag, hidden))
        self._hiding += hidden

    def _leave(self, tag: str) -> None:
        if not any(name == tag for name, _ in self._open):
            return  # a stray end tag closes nothing
        while self._open:
            name, hidden = self._open.pop()
            self._hiding -= hidden
            if name == tag:
                return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._enter(tag, attrs)
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
        self._leave(tag)
        if tag in _HIDDEN:
            self._hidden = max(0, self._hidden - 1)
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden and not self._hiding:
            self.parts.append(data)


def _tidy(text: str) -> str:
    """One space between words, one line per block, no blank lines, no edge spaces."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    lines = (_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def _text(content: bytes, content_type: str, *, drop_hidden: bool) -> str:
    raw = content.decode("utf-8", errors="replace")
    if "html" not in content_type.lower():
        return _tidy(raw)
    wrapped = _SEC_TEXT.search(raw)
    parser = _Words(drop_hidden=drop_hidden, rules=hiding_selectors(raw) if drop_hidden else ())
    parser.feed(wrapped.group(1) if wrapped else raw)
    parser.close()
    return _tidy("".join(parser.parts))


def document_text(content: bytes, content_type: str) -> str:
    """Return the words of a stored copy as plain text, in reading order."""
    return _text(content, content_type, drop_hidden=False)


def visible_text(content: bytes, content_type: str) -> str:
    """Return `document_text` without the text that inline markup hides from a reader.

    An element is hidden by the `hidden` attribute, or by an inline style or a stylesheet rule
    for its tag, classes, or id that sets display:none, visibility:hidden, opacity under 0.1, a
    font of 1px or 1pt and under, a place 1,000px or more off the page, or no height or width
    with clipped overflow; so is everything inside it. Plain text has no markup, so all of it is
    visible.
    """
    return _text(content, content_type, drop_hidden=True)


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
