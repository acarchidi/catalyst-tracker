"""`visible_text` hides only what a reader cannot see (ADR 0018 part 5c, ADR 0044).

The live run on 2026-10-20 skipped 56 releases as having no readable text: `visible_text` gave
nothing at all for them, from every filing agent. These tests take the recorded releases (the gold
set and the injection fixtures) and add the markup that blanks a whole page, or that a child
element overrides, and check that the reader's words come back while every planted hide stays.
"""

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from catalyst.extract.text import (
    PageReading,
    document_text,
    hidden_by,
    read_page,
    read_visible,
    strict_visible_text,
    visible_text,
)
from conftest import FIXTURES, REPO_ROOT

GOLD = sorted((REPO_ROOT / "evals" / "extract_catalysts" / "documents").glob("*.htm"))
HOSTILE = sorted((FIXTURES / "injection").glob("*.html"))
MARCH = "March 1, 2027"


def _head(raw: str, markup: str) -> str:
    return re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + markup, raw, count=1, flags=re.IGNORECASE)


def _wrap_body(raw: str, start: str) -> str:
    body = re.search(r"<body[^>]*>", raw, re.IGNORECASE)
    end = raw.lower().rfind("</body>")
    assert body is not None
    assert end > body.end()
    return raw[: body.end()] + start + raw[body.end() : end] + "</div>" + raw[end:]


# Ways a page hides all of itself until a script, a viewer, or a printer shows it.
CLOAKS: dict[str, Callable[[str], str]] = {
    "body rule": lambda raw: _head(raw, "<style>body { display: none !important; }</style>"),
    "html rule": lambda raw: _head(raw, "<style>html{visibility:hidden}</style>"),
    "body style": lambda raw: re.sub(
        r"<body", '<body style="display:none"', raw, count=1, flags=re.IGNORECASE
    ),
    "wrapper": lambda raw: _wrap_body(raw, '<div style="display:none">'),
    "print-only rule": lambda raw: _head(
        raw, "<style>@media print { body * { visibility: hidden } }</style>"
    ),
}


@pytest.mark.parametrize("cloak", CLOAKS.values(), ids=CLOAKS.keys())
@pytest.mark.parametrize("path", GOLD, ids=[p.name[:12] for p in GOLD])
def test_a_recorded_release_under_a_page_cloak_reads_as_before(
    path: Path, cloak: Callable[[str], str]
) -> None:
    raw = path.read_text(encoding="utf-8")
    cloaked = cloak(raw)
    assert cloaked != raw
    assert visible_text(cloaked.encode(), "text/html") == visible_text(raw.encode(), "text/html")


@pytest.mark.parametrize("cloak", CLOAKS.values(), ids=CLOAKS.keys())
@pytest.mark.parametrize("path", HOSTILE, ids=[p.stem for p in HOSTILE])
def test_a_hostile_fixture_under_a_page_cloak_still_hides_what_it_planted(
    path: Path, cloak: Callable[[str], str]
) -> None:
    """Lifting a page cloak lifts only the cloak: every hide inside it still hides."""
    raw = path.read_text(encoding="utf-8")
    cloaked = cloak(raw)
    visible = visible_text(cloaked.encode(), "text/html")
    assert visible == visible_text(raw.encode(), "text/html")
    assert (MARCH in visible) == (MARCH in visible_text(raw.encode(), "text/html"))


def _page(html: str) -> bytes:
    return f"<html><head></head><body>{html}</body></html>".encode()


INLINE_XBRL = (
    '<div style="display:none"><ix:header><ix:hidden><ix:nonNumeric name="dei:EntityRegistrantName" '
    'contextRef="c-1">Acme Bio, Inc.</ix:nonNumeric></ix:hidden><ix:references><link:schemaRef '
    'xlink:href="acme-20261001.xsd" xlink:type="simple"/></ix:references></ix:header></div>'
    '<div style="text-align:center"><span style="font-size:10pt">Seen.</span></div>'
)
SEEN = {
    "inline XBRL header beside the body": INLINE_XBRL,
    "zero font parent, sized child": "<div style='font-size:0'><b style='font-size:9pt'>Seen.</b></div>",
    "1pt parent, font shorthand child": (
        "<div style='font-size:1pt'><p style='font: 10pt Times New Roman, Times, Serif'>Seen.</p></div>"
    ),
    "tiny parent, class sets the size": (
        "<style>p.MsoNormal { font-size: 12.0pt }</style>"
        "<div style='font-size:1px'><p class='MsoNormal'>Seen.</p></div>"
    ),
    "hidden parent, visible child": (
        "<div style='visibility:hidden'><p style='visibility:visible'>Seen.</p></div>"
    ),
    "transparent parent, coloured child": (
        "<div style='color:transparent'><p style='color:#000000'>Seen.</p></div>"
    ),
    "print-only rule": "<style>@media print { .no { display:none } }</style><p class='no'>Seen.</p>",
}
STILL_HIDDEN = {
    "em child of a zero font": "<div style='font-size:0'><span style='font-size:2em'>Hidden.</span></div>",
    "display none is not undone": (
        "<div style='display:none'><p style='display:block;visibility:visible'>Hidden.</p></div>"
    ),
    "opacity is not undone": "<div style='opacity:0'><p style='opacity:1'>Hidden.</p></div>",
    "sized but transparent": (
        "<div style='font-size:0;color:transparent'><span style='font-size:10pt'>Hidden.</span></div>"
    ),
    "collapsed row": (
        "<table><tr style='visibility:collapse'><td style='visibility:visible'>Hidden.</td></tr></table>"
    ),
    "reveal needs a missing ancestor": (
        "<style>.nope * { visibility: visible }</style><div style='visibility:hidden'><p>Hidden.</p></div>"
    ),
    "reveal on tiny screens only": (
        "<style>@media (max-width: 1px) { p { visibility: visible } }</style>"
        "<div style='visibility:hidden'><p>Hidden.</p></div>"
    ),
    "screen rule": "<style>@media screen { .x { display: none } }</style><p class='x'>Hidden.</p>",
    "print or any screen": (
        "<style>@media print, (min-width: 0) { .x { display:none } }</style><p class='x'>Hidden.</p>"
    ),
    # A browser ignores an invalid or unresolved declaration, and the child inherits the hide.
    "shorthand without a family": "<div style='font-size:0'><p style='font: 10pt'>Hidden.</p></div>",
    "size with junk": "<div style='font-size:0'><p style='font-size: 10pt x'>Hidden.</p></div>",
    "colour from a variable": "<div style='color:transparent'><p style='color:var(--c)'>Hidden.</p></div>",
    # A browser reads only the first of repeated attributes.
    "second style attribute": (
        "<div style='visibility:hidden'><p style='color:red' style='visibility:visible'>Hidden.</p></div>"
    ),
    "second class attribute": (
        "<style>.show { visibility: visible }</style>"
        "<div style='visibility:hidden'><p class='a' class='show'>Hidden.</p></div>"
    ),
}


@pytest.mark.parametrize("html", SEEN.values(), ids=SEEN.keys())
def test_what_a_reader_sees_is_kept(html: str) -> None:
    assert visible_text(_page(html), "text/html") == "Seen."


@pytest.mark.parametrize("html", STILL_HIDDEN.values(), ids=STILL_HIDDEN.keys())
def test_what_a_reader_cannot_see_stays_hidden(html: str) -> None:
    content = _page(html + "<p>Seen.</p>")
    assert visible_text(content, "text/html") == "Seen."
    assert "Hidden." in document_text(content, "text/html")


def test_only_a_cloak_that_holds_every_word_is_lifted() -> None:
    # A visible line beside the hidden part: the part is a hide, not a cloak.
    assert visible_text(_page('<p>Seen.</p><div style="display:none">Hidden.</div>'), "text/html") == "Seen."
    # Two hidden parts: neither holds every word, so neither is lifted.
    two = '<div style="display:none">Hidden one.</div><div style="display:none">Hidden two.</div>'
    assert visible_text(_page(two), "text/html") == ""
    # Inside a lifted cloak, its own hides still hide.
    inner = '<div style="display:none"><p>Seen.</p><p style="font-size:1px">Hidden.</p></div>'
    assert visible_text(_page(inner), "text/html") == "Seen."


def test_hidden_by_names_what_hides_the_most_words() -> None:
    two = '<div style="display:none">Hidden.</div><p class="x" style="opacity:0">Hidden more words.</p>'
    assert hidden_by(_page(two), "text/html") == "p: style"
    assert hidden_by(_page("<p>Seen.</p>"), "text/html") is None
    assert hidden_by(b"plain words", "text/plain") is None


# Opus review of PR #91: inputs that showed the model text that main kept hidden. Each must stay hidden.
_UNDER_HIDDEN = "<div style='visibility:hidden'><p class='s'>INJ</p></div>"
REVIEW_LEAKS = {
    # 1. A show the browser discards: the style does not read cleanly as declarations.
    "brace before the size": "<div style='font-size:0'><p style='x:{;font-size:10pt'>INJ</p></div>",
    "open bracket": "<div style='visibility:hidden'><p style='a:(;visibility:visible'>INJ</p></div>",
    "open quote": '<div style="visibility:hidden"><p style="a:\';visibility:visible">INJ</p></div>',
    "inherit as the family": "<div style='font-size:0'><p style='font: 10pt inherit'>INJ</p></div>",
    "unclosed family": "<div style='font-size:0'><p style='font: 10pt \"Times'>INJ</p></div>",
    # 2. A stylesheet the browser never applies.
    "style in a comment": "<!-- <style>.s{visibility:visible}</style> -->" + _UNDER_HIDDEN,
    "style in a script": "<script>var a = '<style>.s{visibility:visible}</style>';</script>" + _UNDER_HIDDEN,
    "style in an attribute": "<div title='<style>.s{visibility:visible}</style>'></div>" + _UNDER_HIDDEN,
    "style for tiny screens": "<style media='(max-width:1px)'>.s{visibility:visible}</style>" + _UNDER_HIDDEN,
    "style that is not CSS": "<style type='text/plain'>.s{visibility:visible}</style>" + _UNDER_HIDDEN,
    # 3. Braces inside strings, and one-line at-rules.
    "brace in a print string": (
        "<style>@media print { .a{content:\"{\"} } .x{display:none}</style><p class='x'>INJ</p>"
    ),
    "brace lifts a show out of media": (
        '<style>@media (max-width:1px) { .a{content:"}"} .s{visibility:visible} }</style>' + _UNDER_HIDDEN
    ),
    "after @import": "<style>@import url(a.css); .x{display:none}</style><p class='x'>INJ</p>",
    "after @charset": "<style>@charset \"utf-8\"; .x{display:none}</style><p class='x'>INJ</p>",
    "after @layer": "<style>@layer a; .x{display:none}</style><p class='x'>INJ</p>",
    # 4. A hide rule the code cannot read beats the show.
    "attribute rule hides": (
        "<style>.s{visibility:visible} .s[class]{visibility:hidden}</style>" + _UNDER_HIDDEN
    ),
    "not() rule hides": "<style>.s{visibility:visible} .s:not(.z){visibility:hidden}</style>" + _UNDER_HIDDEN,
}


@pytest.mark.parametrize("html", REVIEW_LEAKS.values(), ids=REVIEW_LEAKS.keys())
def test_text_the_review_found_shown_stays_hidden(html: str) -> None:
    content = _page(html + "<p>Seen.</p>")
    assert "INJ" in document_text(content, "text/html")
    assert visible_text(content, "text/html") == "Seen."


def test_a_lifted_cloak_is_reported() -> None:
    """A page that hides everything is read, but a person still checks it (`source_hides_text`)."""
    cloaked = _page("<div style='display:none'><p>Seen.</p></div>")
    assert read_visible(cloaked, "text/html") == ("Seen.", True)
    assert read_visible(_page("<p>Seen.</p><p hidden>Hidden.</p>"), "text/html") == ("Seen.", False)
    assert read_visible(b"plain", "text/plain") == ("plain", False)


def _hidden(inner: str) -> str:
    return f"<div style='visibility:hidden'>{inner}</div>"


_S = "<p class='s'>INJECT</p>"
# Second Opus review of PR #91. Each keeps INJECT hidden in a browser; each showed it on the first fix.
ROUND_TWO_LEAKS = {
    # 1. The browser builds a different tree from the markup than a tag-by-tag reader does.
    "p closed by p": _hidden("<p style='visibility:visible'>ok<p>INJECT"),
    "p closed by div": _hidden("<p style='visibility:visible'>ok<div>INJECT</div>"),
    "li closed by li": _hidden("<ul><li style='visibility:visible'>ok<li>INJECT</ul>"),
    "option closed by option": _hidden(
        "<select><option style='visibility:visible'>ok<option>INJECT</select>"
    ),
    "dt closed by dd": _hidden("<dl><dt style='visibility:visible'>ok<dd>INJECT</dl>"),
    "dd closed by dt": _hidden("<dl><dd style='visibility:visible'>ok<dt>INJECT</dl>"),
    "td outside a table": _hidden("<td style='visibility:visible'>INJECT</td>"),
    "caption outside a table": _hidden("<caption style='visibility:visible'>INJECT</caption>"),
    "nested form": _hidden("<form><form style='visibility:visible'>INJECT</form></form>"),
    "second body": _hidden("<body style='visibility:visible'>INJECT"),
    "second html": _hidden("<html style='visibility:visible'>INJECT"),
    "table text": _hidden("<table style='visibility:visible'>INJECT<tr><td>cell</td></tr></table>"),
    # 2. An open bracket swallows the rest of the sheet.
    "open bracket before a rule": "<style>x( {} .s{visibility:visible}</style>" + _hidden(_S),
    "open square bracket": "<style>x[ {} .s{visibility:visible}</style>" + _hidden(_S),
    "open bracket in an at-rule": "<style>@x ( ; .s{visibility:visible}</style>" + _hidden(_S),
    # 3. A type with a leading space is not CSS.
    "type with a space": "<style type=' text/css'>.s{visibility:visible}</style>" + _hidden(_S),
    # 4. A stylesheet written inside script text.
    "style in a template script": (
        "<script type='text/template'><style>.s{visibility:visible}</style></script>" + _hidden(_S)
    ),
    # 5. Class names split only on ASCII spaces.
    "no-break space in class": "<style>.s{visibility:visible}</style>"
    + _hidden("<p class='a\u00a0s'>INJECT</p>"),
    "em space in class": "<style>.s{visibility:visible}</style>" + _hidden("<p class='a\u2003s'>INJECT</p>"),
    "vertical tab in class": "<style>.s{visibility:visible}</style>"
    + _hidden("<p class='a\x0bs'>INJECT</p>"),
    # Optional items: any unreadable hide anywhere stops every stylesheet show.
    "unreadable hide in another sheet": (
        "<style>.s{visibility:visible}</style><style>.s:not(.z){visibility:hidden}</style>" + _hidden(_S)
    ),
    "unreadable inline style": "<style>.s{visibility:visible}</style><i style='a:('>i</i>" + _hidden(_S),
    "show rule is case-sensitive": "<style>.S{visibility:visible}</style>" + _hidden(_S),
    "hide rule ignores case": "<style>.X{display:none}</style><p class='x'>INJECT</p>",
    "hide id ignores case": "<style>#X{display:none}</style><p id='x'>INJECT</p>",
    # Colours with little or no alpha, and font sizes code cannot judge, never show.
    "rgba near zero": "<div style='color:transparent'><p style='color:rgba(0,0,0,0.05)'>INJECT</p></div>",
    "hsla zero": "<div style='color:transparent'><p style='color:hsla(0,0%,0%,0)'>INJECT</p></div>",
    "slash alpha 5%": "<div style='color:transparent'><p style='color:rgb(0 0 0 / 5%)'>INJECT</p></div>",
    "hex with alpha": "<div style='color:transparent'><p style='color:#000000ff'>INJECT</p></div>",
    "short hex with alpha": "<div style='color:transparent'><p style='color:#000f'>INJECT</p></div>",
    "vw size": "<div style='font-size:0'><p style='font-size:10vw'>INJECT</p></div>",
    "percent size": "<div style='font-size:0'><p style='font-size:500%'>INJECT</p></div>",
    "rem size": "<div style='font-size:0'><p style='font-size:2rem'>INJECT</p></div>",
    "calc size": "<div style='font-size:0'><p style='font-size:calc(10pt)'>INJECT</p></div>",
}


@pytest.mark.parametrize("html", ROUND_TWO_LEAKS.values(), ids=ROUND_TWO_LEAKS.keys())
def test_text_the_second_review_found_shown_stays_hidden(html: str) -> None:
    content = _page(html + "<p>Seen.</p>")
    assert "INJECT" in document_text(content, "text/html")
    visible = visible_text(content, "text/html")
    assert "INJECT" not in visible
    assert visible.endswith("Seen.")


def test_text_that_is_never_page_text() -> None:
    """Script, noscript, template, noembed, noframes, iframe, textarea, and title hold no page text."""
    hidden = "".join(
        f"<{tag}>INJECT</{tag}>"
        for tag in ("script", "noscript", "template", "noembed", "noframes", "iframe", "textarea", "title")
    )
    assert visible_text(_page(hidden + "<p>Seen.</p>"), "text/html") == "Seen."


def test_one_reading_gives_the_text_the_cloak_and_what_hides() -> None:
    cloaked = _page("<div style='display:none'><p>Seen.</p></div>")
    assert read_page(cloaked, "text/html") == PageReading("Seen.", True, None)
    two = _page("<p>Seen.</p><p style='opacity:0'>Hidden words.</p>")
    assert read_page(two, "text/html") == PageReading("Seen.", False, "p: style")


# Third Opus review of PR #91. Each keeps INJECT hidden in a browser.
ROUND_THREE_LEAKS = {
    # B1. Tags inside SVG and MathML carry a namespace, and must be judged by their own name.
    "svg style": "<svg><style>INJECT</style></svg>",
    "svg script": "<svg><script>INJECT</script></svg>",
    "svg title": "<svg><title>INJECT</title></svg>",
    "math noscript": "<math><noscript>INJECT</noscript></math>",
    "svg template": "<svg><template>INJECT</template></svg>",
    # B2. With scripts on, a browser reads noscript as plain text that is never shown.
    "head noscript text": "<noscript>INJECT</noscript>",
    "head noscript stylesheet": (
        "<noscript><p></p><style>.s{visibility:visible}</style></noscript>"
        + _hidden("<p class='s'>INJECT</p>")
    ),
    # B3. content-visibility:hidden is not visibility:hidden, and no child undoes it.
    "content-visibility": (
        "<div style='content-visibility:hidden'><span style='visibility:visible'>INJECT</span></div>"
    ),
    # B5. An unread stylesheet hide may beat an inline show too.
    "attribute rule beats inline show": (
        "<style>[data-x]{visibility:hidden !important}</style>"
        + _hidden("<span data-x style='visibility:visible'>INJECT</span>")
    ),
    "not() rule beats inline show": (
        "<style>span:not(.z){visibility:hidden !important}</style>"
        + _hidden("<span style='visibility:visible'>INJECT</span>")
    ),
    # Optional: a sheet from elsewhere could hide again, so nothing shows.
    "outside stylesheet": "<link rel='stylesheet' href='a.css'>"
    + _hidden("<p style='visibility:visible'>INJECT</p>"),
    "imported stylesheet": "<style>@import url(a.css);</style>"
    + _hidden("<p style='visibility:visible'>INJECT</p>"),
}


@pytest.mark.parametrize("html", ROUND_THREE_LEAKS.values(), ids=ROUND_THREE_LEAKS.keys())
def test_text_the_third_review_found_shown_stays_hidden(html: str) -> None:
    content = _page(html + "<p>Seen.</p>")
    visible = visible_text(content, "text/html")
    assert "INJECT" not in visible
    assert visible.endswith("Seen.")


def test_head_noscript_text_stays_out_of_the_body() -> None:
    content = b"<html><head><noscript>INJECT</noscript></head><body><p>Seen.</p></body></html>"
    assert visible_text(content, "text/html") == "Seen."
    sheet = (
        b"<html><head><noscript><p></p><style>.s{visibility:visible}</style></noscript></head><body>"
        b"<div style='visibility:hidden'><p class='s'>INJECT</p></div><p>Seen.</p></body></html>"
    )
    assert visible_text(sheet, "text/html") == "Seen."


ESCAPED = (
    b"<p>V</p><div style='visibility:hidden'>\x1b$B<span/style='visibility:visible;'>\x1b(BINJECT</div>"
    b"<p>Seen.</p>"
)
ENCODINGS = {
    # B4. A page a browser may decode other than as UTF-8 shows nothing that a parent hid.
    "iso-2022-jp declared": b"<meta charset='iso-2022-jp'>" + ESCAPED,
    "escape byte, no charset": ESCAPED,
    "shift_jis in content-type": (
        b"<meta http-equiv='Content-Type' content='text/html; charset=shift_jis'>"
        b"<div style='visibility:hidden'><p style='visibility:visible'>INJECT</p></div><p>Seen.</p>"
    ),
    "utf-16 mark": b"\xff\xfe"
    + b"<div style='visibility:hidden'><p style='visibility:visible'>INJECT</p></div><p>Seen.</p>",
}


@pytest.mark.parametrize("content", ENCODINGS.values(), ids=ENCODINGS.keys())
def test_a_page_in_another_encoding_shows_nothing_a_parent_hid(content: bytes) -> None:
    assert "INJECT" not in visible_text(content, "text/html")


def test_a_frame_or_text_box_is_never_a_cloak() -> None:
    """A browser never shows these, so one holding every word is not lifted."""
    for tag in ("iframe", "noembed", "canvas", "textarea"):
        assert visible_text(_page(f"<{tag}>Hidden words.</{tag}>"), "text/html") == "", tag
    assert visible_text(_page("<dialog><p>Hidden words.</p></dialog>"), "text/html") == ""


# Fourth Opus review of PR #91: the guarantee is a second, strict reading (ADR 0044, decision 6).
_V = "<p>Visible words here.</p>"
ROUND_FOUR = {
    "later inherit": (
        _V + "<div style='visibility:hidden'>"
        "<span style='visibility:visible;visibility:inherit'>INJECT</span></div>"
    ),
    "camelCase svg selector": (
        _V + "<style>foreignObject{display:none}</style><svg><foreignObject>INJECT</foreignObject></svg>"
    ),
    "titled alternate sheet": (
        _V + "<style title='a'>p{color:black}</style><style title='b'>span{visibility:visible}</style>"
        "<div style='visibility:hidden'><span>INJECT</span></div>"
    ),
    "csp blocks inline styles": (
        _V + "<meta http-equiv='Content-Security-Policy' content=\"style-src-attr 'none'\">"
        "<style>.p{visibility:hidden}</style>"
        "<div class='p'><span style='visibility:visible'>INJECT</span></div>"
    ),
}


@pytest.mark.parametrize("html", ROUND_FOUR.values(), ids=ROUND_FOUR.keys())
def test_text_the_strict_reader_hides_is_never_read_unflagged(html: str) -> None:
    """Either the reading hides INJECT as main does, or it differs from main's and every claim is held."""
    content = _page(html)
    reading = read_page(content, "text/html")
    assert "INJECT" not in strict_visible_text(content, "text/html")
    assert "INJECT" not in reading.visible or reading.differs


def test_the_cheap_round_four_fixes_hide_the_text_outright() -> None:
    """B1: a later inherit undoes a show; B2: hide rules match SVG tag names whatever their case."""
    for name in ("later inherit", "camelCase svg selector"):
        reading = read_page(_page(ROUND_FOUR[name]), "text/html")
        assert "INJECT" not in reading.visible, name


def test_a_parent_hide_undone_by_a_child_differs_from_the_strict_reading() -> None:
    content = _page(
        "<p>Visible words.</p><div style='visibility:hidden'><p style='visibility:visible'>Shown.</p></div>"
    )
    assert read_page(content, "text/html") == PageReading("Visible words.\nShown.", True, None)
    assert strict_visible_text(content, "text/html") == "Visible words."


RECORDED = [*GOLD, *sorted(p for p in FIXTURES.rglob("*") if p.suffix in (".htm", ".html"))]


@pytest.mark.parametrize("path", RECORDED, ids=[p.name[:24] for p in RECORDED])
def test_a_recorded_page_reads_the_same_both_ways(path: Path) -> None:
    """No recorded page, and so no gold-set release, is held for differing from the strict reading."""
    content = path.read_bytes()
    reading = read_page(content, "text/html")
    assert reading.visible == strict_visible_text(content, "text/html")
    assert not reading.differs


MAIN_TEXT_BLOB = (
    "8085e0f8747d560c37ba62b2a1ceebbebb787d9c"  # pipeline/catalyst/extract/text.py at main 6bbc06b
)
STRICT_OUTPUTS = FIXTURES / "strict-visible" / "main-outputs.json"


def _strict_cases() -> dict[str, bytes]:
    """Every page and input this file reads: the 80 recorded pages, cloaked copies, and each case."""
    cases: dict[str, bytes] = {}
    for path in RECORDED:
        raw = path.read_bytes()
        cases[f"page:{path.relative_to(REPO_ROOT)}"] = raw
        if path in GOLD or path in HOSTILE:
            for name, cloak in CLOAKS.items():
                cases[f"cloak:{name}:{path.name}"] = cloak(raw.decode("utf-8")).encode()
    groups = {
        "seen": SEEN,
        "hidden": STILL_HIDDEN,
        "review": REVIEW_LEAKS,
        "two": ROUND_TWO_LEAKS,
        "three": ROUND_THREE_LEAKS,
        "four": ROUND_FOUR,
    }
    for group, inputs in groups.items():
        for name, html in inputs.items():
            cases[f"{group}:{name}"] = _page(html + "<p>Seen.</p>")
    for name, content in ENCODINGS.items():
        cases[f"encoding:{name}"] = content
    return cases


def test_the_strict_reader_is_mains_reader() -> None:
    """The strict reading is main's `visible_text`, byte for byte, and gives main's output on every case."""
    source = (REPO_ROOT / "pipeline" / "catalyst" / "extract" / "strict_text.py").read_bytes()
    assert hashlib.sha1(b"blob %d\0" % len(source) + source).hexdigest() == MAIN_TEXT_BLOB  # noqa: S324
    expected: dict[str, str] = json.loads(STRICT_OUTPUTS.read_text())
    found = {
        name: hashlib.sha256(strict_visible_text(content, "text/html").encode()).hexdigest()
        for name, content in _strict_cases().items()
    }
    assert found == expected


# Fifth Opus review of PR #91: a page the loose reader cannot read, or not in time, gets main's reading.
BROKEN = b"<table>\x00<math><html>"  # html5lib 1.1 raises AssertionError at the end of this input


def _fallbacks(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        r.__dict__["catalyst_fields"] for r in caplog.records if r.getMessage() == "loose_reader_fell_back"
    ]


@pytest.mark.parametrize(
    "content",
    [BROKEN, GOLD[0].read_bytes().replace(b"</TEXT>", BROKEN + b"</TEXT>")],
    ids=["alone", "in a release"],
)
def test_a_page_the_parser_cannot_read_gets_mains_reading(
    content: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    assert visible_text(content, "text/html") == strict_visible_text(content, "text/html")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="catalyst"):
        reading = read_page(content, "text/html")
    assert reading == PageReading(strict_visible_text(content, "text/html"), False, None)
    assert _fallbacks(caplog) == [{"reason": "AssertionError"}]


def test_a_deeply_nested_page_falls_back_fast(caplog: pytest.LogCaptureFixture) -> None:
    """html5lib takes minutes on 50,000 nested elements; the depth is counted before it is asked."""
    content = b"<html><body><p>Seen.</p>" + b"<div>" * 50_000 + b"deep</body></html>"
    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger="catalyst"):
        reading = read_page(content, "text/html")
    assert time.monotonic() - started < 5
    assert reading == PageReading(strict_visible_text(content, "text/html"), False, None)
    assert _fallbacks(caplog) == [{"reason": "TooDeep"}]


def test_a_reading_over_its_time_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("catalyst.extract.text.LOOSE_SECONDS", 0.0)
    content = _page(
        "<div style='visibility:hidden'><p style='visibility:visible'>Shown.</p></div><p>Seen.</p>"
    )
    with caplog.at_level(logging.INFO, logger="catalyst"):
        reading = read_page(content, "text/html")
    assert reading == PageReading("Seen.", False, None)
    assert _fallbacks(caplog) == [{"reason": "TimeoutError"}]


def test_what_hides_is_named_without_page_text() -> None:
    """The log names the tag and the kind of hide only, never attribute values."""
    cases = {
        "<p style='opacity:0'>Hidden words.</p>": "p: style",
        "<p hidden class='secret-name'>Hidden words.</p>": "p: hidden attribute",
        "<iframe>Hidden words.</iframe>": "iframe: never shown",
        "<style>.x{display:none}</style><div class='x'>Hidden words.</div>": "div: stylesheet rule",
    }
    for html, named in cases.items():
        assert hidden_by(_page(html + "<p>Seen.</p>"), "text/html") == named, html
