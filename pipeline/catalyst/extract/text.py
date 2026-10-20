"""Plain text of a stored copy, and exact quotes within it (spec: Provenance; ADR 0012).

Claims store a quote and its character offsets in this text, so the rules here are versioned:
any change to them is a new `TEXT_VERSION`, and older offsets are re-checked against it.

`document_text` drops comments, scripts, styles, the head, and every attribute (alt text,
titles), but keeps text that markup hides. `visible_text` also drops that hidden text: text an
inline style or a stylesheet rule hides, places off the page, or clips to nothing.
It is what a model reads, and every quote must be found in it too; it places no offsets, so it
has no version of its own (ADR 0018, part 5c). It follows CSS where a reader would see the
text: a child may undo an inherited visibility, font size, or colour, print-only rules do not
apply on screen, and a cloak that hides the whole page is lifted (ADR 0044). It reads the page
as a browser builds it, with html5lib, which follows the HTML standard's parsing rules.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import NamedTuple
from xml.etree.ElementTree import Element

import html5lib

from catalyst.extract.strict_text import visible_text as strict_visible_text
from catalyst.logs import log_event

# A page the loose reader cannot read, or not in time, gets the strict reading, as on main.
# On the 80 recorded pages the most is 5,430 tags, nesting 15 deep, 138 characters of
# stylesheet, and 0.32 s; html5lib takes minutes on 50,000 nested elements. The limits leave
# room for releases many times larger than any on file.
LOOSE_SECONDS = 5.0
_MAX_DEPTH = 500
_MAX_TAGS = 100_000
_MAX_CSS = 200_000
_TAG = re.compile(r"<(/?)([a-zA-Z][^\s/>]*)[^>]*?(/?)>")
# Elements with no end tag: they never deepen the nesting.
_LEAVES = frozenset(
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

_log = logging.getLogger(__name__)


class TooDeep(Exception):
    """The page nests elements deeper than the loose reader reads in time."""


class TooLarge(Exception):
    """The page has more tags or more stylesheet than the loose reader reads in time."""


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
# Elements a stylesheet may sit in and still apply on every screen.
_ROOTS = frozenset({"html", "head", "body"})
# Class names are split on ASCII whitespace only, as in HTML; a no-break space is part of a name.
_CLASS_SPACES = re.compile("[ \t\n\f\r]+")
# Styles that hide an element's text: display:none, visibility:hidden, opacity 0, transparent
# text, a font too small to read (1px or 1pt and under), a place far off the page (an offset or a
# translation of 1,000px or more, or of 100% or 100vw or more), a scale of 0, a clip to nothing, or
# a box 1px or smaller that clips what overflows it. A colour alone does not hide: white text on a
# dark background is ordinary. Filings use small negative offsets (-18pt indents).
_NO_DISPLAY = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*collapse|content-visibility\s*:\s*hidden", re.IGNORECASE
)
_INVISIBLE = re.compile(r"(?<![\w-])visibility\s*:\s*hidden", re.IGNORECASE)
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
# A colour that shows text again: a named colour, #rgb or #rrggbb, or rgb()/hsl() with an alpha of
# 0.1 or more. #rgba and #rrggbbaa are left out, as is anything code cannot judge.
_NAMED_COLOURS = frozenset(
    {"black", "white", "gray", "grey", "red", "blue", "green", "navy", "maroon", "windowtext"}
)
_HEX_COLOUR = re.compile(r"#(?:[0-9a-f]{3}|[0-9a-f]{6})", re.IGNORECASE)
_COLOUR_CALL = re.compile(r"(?:rgb|hsl)a?\((?P<parts>[\d.%\s,/]+)\)", re.IGNORECASE)
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
# Elements whose content a reader does not see: fallback content of embedded media and frames, a
# text box's starting text, and a dialog that is not open.
_FALLBACK = frozenset({"canvas", "iframe", "object", "video", "audio", "noembed", "noframes", "textarea"})
# Stylesheet rules: "selector, selector { declarations }". Rules inside @media blocks are read too,
# except where the media are only print or speech: a reader on screen never meets those.
_STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.DOTALL | re.IGNORECASE)
_CSS_COMMENT = re.compile(r"/\*.*?\*/|<!--|-->", re.DOTALL)
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_ONE_LINE_AT = re.compile(r"@[\w-]+[^{};]*;")  # @import, @charset, @layer a;
_OFF_SCREEN_QUERY = re.compile(r"^(?:only\s+)?(?:print|speech)\b", re.IGNORECASE)
# A page a browser may decode other than as UTF-8 or a Latin alphabet: there, markup can swallow
# other markup (ISO-2022-JP escapes), so nothing on such a page may show what a parent hid.
_CHARSET = re.compile(rb"<meta\b[^>]*?charset\s*=\s*[\"']?\s*(?P<name>[\w.:-]+)", re.IGNORECASE)
_PLAIN_CHARSETS = frozenset({b"utf-8", b"us-ascii", b"iso-8859-1", b"windows-1252"})
_IMPORT = re.compile(r"@import\b", re.IGNORECASE)
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
    """Say whether the style sets visibility:visible, a readable font size, and a colour again.

    A show counts only as the last declaration of its property and after any `all`: a later
    `inherit`, `unset`, `currentcolor`, or `%` would undo it.
    """
    visible = sized = coloured = False
    found = _declarations(style) or []
    last = {name: place for place, (name, _) in enumerate(found)}
    for place, (name, value) in enumerate(found):
        later = {"font-size", "font"} if name in ("font-size", "font") else {name}
        if place < last.get("all", -1) or any(place < last.get(other, -1) for other in later):
            continue
        if name == "visibility":
            visible = visible or _VISIBLE_VALUE.fullmatch(value) is not None
        elif name == "font-size":
            sized = sized or _readable(_SIZE_VALUE.fullmatch(value))
        elif name == "font" and not _CSS_WIDE & set(re.findall(r"[a-z][\w-]*", value.lower())):
            sized = sized or _readable(_FONT_VALUE.fullmatch(value))
        elif name == "color":
            coloured = coloured or _own_colour(value)
    return visible, sized, coloured


def _own_colour(value: str) -> bool:
    """Say whether a colour value is one a reader can see: solid, or at least 10% opaque."""
    value = re.sub(r"\s*!\s*important$", "", value, flags=re.IGNORECASE).strip()
    if value.lower() in _NAMED_COLOURS or _HEX_COLOUR.fullmatch(value):
        return True
    call = _COLOUR_CALL.fullmatch(value)
    parts = [part for part in re.split(r"[\s,/]+", call.group("parts")) if part] if call else []
    if len(parts) == 3:
        return True
    alpha = _number(parts[3].rstrip("%")) if len(parts) == 4 else None
    return alpha is not None and (alpha / 100 if parts[3].endswith("%") else alpha) >= 0.1


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

    def matches_loosely(self, tag: str, classes: frozenset[str], ids: frozenset[str]) -> bool:
        """Say whether the selector fits when the case of tags, classes, and ids is ignored."""
        return (
            (self.tag is None or self.tag == tag.lower())
            and {name.lower() for name in self.classes} <= {name.lower() for name in classes}
            and (self.id is None or self.id.lower() in {name.lower() for name in ids})
        )


def _off_screen(prelude: str) -> bool:
    """Say whether an @media block is for print or speech only, so a reader on screen never meets it."""
    if not prelude.lower().startswith("@media"):
        return False
    return all(_OFF_SCREEN_QUERY.match(query.strip()) for query in prelude[len("@media") :].split(","))


def _css_rules(css: str) -> list[tuple[str, str, bool]] | None:
    """Return each rule as (selectors, declarations, top level), leaving out print-only @media.

    A rule inside any at-rule block (such as @media) is not top level: it applies only on some
    screens. Strings are emptied and one-line at-rules dropped before braces are counted, and
    brackets are counted with them, since an open bracket swallows the rest of a sheet. None when
    the sheet does not read cleanly: an escape, an open string or bracket, a nested rule, or
    unbalanced braces.
    """
    bare = _strings_out(css)
    if bare is None or not _brackets_close(bare):
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


def _brackets_close(css: str) -> bool:
    """Say whether every ( and [ closes in order, with no brace between: an open one swallows the sheet."""
    opened: list[str] = []
    for mark in re.finditer(r"[{}()\[\]]", css):
        sign = mark.group()
        if sign in "([":
            opened.append(sign)
        elif sign in _BRACKETS:
            if not opened or opened.pop() != _BRACKETS[sign]:
                return False
        elif opened:
            return False
    return not opened


def _sheet_rules(css: str, *, shows: bool) -> tuple[list[tuple[_Selector, _Look]], bool]:
    """Return a stylesheet's selectors that change how text shows, and whether it has an unread hide.

    Only the selector's last part is matched, so a rule may fit more elements than it does in a
    browser. That is safe for a rule that hides, so it is kept; a rule that shows (visibility:visible,
    a readable font size, a colour) is kept only when `shows` (a sheet the browser applies on every
    screen) and when it is one plain part outside any @media block. A selector with a pseudo-class
    or an attribute test (":hover", "[lang]") is left out: it applies only sometimes, or where code
    cannot tell. A sheet that does not read cleanly is read as main read it, rule by rule, hides
    only. An unread rule that hides, or a sheet that does not read cleanly, is reported, because
    such a rule may beat any show on the page.
    """
    css = _CSS_COMMENT.sub(" ", css)
    rules = _css_rules(css)
    unread = rules is None
    if rules is None:
        rules = [(selectors, declarations, False) for selectors, declarations in _RULE.findall(css)]
    read: list[tuple[_Selector, _Look, bool]] = []
    for selectors, declarations, top_level in rules:
        look = _style_look(declarations)
        for selector in selectors.split(","):
            parts = re.split(r"[\s>+~]+", selector.strip())
            if parts[0].startswith("@"):
                continue
            shape = _COMPOUND.match(parts[-1]) if parts[-1] else None
            if shape is None:
                unread = unread or look.hides()
                continue
            rest, tag = shape.group("rest"), shape.group("tag")
            exact = _Selector(
                tag=None if tag in (None, "*") else tag.lower(),
                classes=frozenset(re.findall(r"\.([\w-]+)", rest)),
                id=next(iter(re.findall(r"#([\w-]+)", rest)), None),
            )
            read.append((exact, look, top_level and len(parts) == 1))
    found = [(exact, look if shows and plain else look.hiding_only()) for exact, look, plain in read]
    return [(exact, look) for exact, look in found if look != _PLAIN], unread


def _local(tag: str) -> str:
    """Return a tag's own name: html5lib names SVG and MathML tags with their namespace, as "{ns}style"."""
    return tag.rpartition("}")[2]


def _other_encoding(content: bytes) -> bool:
    """Say whether a browser may read the copy in an encoding other than UTF-8 or a Latin alphabet."""
    if b"\x1b" in content or content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    return any(found.group("name").lower() not in _PLAIN_CHARSETS for found in _CHARSET.finditer(content))


def _survey(root: Element) -> tuple[list[str], bool]:
    """Return the stylesheets a browser applies on every screen, and whether any style may be unread.

    A stylesheet is a `<style>` element whose parents are only html, head, and body, with no media
    attribute and a type of CSS (compared exactly, ignoring case only). `<style>` text in a comment,
    a script, an attribute, or any other element's text is not an element of the tree. A style may
    be unread when an inline style does not read cleanly or a `<link rel=stylesheet>` brings one in.
    """
    sheets: list[str] = []
    unread_inline = False
    todo: list[tuple[Element, bool]] = [(root, True)]
    while todo:
        element, plain_parents = todo.pop()
        if callable(element.tag):  # a comment
            continue
        tag, style = _local(element.tag), element.get("style")
        unread_inline = unread_inline or (style is not None and _declarations(style) is None)
        links = tag == "link" and "stylesheet" in _CLASS_SPACES.split(element.get("rel", "").lower())
        unread_inline = unread_inline or links
        if tag == "style":
            kind = element.get("type")
            if plain_parents and element.get("media") is None and (kind or "").lower() in ("", "text/css"):
                sheets.append(element.text or "")
        todo.extend((child, plain_parents and tag in _ROOTS) for child in element)
    return sheets, unread_inline


@dataclass(frozen=True)
class _Styles:
    """The page's stylesheet rules, and whether anything on the page may show what a parent hid."""

    rules: tuple[tuple[_Selector, _Look], ...]
    shows: bool


def _style_rules(raw: str, root: Element, *, shows: bool) -> _Styles:
    """Return the rules that change how text shows, and whether any show (inline or by rule) counts.

    Hides come from any `<style>` text in the copy, read loosely. Shows count only while `shows`
    (the encoding is plain) and while no stylesheet and no inline style on the page holds a hide, or
    a style, that code cannot read, and no sheet comes from elsewhere (`@import`, a stylesheet link):
    such a rule could beat any show, inline or not. Stylesheet shows come only from the tree.
    """
    styles = _STYLE_BLOCK.findall(raw)
    hides = [_sheet_rules(css, shows=False) for css in styles]
    sheets, unread_inline = _survey(root)
    shown = [_sheet_rules(css, shows=True) for css in sheets]
    shows = shows and not unread_inline and not any(unread for _, unread in (*hides, *shown))
    shows = shows and not any(_IMPORT.search(css) for css in styles)
    found = [rule for rules, _ in hides for rule in rules]
    found += [(exact, look if shows else look.hiding_only()) for rules, _ in shown for exact, look in rules]
    return _Styles(tuple(found), shows)


def _never_shown(tag: str, attrs: Mapping[str, str]) -> bool:
    """Say whether a browser never shows the element's text: a frame, media, a text box, a closed dialog."""
    return tag in _FALLBACK or (tag == "dialog" and "open" not in attrs)


def _element_look(tag: str, attrs: Mapping[str, str], styles: _Styles) -> _Look:
    """Return how an element's tag, its attributes, and the stylesheet rules that fit it show its text.

    A rule that fits only when case is ignored (as in a page without a doctype) may hide, never show.
    When the page may not show (`styles.shows`), the inline style may hide, never show.
    """
    if _never_shown(tag, attrs) or "hidden" in attrs:
        return _Look(gone=True)
    look = _style_look(attrs.get("style", ""))
    look = look if styles.shows else look.hiding_only()
    rules = styles.rules
    classes = frozenset(name for name in _CLASS_SPACES.split(attrs.get("class", "")) if name)
    ids = frozenset({attrs["id"]} if "id" in attrs else ())
    for selector, rule in rules:
        if selector.matches(tag, classes, ids):
            look = look | rule
        elif selector.matches_loosely(tag, classes, ids):
            look = look | rule.hiding_only()
    return look


def _describe(tag: str, attrs: Mapping[str, str]) -> str:
    """Name an element for the log by its tag and the kind of hide only: never any page text."""
    if _never_shown(tag, attrs):
        kind = "never shown"
    elif "hidden" in attrs:
        kind = "hidden attribute"
    elif _style_look(attrs.get("style", "")).hides():
        kind = "style"
    else:
        kind = "stylesheet rule"
    return f"{tag}: {kind}"


@dataclass
class _Walk:
    """What one walk of the tree found: the visible text, and the letters each hiding element hides."""

    parts: list[str] = field(default_factory=list[str])
    hidden_letters: Counter[int] = field(default_factory=Counter[int])
    hiders: dict[int, str] = field(default_factory=dict[int, str])
    never_shown: set[int] = field(default_factory=set[int])  # hiders a browser never shows: never a cloak


_Todo = list[tuple["Element | str | None", _Look, "int | None"]]


def _walk(root: Element, styles: _Styles, lifted: frozenset[int], deadline: float) -> _Walk:
    """Walk the tree in reading order and keep the text a reader sees.

    Hidden letters are counted against the outermost element whose own look hides them, so a page
    cloak (one such element hiding every letter) can be found. Elements in `lifted` are cloaks: their
    own look is ignored, but every hide inside them still holds. Past `deadline`, it stops.
    """
    walk = _Walk()
    steps = 0
    # To do, last first: an element to enter, a text, or None for the line that ends a block.
    todo: _Todo = [(root, _PLAIN, None)]
    while todo:
        if steps % 1_000 == 0 and time.monotonic() >= deadline:
            raise TimeoutError
        steps += 1
        item, around, hider = todo.pop()
        if item is None:
            walk.parts.append("\n")
        elif isinstance(item, str):
            if not around.hides():
                walk.parts.append(item)
            elif hider is not None:
                walk.hidden_letters[hider] += sum(ch.isalpha() for ch in item)
        elif not callable(item.tag) and _local(item.tag) not in _HIDDEN:  # a comment's tag is a function
            _enter(walk, todo, item, around, hider, styles, lifted)
    return walk


def _enter(
    walk: _Walk,
    todo: _Todo,
    element: Element,
    around: _Look,
    hider: int | None,
    styles: _Styles,
    lifted: frozenset[int],
) -> None:
    """Start an element: note what it hides, mark a new line or cell, and queue what it holds."""
    tag, attrs = _local(element.tag), element.attrib
    look = _PLAIN if id(element) in lifted else _element_look(tag, attrs, styles)
    if hider is None and look.hides():
        hider = id(element)
        walk.hiders[hider] = _describe(tag, attrs)
        if _never_shown(tag, attrs):
            walk.never_shown.add(hider)
    inside = look.within(around)
    if tag in _BLOCKS:
        walk.parts.append("\n")
        todo.append((None, inside, hider))
    elif tag in _CELLS:
        walk.parts.append(" ")
    for child in reversed(element):
        if child.tail:
            todo.append((child.tail, inside, hider))
        todo.append((child, inside, hider))
    if element.text:
        todo.append((element.text, inside, hider))


class _Words(HTMLParser):
    """Read a copy tag by tag for `document_text`, whose offsets are fixed by `TEXT_VERSION`."""

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


def _section(content: bytes) -> tuple[str, str]:
    """Return the whole copy as text, and the part that is the document (inside SEC's <TEXT>)."""
    raw = content.decode("utf-8", errors="replace")
    wrapped = _SEC_TEXT.search(raw)
    return raw, wrapped.group(1) if wrapped else raw


def document_text(content: bytes, content_type: str) -> str:
    """Return the words of a stored copy as plain text, in reading order."""
    raw, section = _section(content)
    if "html" not in content_type.lower():
        return _tidy(raw)
    parser = _Words()
    parser.feed(section)
    parser.close()
    return _tidy("".join(parser.parts))


class PageReading(NamedTuple):
    """What a reader sees of a page, whether that differs from the strict reading, and what hides."""

    visible: str
    # The visible text is not what main's strict reader (`strict_visible_text`) gives: a show, a
    # lifted cloak, or a print rule let text through that the strict reader hides. Every claim from
    # such a copy goes to a person (ADR 0044, decision 6).
    differs: bool
    hidden_by: str | None  # the element hiding the most letters, for the log; None if nothing is hidden


def read_page(content: bytes, content_type: str) -> PageReading:
    """Read a stored copy once, as a browser builds it, and return what a reader sees (`visible_text`).

    A copy the loose reader cannot read (html5lib fails), or cannot read in time, gets the strict
    reading, exactly as main reads it, and the log says why by the error's name only.
    """
    try:
        return _read_loose(content, content_type, time.monotonic() + LOOSE_SECONDS)
    except Exception as error:  # any failure of the loose reader falls back to main's
        log_event(_log, "loose_reader_fell_back", reason=type(error).__name__)
        return PageReading(strict_visible_text(content, content_type), False, None)


def _check_size(section: str) -> None:
    """Refuse a page too deep or too large to read in time, from cheap counts made before parsing."""
    depth = deepest = tags = 0
    for found in _TAG.finditer(section):
        tags += 1
        if found.group(1):
            depth = max(0, depth - 1)
        elif not found.group(3) and found.group(2).lower() not in _LEAVES:
            depth += 1
            deepest = max(deepest, depth)
    if deepest > _MAX_DEPTH:
        raise TooDeep
    if tags > _MAX_TAGS or sum(len(css) for css in _STYLE_BLOCK.findall(section)) > _MAX_CSS:
        raise TooLarge


def _read_loose(content: bytes, content_type: str, deadline: float) -> PageReading:
    raw, section = _section(content)
    if "html" not in content_type.lower():
        return PageReading(_tidy(raw), False, None)
    _check_size(section)
    # With scripts on, as a reader's browser has them: noscript is then text that is never shown.
    root = html5lib.parse(section, treebuilder="etree", namespaceHTMLElements=False, scripting=True)  # pyright: ignore[reportUnknownMemberType]
    styles = _style_rules(raw, root, shows=not _other_encoding(content))
    lifted: frozenset[int] = frozenset()
    while True:
        walk = _walk(root, styles, lifted, deadline)
        text = _tidy("".join(walk.parts))
        hiders = [number for number, letters in walk.hidden_letters.items() if letters]
        # A page cloak: one element hides every letter on the page. Lift it and read again.
        liftable = len(hiders) == 1 and hiders[0] not in walk.never_shown and len(lifted) < _MAX_CLOAKS
        if any(ch.isalpha() for ch in text) or not liftable:
            top = walk.hidden_letters.most_common(1)
            hidden = walk.hiders[top[0][0]] if top and top[0][1] else None
            return PageReading(text, text != strict_visible_text(content, content_type), hidden)
        lifted |= {hiders[0]}


def visible_text(content: bytes, content_type: str) -> str:
    """Return `document_text` without the text that markup hides from a reader.

    The page is parsed as the HTML standard says a browser parses it (html5lib), so implied ends,
    ignored tags, and table text moved out of a table land where a browser puts them. Text in a
    script, noscript, template, title, or style is never page text. An element is hidden by the
    `hidden` attribute; by being a dialog that is not open, or a canvas, frame, object, media
    player, noembed, noframes, or textarea; or by an inline style or a stylesheet rule for its tag,
    classes, or id that sets display:none, opacity under 0.1, a place or translation far off the
    page, a scale of 0, a clip to nothing, or a box 1px or smaller with clipped overflow: everything
    inside it is hidden too. Visibility:hidden, transparent text, and a font of 1px or 1pt and under
    are inherited, as in CSS, until a child sets visibility:visible, a colour, or a readable size in
    absolute units (pt, px, in, cm, mm, pc). Rules for print or speech only do not apply. When one
    element hides every letter on the page, it is a cloak (a page hidden until a script or viewer
    shows it), not a hidden part: it is lifted, and what it holds is read with its own hides; the
    page still hides its words, and `read_page` says so (ADR 0044). Plain text is all visible.
    """
    return read_page(content, content_type).visible


def read_visible(content: bytes, content_type: str) -> tuple[str, bool]:
    """Return `visible_text`, and whether it differs from the strict reading: hold its claims if so."""
    reading = read_page(content, content_type)
    return reading.visible, reading.differs


def hidden_by(content: bytes, content_type: str) -> str | None:
    """Name the element that hides the most letters from a reader, or None if nothing is hidden."""
    return read_page(content, content_type).hidden_by


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
