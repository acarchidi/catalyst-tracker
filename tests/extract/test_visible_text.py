"""`visible_text` hides only what a reader cannot see (ADR 0018 part 5c, ADR 0044).

The live run on 2026-10-20 skipped 56 releases as having no readable text: `visible_text` gave
nothing at all for them, from every filing agent. These tests take the recorded releases (the gold
set and the injection fixtures) and add the markup that blanks a whole page, or that a child
element overrides, and check that the reader's words come back while every planted hide stays.
"""

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from catalyst.extract.text import document_text, hidden_by, read_visible, visible_text
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
    assert hidden_by(_page(two), "text/html") == '<p class="x" style="opacity:0">'
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
