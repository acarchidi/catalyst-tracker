"""Stored copies become plain text, and quotes are found in it exactly (spec: Provenance)."""

from hypothesis import given
from hypothesis import strategies as st

from catalyst.extract.text import TEXT_VERSION, document_text, find_quote

PRESS_RELEASE = b"""<DOCUMENT><TYPE>EX-99.1<TEXT><HTML><HEAD><TITLE>pr</TITLE>
<style>p { color: red }</style><script>var x = 1;</script></HEAD><BODY>
<P>Acme&nbsp;Bio Announces <B>Phase&#160;3</B> Results</P>
<DIV>Topline data are expected in the<BR>second half of 2027.</DIV>
<TABLE><TR><TD>Cash</TD><TD>$1.2&nbsp;billion</TD></TR></TABLE>
<P>Caf&eacute; &amp; &#8220;quotes&#8221;</P></BODY></HTML></TEXT></DOCUMENT>"""


def test_html_becomes_one_line_per_block_without_markup() -> None:
    assert document_text(PRESS_RELEASE, "text/html") == (
        "Acme Bio Announces Phase 3 Results\n"
        "Topline data are expected in the\n"
        "second half of 2027.\n"
        "Cash $1.2 billion\n"
        "Café & “quotes”"
    )


def test_plain_text_keeps_its_words_and_tidies_spaces() -> None:
    assert document_text(b"  One   two \r\n\r\n three\t", "text/plain") == "One two\nthree"


def test_the_text_rules_have_a_version() -> None:
    # Quote offsets are counted in this text; changing the rules means a new version.
    assert TEXT_VERSION == "1"


def test_a_quote_is_found_where_it_is() -> None:
    text = document_text(PRESS_RELEASE, "text/html")
    span = find_quote(text, "Phase 3 Results")
    assert span is not None
    assert text[span.start : span.end] == "Phase 3 Results"
    assert span.quote == "Phase 3 Results"


def test_a_quote_across_a_line_break_keeps_the_source_words() -> None:
    text = document_text(PRESS_RELEASE, "text/html")
    span = find_quote(text, "expected in the second half of 2027.")
    assert span is not None
    assert span.quote == "expected in the\nsecond half of 2027."
    assert text[span.start : span.end] == span.quote


def test_a_quote_not_in_the_text_is_not_found() -> None:
    text = document_text(PRESS_RELEASE, "text/html")
    assert find_quote(text, "expected in the first half of 2027.") is None
    assert find_quote(text, "Phase 3 results") is None  # case matters: the quote must be verbatim
    assert find_quote(text, "   ") is None


words = st.lists(st.text(alphabet="abcdefgh", min_size=1, max_size=6), min_size=1, max_size=30)


@given(words=words, data=st.data())
def test_any_run_of_words_from_the_text_is_found(words: list[str], data: st.DataObject) -> None:
    text = " ".join(words)
    first = data.draw(st.integers(min_value=0, max_value=len(words) - 1))
    last = data.draw(st.integers(min_value=first, max_value=len(words) - 1))
    quote = " ".join(words[first : last + 1])
    span = find_quote(text, quote)
    assert span is not None
    assert text[span.start : span.end] == span.quote
    assert span.quote == quote


def test_escaped_markup_is_unescaped_once() -> None:
    assert document_text(b"<p>a &amp;lt; b</p>", "text/html") == "a &lt; b"
