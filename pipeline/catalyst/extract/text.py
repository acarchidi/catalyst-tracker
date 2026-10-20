"""Plain text of a stored copy, and exact quotes within it (spec: Provenance; ADR 0012).

Claims store a quote and its character offsets in this text, so the rules here are versioned:
any change to them is a new `TEXT_VERSION`, and older offsets are re-checked against it.

`document_text` drops comments, scripts, styles, the head, and every attribute (alt text,
titles), but keeps text that markup hides. `visible_text` also drops that hidden text: text an
inline style or a stylesheet rule hides, places off the page, or clips to nothing.
It is what a model reads, and every quote must be found in it too; it places no offsets, so it
has no version of its own (ADR 0018, part 5c). It follows CSS where a reader would see the
text: a child may undo an inherited visibility, font size, or colour, print-only rules do not
apply on screen, and a cloak that hides the whole page is lifted (ADR 0044).
"""

from __future__ import annotations

import re
from collections import Counter
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
# Styles that hide an element's text: display:none, visibility:hidden, opacity 0, transparent
# text, a font too small to read (1px or 1pt and under), a place far off the page (an offset or a
# translation of 1,000px or more, or of 100% or 100vw or more), a scale of 0, a clip to nothing, or
# a box 1px or smaller that clips what overflows it. A colour alone does not hide: white text on a
# dark background is ordinary. Filings use small negative offsets (-18pt indents).
_NO_DISPLAY = re.compile(r"display\s*:\s*none|visibility\s*:\s*collapse", re.IGNORECASE)
_INVISIBLE = re.compile(r"visibility\s*:\s*hidden", re.IGNORECASE)
_OPACITY = re.compile(r"(?<![\w-])opacity\s*:\s*(?P<value>[\d.]+)", re.IGNORECASE)
_FONT_SIZE = re.compile(r"font-size\s*:\s*(?P<value>[\d.]+)\s*(?P<unit>px|pt|em|rem|%)?", re.IGNORECASE)
_TINY = {"px": 1.0, "pt": 1.0, "em": 0.1, "rem": 0.1, "%": 10.0, "": 0.0}
# Declarations that set an inherited property again, so a child shows what a parent hid. A browser
# drops a declaration it cannot read, and the child inherits, so a show counts only from a style
# that reads cleanly (`_declarations`) and from a whole, valid value: a size in absolute units (em
# and % scale the parent's), a `font` shorthand with a family, a plain colour, visibility:visible.
_ABSOLUTE = r"(?P<value>\d*\.?\d+)(?P<unit>px|pt|in|cm|mm|pc)"
_IMPORTANT = r"(?:\s*!\s*important)?"
_SIZE_VALUE = re.compile(_ABSOLUTE + _IMPORTANT, re.IGNORECASE)
_FAMILY = r'(?:""|[a-z][\w-]*(?:\s+[a-z][\w-]*)*)'
_FONT_VALUE = re.compile(
    r"(?:(?:normal|italic|oblique|bold|bolder|lighter|small-caps|[1-9]00)\s+)*"
    + _ABSOLUTE
    + r"(?:/[\w.%]+)?\s+"
    + _FAMILY
    + r"(?:\s*,\s*"
    + _FAMILY
    + ")*"
    + _IMPORTANT,
    re.IGNORECASE,
)
_COLOUR_VALUE = re.compile(
    r"(?:#[0-9a-f]{3}|#[0-9a-f]{6}|(?:rgb|hsl)a?\([\d.,%\s/]+\)"
    r"|black|white|gray|grey|red|blue|green|navy|maroon|windowtext)" + _IMPORTANT,
    re.IGNORECASE,
)
_VISIBLE_VALUE = re.compile("visible" + _IMPORTANT, re.IGNORECASE)
_CSS_WIDE = frozenset({"inherit", "initial", "unset", "revert", "revert-layer", "default"})
_STRING = re.compile(r"\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'")
_DECLARATION = re.compile(r"\s*(?P<name>-?[a-z][\w-]*)\s*:(?P<value>.*)", re.IGNORECASE | re.DOTALL)
_BRACKETS = {")": "(", "]": "["}
# A `font` shorthand's size, for hiding only: read loosely, since a false hide is safe.
_SHORTHAND_SIZE = re.compile(r"(?<![\w-])font\s*:[^;]*?(?<![\w.-])" + _ABSOLUTE, re.IGNORECASE)
_UNKNOWN_VALUE = re.compile(r"\b(?:var|env|attr|calc)\(", re.IGNORECASE)  # resolved only in a browser
_LENGTH = r"(?P<sign>[-+]?)\s*(?P<value>[\d.]+)\s*(?P<unit>px|pt|em|rem|in|cm|mm|%|vw|vh)?"
# Where an element sits: left, right, top, or bottom far off either side; a margin or indent far to
# the left or top.
_PLACE = re.compile(r"(?<![\w-])(?P<prop>left|right|top|bottom)\s*:\s*" + _LENGTH, re.IGNORECASE)
_PULL = re.compile(r"(?<![\w-])(?:margin-left|margin-top|text-indent)\s*:\s*" + _LENGTH, re.IGNORECASE)
_TRANSFORM = re.compile(r"(?<![\w-])transform\s*:([^;]*)", re.IGNORECASE)
_TRANSLATE_PART = re.compile(_LENGTH, re.IGNORECASE)
_SCALE_ZERO = re.compile(r"scale[xy]?\(\s*0*(?:\.0*)?\s*[,)]", re.IGNORECASE)
_PIXELS = {"px": 1.0, "": 1.0, "pt": 4 / 3, "em": 16.0, "rem": 16.0, "in": 96.0, "cm": 37.8, "mm": 3.78}
_SHARE = {"%", "vw", "vh"}  # off the page at 100 or more
OFF_PAGE_PIXELS = 1_000
_SIZE = re.compile(r"(?<![\w-])(?:max-)?(?:height|width)\s*:\s*" + _LENGTH, re.IGNORECASE)
_CLIPPED = re.compile(
    r"overflow(?:-[xy])?\s*:\s*(?:hidden|clip)|(?<![\w-])clip\s*:\s*rect\(|clip-path\s*:", re.IGNORECASE
)
_CLIPPED_AWAY = re.compile(
    r"(?<![\w-])clip\s*:\s*rect\(\s*0[a-z]*[\s,]+0[a-z]*[\s,]+0[a-z]*[\s,]+0[a-z]*\s*\)"
    r"|clip-path\s*:\s*(?:inset\(\s*(?:50|100)%|circle\(\s*0)",
    re.IGNORECASE,
)
_COLOUR = re.compile(r"(?<![\w-])color\s*:\s*(?P<value>[^;]+)", re.IGNORECASE)
_ZERO_ALPHA = re.compile(
    r"^(?:transparent|(?:rgba|hsla)\([^)]*,\s*0*(?:\.0*)?%?\s*\)|(?:rgb|hsl)\([^)]*/\s*0*(?:\.0*)?%?\s*\)"
    r"|#[0-9a-f]{3}0|#[0-9a-f]{6}00)\s*(?:!important)?$",
    re.IGNORECASE,
)
# Elements whose content a reader does not see: fallback content of embedded media and frames, and a
# dialog that is not open.
_FALLBACK = frozenset({"canvas", "iframe", "object", "video", "audio", "noembed", "noframes"})
# Stylesheet rules: "selector, selector { declarations }". Rules inside @media blocks are read too,
# except where the media are only print or speech: a reader on screen never meets those.
_STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.DOTALL | re.IGNORECASE)
_CSS_COMMENT = re.compile(r"/\*.*?\*/|<!--|-->", re.DOTALL)
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_ONE_LINE_AT = re.compile(r"@[\w-]+[^{};]*;")  # @import, @charset, @layer a;
_OFF_SCREEN_QUERY = re.compile(r"^(?:only\s+)?(?:print|speech)\b", re.IGNORECASE)
# A page hides all of itself (a cloak) at most this deep: the html element, the body, and a wrapper.
_MAX_CLOAKS = 3
_COMPOUND = re.compile(r"^(?P<tag>[a-z][\w-]*|\*)?(?P<rest>(?:[.#][\w-]+)*)$", re.IGNORECASE)
# SEC filings wrap each document in SGML headers (<TYPE>, <FILENAME>, ...) before <TEXT>.
_SEC_TEXT = re.compile(r"<TEXT>(.*?)(?:</TEXT>|\Z)", re.DOTALL | re.IGNORECASE)
_SPACES = re.compile(r"[^\S\n]+")


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _off_page(match: re.Match[str]) -> bool:
    """Say whether one length is far enough to put the element off the page."""
    value = _number(match.group("value"))
    if value is None:
        return False
    unit = (match.group("unit") or "").lower()
    if unit in _SHARE:
        return value >= 100
    return value * _PIXELS[unit] >= OFF_PAGE_PIXELS


def _see_through(style: str) -> bool:
    """Say whether the element is nearly see-through; no child can undo that."""
    return any(
        (value := _number(found.group("value"))) is not None and value < 0.1
        for found in _OPACITY.finditer(style)
    )


def _tiny(style: str) -> bool:
    """Say whether the font is set at 1px or 1pt and under."""
    for found in _FONT_SIZE.finditer(style):
        value = _number(found.group("value"))
        if value is not None and value <= _TINY[(found.group("unit") or "").lower()]:
            return True
    return any(not _readable(found) for found in _SHORTHAND_SIZE.finditer(style))


def _readable(found: re.Match[str] | None) -> bool:
    """Say whether a matched absolute size is over 1px or 1pt."""
    if found is None or (value := _number(found.group("value"))) is None:
        return False
    unit = found.group("unit").lower()
    return value * (16.0 if unit == "pc" else _PIXELS[unit]) > (1.0 if unit == "px" else 4 / 3)


def _clear(style: str) -> bool:
    """Say whether the text is transparent."""
    return any(_ZERO_ALPHA.match(found.group("value").strip()) for found in _COLOUR.finditer(style))


def _strings_out(css: str) -> str | None:
    """Return CSS with every string emptied, or None if it escapes or leaves a string open."""
    if "\\" in css:
        return None
    bare = _STRING.sub('""', css)
    return None if "'" in bare or '"' in bare.replace('""', "") else bare


def _declarations(style: str) -> list[tuple[str, str]] | None:
    """Return a style's declarations as (property, value), or None if it does not read cleanly.

    Cleanly: no braces, comments, escapes, or open strings or brackets, and every part between
    semicolons a property and a value. A browser recovers from such a style in ways code cannot
    follow, so none of it may show text.
    """
    bare = _strings_out(style)
    if bare is None or "{" in bare or "}" in bare or "/*" in bare:
        return None
    opened: list[str] = []
    for mark in bare:
        if mark in "([":
            opened.append(mark)
        elif mark in _BRACKETS and (not opened or opened.pop() != _BRACKETS[mark]):
            return None
    if opened:
        return None
    found: list[tuple[str, str]] = []
    for piece in bare.split(";"):
        if piece.strip():
            declaration = _DECLARATION.fullmatch(piece)
            if declaration is None:
                return None
            found.append((declaration.group("name").lower(), declaration.group("value").strip()))
    return found


def _shows(style: str) -> tuple[bool, bool, bool]:
    """Say whether the style sets visibility:visible, a readable font size, and a colour again."""
    visible = sized = coloured = False
    for name, value in _declarations(style) or ():
        if name == "visibility":
            visible = visible or _VISIBLE_VALUE.fullmatch(value) is not None
        elif name == "font-size":
            sized = sized or _readable(_SIZE_VALUE.fullmatch(value))
        elif name == "font" and not _CSS_WIDE & set(re.findall(r"[a-z][\w-]*", value.lower())):
            sized = sized or _readable(_FONT_VALUE.fullmatch(value))
        elif name == "color":
            coloured = coloured or _COLOUR_VALUE.fullmatch(value) is not None
    return visible, sized, coloured


def _moved_away(style: str) -> bool:
    """Say whether the element is placed, pulled, or translated off the page, or scaled to nothing."""
    if any(_off_page(found) for found in _PLACE.finditer(style)):
        return True
    if any(found.group("sign") == "-" and _off_page(found) for found in _PULL.finditer(style)):
        return True
    for found in _TRANSFORM.finditer(style):
        moves = found.group(1)
        if _SCALE_ZERO.search(moves):
            return True
        for call in re.finditer(r"translate(?:3d|x|y)?\(([^)]*)\)", moves, re.IGNORECASE):
            if any(_off_page(part) for part in _TRANSLATE_PART.finditer(call.group(1))):
                return True
    return False


def _clipped_small(style: str) -> bool:
    """Say whether the element is a box 1px or smaller that clips what overflows it."""
    if not _CLIPPED.search(style):
        return False
    for found in _SIZE.finditer(style):
        value = _number(found.group("value"))
        unit = (found.group("unit") or "").lower()
        if value == 0 or (value is not None and unit not in _SHARE and value * _PIXELS[unit] <= 1):
            return True
    return False


def _either(first: bool | None, second: bool | None) -> bool | None:
    """Combine two settings of one inherited property: hiding wins, then showing, then inheriting."""
    if first or second:
        return True
    return False if first is False or second is False else None


@dataclass(frozen=True)
class _Look:
    """How an element's own markup shows its text.

    `gone` hides everything inside, and no child can undo it (display:none, opacity, a place off
    the page, a clip). The other three are inherited, and a child may set them again: True hides,
    False shows, None takes the parent's (visibility, a font of 1px or 1pt and under, transparent text).
    """

    gone: bool = False
    invisible: bool | None = None
    tiny: bool | None = None
    clear: bool | None = None

    def hides(self) -> bool:
        return self.gone or bool(self.invisible) or bool(self.tiny) or bool(self.clear)

    def __or__(self, other: _Look) -> _Look:
        return _Look(
            self.gone or other.gone,
            _either(self.invisible, other.invisible),
            _either(self.tiny, other.tiny),
            _either(self.clear, other.clear),
        )

    def within(self, parent: _Look) -> _Look:
        """Return what holds for text inside this element, given what holds around it."""

        def own(mine: bool | None, theirs: bool | None) -> bool | None:
            return theirs if mine is None else mine

        return _Look(
            parent.gone or self.gone,
            own(self.invisible, parent.invisible),
            own(self.tiny, parent.tiny),
            own(self.clear, parent.clear),
        )

    def hiding_only(self) -> _Look:
        """Return the same look without what it shows: for rules that may not fit (see `_style_rules`)."""
        return _Look(self.gone, self.invisible or None, self.tiny or None, self.clear or None)


_PLAIN = _Look()
_PAGE: tuple[str, _Look, int | None] = ("", _PLAIN, None)


def _style_look(style: str) -> _Look:
    """Return how a list of style declarations shows the text it applies to."""
    if not style.strip():
        return _PLAIN
    gone = bool(_NO_DISPLAY.search(style) or _CLIPPED_AWAY.search(style))
    gone = gone or _see_through(style) or _moved_away(style) or _clipped_small(style)
    visible, sized, coloured = _shows(style)
    look = _Look(
        gone,
        True if _INVISIBLE.search(style) else (False if visible else None),
        True if _tiny(style) else (False if sized else None),
        True if _clear(style) else (False if coloured else None),
    )
    return look.hiding_only() if _UNKNOWN_VALUE.search(style) else look


@dataclass(frozen=True)
class _Selector:
    """The last part of a CSS selector: a tag, classes, and an id, each optional."""

    tag: str | None
    classes: frozenset[str]
    id: str | None

    def matches(self, tag: str, classes: frozenset[str], ids: frozenset[str]) -> bool:
        return (
            (self.tag is None or self.tag == tag)
            and self.classes <= classes
            and (self.id is None or self.id in ids)
        )


def _off_screen(prelude: str) -> bool:
    """Say whether an @media block is for print or speech only, so a reader on screen never meets it."""
    if not prelude.lower().startswith("@media"):
        return False
    return all(_OFF_SCREEN_QUERY.match(query.strip()) for query in prelude[len("@media") :].split(","))


def _css_rules(css: str) -> list[tuple[str, str, bool]] | None:
    """Return each rule as (selectors, declarations, top level), leaving out print-only @media.

    A rule inside any at-rule block (such as @media) is not top level: it applies only on some
    screens. Strings are emptied and one-line at-rules dropped before braces are counted. None when
    the sheet does not read cleanly: an escape, an open string, a nested rule, or unbalanced braces.
    """
    bare = _strings_out(css)
    if bare is None:
        return None
    bare = _ONE_LINE_AT.sub(" ", bare)
    rules: list[tuple[str, str, bool]] = []
    # Each open block: its prelude, where its inside starts, and whether its rules are read.
    blocks: list[tuple[str, int, bool]] = []
    start = 0
    for brace in re.finditer(r"[{}]", bare):
        if brace.group() == "{":
            prelude = bare[start : brace.start()].strip()
            read = (not blocks or blocks[-1][2]) and not _off_screen(prelude)
            blocks.append((prelude, brace.end(), read))
        elif not blocks:
            return None
        else:
            prelude, opened, read = blocks.pop()
            inner = bare[opened : brace.start()]
            if not prelude.startswith("@"):
                if "{" in inner:
                    return None  # a nested rule
                if read:
                    rules.append((prelude, inner, not blocks))
        start = brace.end()
    return None if blocks else rules


def _sheet_rules(css: str, *, shows: bool) -> list[tuple[_Selector, _Look]]:
    """Return a stylesheet's selectors that change how text shows, with that change.

    Only the selector's last part is matched, so a rule may fit more elements than it does in a
    browser. That is safe for a rule that hides, so it is kept; a rule that shows (visibility:visible,
    a readable font size, a colour) is kept only when `shows` (a sheet the browser applies on every
    screen), when it is one plain part outside any @media block, and when no rule in the sheet that
    hides was left unread. A selector with a pseudo-class or an attribute test (":hover", "[lang]")
    is left out: it applies only sometimes, or where code cannot tell. A sheet that does not read
    cleanly is read as main read it, rule by rule, and only its hides count.
    """
    css = _CSS_COMMENT.sub(" ", css)
    rules = _css_rules(css)
    if rules is None:
        rules, shows = (
            [(selectors, declarations, False) for selectors, declarations in _RULE.findall(css)],
            False,
        )
    read: list[tuple[_Selector, _Look, bool]] = []
    for selectors, declarations, top_level in rules:
        look = _style_look(declarations)
        for selector in selectors.split(","):
            parts = re.split(r"[\s>+~]+", selector.strip())
            if parts[0].startswith("@"):
                continue
            shape = _COMPOUND.match(parts[-1]) if parts[-1] else None
            if shape is None:
                shows = shows and not look.hides()  # an unread hide could beat any show
                continue
            rest, tag = shape.group("rest"), shape.group("tag")
            exact = _Selector(
                tag=None if tag in (None, "*") else tag.lower(),
                classes=frozenset(re.findall(r"\.([\w-]+)", rest)),
                id=next(iter(re.findall(r"#([\w-]+)", rest)), None),
            )
            read.append((exact, look, top_level and len(parts) == 1))
    found = [(exact, look if shows and plain else look.hiding_only()) for exact, look, plain in read]
    return [(exact, look) for exact, look in found if look != _PLAIN]


class _Sheets(HTMLParser):
    """Collect the stylesheets a browser applies on every screen.

    That is a `<style>` element in the head or body, with no media attribute and a CSS type. A
    `<style>` in a comment, a script, or an attribute is not an element, and is never collected.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.sheets: list[str] = []
        self._open: list[str] = []
        self._sheet: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "style":
            kind = next(((value or "").strip().lower() for name, value in attrs if name == "type"), "")
            applies = set(self._open) <= {"html", "head", "body"} and kind in ("", "text/css")
            self._sheet = [] if applies and not any(name == "media" for name, _ in attrs) else None
        elif tag not in _VOID:
            self._open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            if self._sheet is not None:
                self.sheets.append("".join(self._sheet))
            self._sheet = None
        elif tag in self._open:
            while self._open.pop() != tag:
                pass

    def handle_data(self, data: str) -> None:
        if self._sheet is not None:
            self._sheet.append(data)


def _style_rules(raw: str, section: str) -> tuple[tuple[_Selector, _Look], ...]:
    """Return the rules that change how text shows: hides from any `<style>` text, shows only from sheets."""
    found = [rule for css in _STYLE_BLOCK.findall(raw) for rule in _sheet_rules(css, shows=False)]
    sheets = _Sheets()
    sheets.feed(section)
    sheets.close()
    found += [rule for css in sheets.sheets for rule in _sheet_rules(css, shows=True)]
    return tuple(found)


def _element_look(
    tag: str, attrs: list[tuple[str, str | None]], rules: tuple[tuple[_Selector, _Look], ...]
) -> _Look:
    """Return how an element's tag, its attributes, and the stylesheet rules that fit it show its text.

    A browser reads only the first of repeated attributes, so a repeat may hide text but never show it.
    """
    if tag in _FALLBACK or (tag == "dialog" and not any(name == "open" for name, _ in attrs)):
        return _Look(gone=True)
    if any(name == "hidden" for name, _ in attrs):
        return _Look(gone=True)

    def values(attribute: str) -> list[str]:
        return [value or "" for name, value in attrs if name == attribute]

    styles, classes, ids = values("style"), values("class"), values("id")
    look = _style_look(styles[0]) if styles else _PLAIN
    for extra in styles[1:]:
        look = look | _style_look(extra).hiding_only()
    first = (frozenset(classes[0].split()) if classes else frozenset[str](), frozenset(ids[:1]))
    every = (frozenset(" ".join(classes).split()), frozenset(ids))
    for selector, rule in rules:
        if selector.matches(tag, *first):
            look = look | rule
        elif selector.matches(tag, *every):
            look = look | rule.hiding_only()
    return look


def _describe(tag: str, attrs: list[tuple[str, str | None]]) -> str:
    """Name an element for the log: its tag with its id, class, style, and hidden attributes."""
    shown = (
        f'{name}="{(value or "")[:100]}"'
        for name, value in attrs
        if name in ("id", "class", "style", "hidden")
    )
    return "<" + " ".join((tag, *shown)) + ">"


class _Words(HTMLParser):
    def __init__(
        self,
        *,
        drop_hidden: bool = False,
        rules: tuple[tuple[_Selector, _Look], ...] = (),
        lifted: frozenset[int] = frozenset(),
    ) -> None:
        super().__init__(convert_charrefs=True)
        self._rules = rules
        self._lifted = lifted  # page cloaks: elements, by start order, whose own look is ignored
        self.parts: list[str] = []
        self._hidden = 0
        self._drop_hidden = drop_hidden
        # Open elements: tag, what holds for text inside, and the outermost element around it whose
        # own look hides (by start order), which is what a page cloak is.
        self._open: list[tuple[str, _Look, int | None]] = []
        self._started = 0
        self.hidden_letters: Counter[int] = Counter()  # letters hidden, by the outermost element hiding them
        self.hiders: dict[int, str] = {}

    def _enter(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self._drop_hidden or tag in _VOID:
            return
        number, self._started = self._started, self._started + 1
        look = _PLAIN if number in self._lifted else _element_look(tag, attrs, self._rules)
        _, around, hider = self._open[-1] if self._open else _PAGE
        if hider is None and look.hides():
            hider, self.hiders[number] = number, _describe(tag, attrs)
        self._open.append((tag, look.within(around), hider))

    def _leave(self, tag: str) -> None:
        if not any(name == tag for name, _, _ in self._open):
            return  # a stray end tag closes nothing
        while self._open and self._open.pop()[0] != tag:
            pass

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
        if self._hidden:
            return
        _, here, hider = self._open[-1] if self._open else _PAGE
        if not here.hides():
            self.parts.append(data)
        elif hider is not None:
            self.hidden_letters[hider] += sum(ch.isalpha() for ch in data)


def _tidy(text: str) -> str:
    """One space between words, one line per block, no blank lines, no edge spaces."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    lines = (_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def _read(content: bytes, content_type: str, *, drop_hidden: bool) -> tuple[str, _Words | None, bool]:
    """Return the text, the parser that read it, and whether a page cloak was lifted."""
    raw = content.decode("utf-8", errors="replace")
    if "html" not in content_type.lower():
        return _tidy(raw), None, False
    wrapped = _SEC_TEXT.search(raw)
    section = wrapped.group(1) if wrapped else raw
    rules = _style_rules(raw, section) if drop_hidden else ()
    lifted: frozenset[int] = frozenset()
    while True:
        parser = _Words(drop_hidden=drop_hidden, rules=rules, lifted=lifted)
        parser.feed(section)
        parser.close()
        text = _tidy("".join(parser.parts))
        hiders = [number for number, letters in parser.hidden_letters.items() if letters]
        # A page cloak: one element hides every letter on the page. Lift it and read again.
        if any(ch.isalpha() for ch in text) or len(hiders) != 1 or len(lifted) >= _MAX_CLOAKS:
            return text, parser, bool(lifted)
        lifted |= {hiders[0]}


def document_text(content: bytes, content_type: str) -> str:
    """Return the words of a stored copy as plain text, in reading order."""
    return _read(content, content_type, drop_hidden=False)[0]


def visible_text(content: bytes, content_type: str) -> str:
    """Return `document_text` without the text that markup hides from a reader.

    An element is hidden by the `hidden` attribute; by being a dialog that is not open, or the
    fallback content of a canvas, frame, object, or media player; or by an inline style or a
    stylesheet rule for its tag, classes, or id that sets display:none, opacity under 0.1, a place
    or translation far off the page, a scale of 0, a clip to nothing, or a box 1px or smaller with
    clipped overflow: everything inside it is hidden too. Visibility:hidden, transparent text, and
    a font of 1px or 1pt and under are inherited, as in CSS, until a child sets visibility:visible,
    a colour, or a readable size in absolute units (pt, px, in, cm, mm, pc). Rules for print or
    speech only do not apply. When one element hides every letter on the page, it is a cloak (a
    page hidden until a script or viewer shows it), not a hidden part: it is lifted, and what it
    holds is read with its own hides (ADR 0044). Plain text has no markup, so all of it is visible.
    """
    return _read(content, content_type, drop_hidden=True)[0]


def read_visible(content: bytes, content_type: str) -> tuple[str, bool]:
    """Return `visible_text`, and whether it lifted a page cloak: the page still hides its words."""
    text, _, cloaked = _read(content, content_type, drop_hidden=True)
    return text, cloaked


def hidden_by(content: bytes, content_type: str) -> str | None:
    """Name the element that hides the most letters from a reader, or None if nothing is hidden."""
    parser = _read(content, content_type, drop_hidden=True)[1]
    if parser is None or not +parser.hidden_letters:
        return None
    return parser.hiders[parser.hidden_letters.most_common(1)[0][0]]


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
