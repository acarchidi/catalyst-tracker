"""Date phrases become windows (spec: Date precision). Written before the parser (AGENTS.md)."""

import calendar
from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from catalyst.extract.agent import has_words
from catalyst.extract.dates import (
    AHEAD_DAYS,
    DateWindow,
    cut_short,
    parse_window,
    unsettled_year,
    whole_date,
    writes_year,
)
from catalyst.schemas.enums import DatePrecision

PUBLISHED = date(2026, 10, 14)
QUARTER_ENDS = {1: date(2000, 3, 31), 2: date(2000, 6, 30), 3: date(2000, 9, 30), 4: date(2000, 12, 31)}


def _window(start: date, end: date, precision: DatePrecision) -> tuple[date, date, DatePrecision]:
    return start, end, precision


def _parsed(text: str, published: date = PUBLISHED) -> tuple[date, date, DatePrecision]:
    window = parse_window(text, published)
    assert window is not None, text
    return window.start, window.end, window.precision


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("October 30, 2026", _window(date(2026, 10, 30), date(2026, 10, 30), DatePrecision.DAY)),
        ("Oct. 30, 2026", _window(date(2026, 10, 30), date(2026, 10, 30), DatePrecision.DAY)),
        ("April 2027", _window(date(2027, 4, 1), date(2027, 4, 30), DatePrecision.MONTH)),
        ("February 2028", _window(date(2028, 2, 1), date(2028, 2, 29), DatePrecision.MONTH)),
        ("in December of 2026", _window(date(2026, 12, 1), date(2026, 12, 31), DatePrecision.MONTH)),
        ("Q1 2027", _window(date(2027, 1, 1), date(2027, 3, 31), DatePrecision.QUARTER)),
        ("1Q27", _window(date(2027, 1, 1), date(2027, 3, 31), DatePrecision.QUARTER)),
        ("the first quarter of 2027", _window(date(2027, 1, 1), date(2027, 3, 31), DatePrecision.QUARTER)),
        ("fourth quarter 2026", _window(date(2026, 10, 1), date(2026, 12, 31), DatePrecision.QUARTER)),
        ("2H26", _window(date(2026, 7, 1), date(2026, 12, 31), DatePrecision.HALF)),
        ("H1 2027", _window(date(2027, 1, 1), date(2027, 6, 30), DatePrecision.HALF)),
        ("second half of 2027", _window(date(2027, 7, 1), date(2027, 12, 31), DatePrecision.HALF)),
        ("first half 2027", _window(date(2027, 1, 1), date(2027, 6, 30), DatePrecision.HALF)),
        ("early 2027", _window(date(2027, 1, 1), date(2027, 6, 30), DatePrecision.HALF)),
        ("mid-2027", _window(date(2027, 4, 1), date(2027, 9, 30), DatePrecision.HALF)),
        ("late 2027", _window(date(2027, 7, 1), date(2027, 12, 31), DatePrecision.HALF)),
        ("2027", _window(date(2027, 1, 1), date(2027, 12, 31), DatePrecision.YEAR)),
        ("during 2027", _window(date(2027, 1, 1), date(2027, 12, 31), DatePrecision.YEAR)),
        ("year-end 2026", _window(date(2026, 10, 1), date(2026, 12, 31), DatePrecision.QUARTER)),
        ("the end of 2027", _window(date(2027, 10, 1), date(2027, 12, 31), DatePrecision.QUARTER)),
    ],
)
def test_phrases_with_a_year(text: str, expected: tuple[date, date, DatePrecision]) -> None:
    assert _parsed(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # "By" means any day from the document's date up to the end of the period.
        ("by year-end", _window(PUBLISHED, date(2026, 12, 31), DatePrecision.YEAR)),
        ("by the end of the year", _window(PUBLISHED, date(2026, 12, 31), DatePrecision.YEAR)),
        ("by the end of 2027", _window(PUBLISHED, date(2027, 12, 31), DatePrecision.YEAR)),
        ("by year-end 2026", _window(PUBLISHED, date(2026, 12, 31), DatePrecision.YEAR)),
        (
            "by the end of the first quarter of 2027",
            _window(PUBLISHED, date(2027, 3, 31), DatePrecision.QUARTER),
        ),
        ("later this year", _window(PUBLISHED, date(2026, 12, 31), DatePrecision.YEAR)),
        ("this quarter", _window(date(2026, 10, 1), date(2026, 12, 31), DatePrecision.QUARTER)),
        ("the fourth quarter", _window(date(2026, 10, 1), date(2026, 12, 31), DatePrecision.QUARTER)),
        ("next year", _window(date(2027, 1, 1), date(2027, 12, 31), DatePrecision.YEAR)),
        ("the second half of next year", _window(date(2027, 7, 1), date(2027, 12, 31), DatePrecision.HALF)),
        ("the second half of the year", _window(date(2026, 7, 1), date(2026, 12, 31), DatePrecision.HALF)),
    ],
)
def test_phrases_that_lean_on_the_document_date(
    text: str, expected: tuple[date, date, DatePrecision]
) -> None:
    assert _parsed(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "soon",
        "in the coming months",
        "upon FDA approval",
        "Q5 2026",
        "13H26",
        "February 30, 2027",
        # A bare two-digit number is not a year (review of PR #14).
        "10",
        "in 26",
        "June 15",  # with no year, the next June 15 is more than half a year ahead: a past event? (ADR 0061)
        "by year-end 26",  # a two-digit year needs an apostrophe here too (review of PR #14)
    ],
)
def test_vague_or_impossible_phrases_give_no_window(text: str) -> None:
    assert parse_window(text, PUBLISHED) is None


@pytest.mark.parametrize(
    ("text", "day"),
    [("November 12", date(2026, 11, 12)), ("Oct. 14", date(2026, 10, 14)), ("December 1", date(2026, 12, 1))],
)
def test_a_day_without_a_year_is_that_day_in_the_document_year_if_still_ahead(text: str, day: date) -> None:
    assert _parsed(text) == (day, day, DatePrecision.DAY)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("April '27", _window(date(2027, 4, 1), date(2027, 4, 30), DatePrecision.MONTH)),
        ("'27", _window(date(2027, 1, 1), date(2027, 12, 31), DatePrecision.YEAR)),
        ("Q1 27", _window(date(2027, 1, 1), date(2027, 3, 31), DatePrecision.QUARTER)),
        ("H2 26", _window(date(2026, 7, 1), date(2026, 12, 31), DatePrecision.HALF)),
        ("by year-end '27", _window(PUBLISHED, date(2027, 12, 31), DatePrecision.YEAR)),
    ],
)
def test_two_digit_years_need_an_apostrophe_except_in_compact_forms(
    text: str, expected: tuple[date, date, DatePrecision]
) -> None:
    assert _parsed(text) == expected


@given(
    month=st.integers(min_value=1, max_value=12),
    day=st.integers(min_value=1, max_value=28),
    published=st.dates(min_value=date(2000, 1, 1), max_value=date(2099, 12, 31)),
)
def test_a_month_and_day_is_never_read_as_a_month_and_year(month: int, day: int, published: date) -> None:
    window = parse_window(f"{date(2000, month, 1):%B} {day}", published)
    if window is not None:
        assert window.precision is DatePrecision.DAY
        # The first such day on or after the document's date (ADR 0061), never a month of year 20dd.
        assert window.start in (date(published.year, month, day), date(published.year + 1, month, day))
        assert window.start >= published


def test_a_by_deadline_already_past_gives_no_window() -> None:
    assert parse_window("by the end of the second quarter of 2026", PUBLISHED) is None


def test_by_year_end_on_new_years_eve_is_a_single_day() -> None:
    assert _parsed("by year-end", date(2026, 12, 31)) == _window(
        date(2026, 12, 31), date(2026, 12, 31), DatePrecision.DAY
    )


def test_the_raw_text_is_kept_exactly() -> None:
    window = parse_window("  the Second Half of 2027 ", PUBLISHED)
    assert window == DateWindow(
        date(2027, 7, 1), date(2027, 12, 31), DatePrecision.HALF, "  the Second Half of 2027 "
    )


years = st.integers(min_value=2000, max_value=2099)
published_days = st.dates(min_value=date(2000, 1, 1), max_value=date(2099, 12, 31))


@given(year=years, half=st.sampled_from([1, 2]))
def test_every_half_spelling_gives_the_same_window(year: int, half: int) -> None:
    short = year % 100
    ordinal = "first" if half == 1 else "second"
    spellings = [f"{half}H{short:02d}", f"{half}H {year}", f"H{half} {year}", f"{ordinal} half of {year}"]
    windows = {_parsed(text, date(year, 1, 1)) for text in spellings}
    start = date(year, 1 if half == 1 else 7, 1)
    end = date(year, 6, 30) if half == 1 else date(year, 12, 31)
    assert windows == {(start, end, DatePrecision.HALF)}


@given(year=years, quarter=st.integers(min_value=1, max_value=4))
def test_every_quarter_spelling_gives_the_same_window(year: int, quarter: int) -> None:
    ordinal = ["first", "second", "third", "fourth"][quarter - 1]
    spellings = [
        f"Q{quarter} {year}",
        f"{quarter}Q{year % 100:02d}",
        f"{ordinal} quarter of {year}",
        f"the {ordinal.upper()} QUARTER {year}",
    ]
    windows = {_parsed(text, date(year, 1, 1)) for text in spellings}
    end = QUARTER_ENDS[quarter].replace(year=year)
    assert windows == {(date(year, 3 * quarter - 2, 1), end, DatePrecision.QUARTER)}


@given(text=st.text(max_size=60), published=published_days)
def test_any_text_gives_a_valid_window_or_none(text: str, published: date) -> None:
    window = parse_window(text, published)
    if window is not None:
        assert window.start <= window.end
        assert (window.precision is DatePrecision.DAY) == (window.start == window.end)
        assert window.raw_text == text


@given(published=published_days, quarter=st.integers(min_value=1, max_value=4), year=years)
def test_a_by_window_starts_on_the_document_date(published: date, quarter: int, year: int) -> None:
    window = parse_window(f"by the end of Q{quarter} {year}", published)
    end = QUARTER_ENDS[quarter].replace(year=year)
    if end < published:
        assert window is None
    else:
        assert window is not None
        assert (window.start, window.end) == (published, end)


BY_PHRASES = (
    "by Q{q} {y}",
    "by the end of Q{q} {y}",
    "by end of {y}",
    "by year-end {y}",
    "by the end of the year",
    "by year-end",
    "by {y}",
    "by mid-{y}",
    "by the second half of {y}",
    "by {q}H{yy}",
    "by December {q}, {y}",
    "by the end of December {y}",
)


@given(
    published=published_days,
    phrase=st.sampled_from(BY_PHRASES),
    quarter=st.integers(min_value=1, max_value=4),
    year=years,
)
def test_every_by_phrase_starts_on_the_document_date(
    published: date, phrase: str, quarter: int, year: int
) -> None:
    """`writes_year` compares only a "by" phrase's end, because its start is always the release date.

    Pinned here so a change to the "by" rule cannot quietly make `writes_year` pass words whose
    start moves with the document (Claude review of PR #71).
    """
    words = phrase.format(q=quarter if "H" not in phrase else min(quarter, 2), y=year, yy=f"{year % 100:02d}")
    window = parse_window(words, published)
    if window is not None:
        assert window.start == published, words
        # A deadline on a day is still a window of many days, never day precision (ADR 0061).
        assert (window.precision is DatePrecision.DAY) == (window.start == window.end), words


@given(rest=st.text(max_size=30), published=published_days)
def test_any_by_phrase_the_parser_reads_starts_on_the_document_date(rest: str, published: date) -> None:
    window = parse_window("by " + rest, published)
    if window is not None:
        assert window.start == published


# Date ranges (ADR 0018, part 3): the window runs from the first part's start to the last part's
# end; its precision is the coarsest of the parts, and a range of days counts as month precision.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("November 16\u201319, 2026", _window(date(2026, 11, 16), date(2026, 11, 19), DatePrecision.MONTH)),
        ("November 16-19, 2026", _window(date(2026, 11, 16), date(2026, 11, 19), DatePrecision.MONTH)),
        (
            "November 30 \u2013 December 2, 2026",
            _window(date(2026, 11, 30), date(2026, 12, 2), DatePrecision.MONTH),
        ),
        ("late 2027 or early 2028", _window(date(2027, 7, 1), date(2028, 6, 30), DatePrecision.HALF)),
        (
            "year-end 2026/first quarter 2027",
            _window(date(2026, 10, 1), date(2027, 3, 31), DatePrecision.QUARTER),
        ),
        ("Q4 2026 to Q1 2027", _window(date(2026, 10, 1), date(2027, 3, 31), DatePrecision.QUARTER)),
        ("between Q3 and Q4 2027", _window(date(2027, 7, 1), date(2027, 12, 31), DatePrecision.QUARTER)),
        (
            "first or second quarter of 2027",
            _window(date(2027, 1, 1), date(2027, 6, 30), DatePrecision.QUARTER),
        ),
        (
            "third or fourth quarter 2026",
            _window(date(2026, 7, 1), date(2026, 12, 31), DatePrecision.QUARTER),
        ),
        ("2H26 or 1H27", _window(date(2026, 7, 1), date(2027, 6, 30), DatePrecision.HALF)),
        ("2027 or 2028", _window(date(2027, 1, 1), date(2028, 12, 31), DatePrecision.YEAR)),
        ("December 2026 or January 2027", _window(date(2026, 12, 1), date(2027, 1, 31), DatePrecision.MONTH)),
        ("Q4 2026 through mid-2027", _window(date(2026, 10, 1), date(2027, 9, 30), DatePrecision.HALF)),
    ],
)
def test_ranges_span_from_the_first_part_to_the_last(
    text: str, expected: tuple[date, date, DatePrecision]
) -> None:
    assert _parsed(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Q1 2028 or Q4 2027",  # backwards
        "November 19\u201316, 2026",  # backwards
        "November 31\u2013December 2, 2026",  # no such day
        "Q4 or soon",  # one part names no window
        "late 2027 or",  # a part is missing
        "mid-2027",  # the hyphen in mid-2027 is not a range
    ],
)
def test_ranges_that_are_backwards_or_incomplete_give_no_range(text: str) -> None:
    window = parse_window(text, PUBLISHED)
    if text == "mid-2027":
        assert window is not None
        assert (window.start, window.end) == (date(2027, 4, 1), date(2027, 9, 30))
    else:
        assert window is None


# Review of PR #40: when only the last part writes the year, the first part takes that year,
# never the document's.
EARLY_2026 = date(2026, 3, 1)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "first quarter to second quarter of 2028",
            _window(date(2028, 1, 1), date(2028, 6, 30), DatePrecision.QUARTER),
        ),
        ("first half to second half 2028", _window(date(2028, 1, 1), date(2028, 12, 31), DatePrecision.HALF)),
        (
            "November 16 to December 2, 2027",
            _window(date(2027, 11, 16), date(2027, 12, 2), DatePrecision.MONTH),
        ),
        ("Q3 to Q4 2027", _window(date(2027, 7, 1), date(2027, 12, 31), DatePrecision.QUARTER)),
        # A year written in words at the end applies too (second review of PR #40).
        (
            "first quarter to second quarter of next year",
            _window(date(2027, 1, 1), date(2027, 6, 30), DatePrecision.QUARTER),
        ),
        (
            "first half or second half next year",
            _window(date(2027, 1, 1), date(2027, 12, 31), DatePrecision.HALF),
        ),
        # "this quarter" names its own year, so it keeps it.
        ("this quarter or Q1 2027", _window(date(2026, 1, 1), date(2027, 3, 31), DatePrecision.QUARTER)),
    ],
)
def test_a_year_written_only_at_the_end_applies_to_the_first_part(
    text: str, expected: tuple[date, date, DatePrecision]
) -> None:
    assert _parsed(text, EARLY_2026) == expected


# A year written only at the start applies to the last part too (Claude review of PR #40).
EARLY_2027 = date(2027, 2, 1)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Q3 2026 to year-end", _window(date(2026, 7, 1), date(2026, 12, 31), DatePrecision.QUARTER)),
        (
            "first quarter of 2028 to second quarter",
            _window(date(2028, 1, 1), date(2028, 6, 30), DatePrecision.QUARTER),
        ),
        ("November 2027 to December", _window(date(2027, 11, 1), date(2027, 12, 31), DatePrecision.MONTH)),
    ],
)
def test_a_year_written_only_at_the_start_applies_to_the_last_part(
    text: str, expected: tuple[date, date, DatePrecision]
) -> None:
    assert _parsed(text, EARLY_2027) == expected


def test_a_last_part_that_runs_backwards_in_the_written_year_gives_no_window() -> None:
    """Q4 2026 to "first quarter" read as Q1 2026 runs backwards; code does not guess 2027.

    Before this rule the last part took the document's year (2027) and gave Oct 2026 to Mar 2027.
    """
    assert parse_window("Q4 2026 to first quarter", EARLY_2027) is None


@pytest.mark.parametrize(
    "text",
    [
        "year-end or first quarter 2028",  # year-end of 2027 or 2028? Code does not guess.
        "year-end or first quarter next year",  # the same rule with the year in words
        "Q4 2026 to Q1",  # "Q1" alone has no year in any form, so this never had a window
        "2027-2028",  # an unspaced ASCII hyphen joins days ("November 16-19") and quarters or halves
    ],
)
def test_ranges_code_cannot_read_safely_give_no_window(text: str) -> None:
    assert parse_window(text, EARLY_2026) is None


@given(
    first=st.tuples(years, st.integers(min_value=1, max_value=4)),
    second=st.tuples(years, st.integers(min_value=1, max_value=4)),
    joiner=st.sampled_from([" or ", " to ", " through ", "/", "\u2013", " \u2013 "]),
)
def test_a_quarter_range_covers_both_quarters_in_order(
    first: tuple[int, int], second: tuple[int, int], joiner: str
) -> None:
    window = parse_window(f"Q{first[1]} {first[0]}{joiner}Q{second[1]} {second[0]}", date(2000, 1, 1))
    if first > second:
        assert window is None
        return
    assert window is not None
    assert window.start == date(first[0], 3 * first[1] - 2, 1)
    assert window.end == QUARTER_ENDS[second[1]].replace(year=second[0])
    assert window.precision is DatePrecision.QUARTER


def _digits(text: str, zero: int) -> str:
    """Write the ASCII digits of `text` with the digits of the script whose zero is `zero`."""
    return "".join(chr(zero + int(c)) if c.isdigit() else c for c in text)


@pytest.mark.parametrize(
    "phrase",
    [
        "August 2" + _digits("4", 0x09E6) + ", 2027",
        "Q" + _digits("4", 0x0660) + " 2026",
        _digits("2027", 0xFF10),
        "second half of " + _digits("2027", 0x0966),
    ],
    ids=["Bengali four", "Arabic-Indic four", "fullwidth year", "Devanagari year"],
)
def test_digits_from_other_scripts_name_no_window(phrase: str) -> None:
    """A reader may see "28" where code reads another digit; only ASCII digits count (CP4, B3)."""
    assert parse_window(phrase, date(2026, 10, 14)) is None


@pytest.mark.parametrize(
    "phrase",
    [
        "second half of 2027",
        "2H26",
        "Q4 '27",
        "Q1 2027",
        "December 15, 2027",
        "November 16 to December 2, 2027",
        "November 2027 to December",
        "late 2026 or early 2027",
        "by the end of 2027",
        "by Q4 2027",
        "year-end 2026/first quarter 2027",
    ],
)
def test_words_that_write_their_year_in_digits(phrase: str) -> None:
    """Their window is the same whenever the document came out (owner decision, 2026-10-19)."""
    assert writes_year(phrase, PUBLISHED) is True


@pytest.mark.parametrize(
    "phrase",
    [
        "second half",
        "December 15",
        "by year-end",
        "third quarter",
        "November 16 to December 2",
        "this year",
        "next year",
        "the next year",
        "second half of next year",
        "this quarter",
        "later this year",
        "early next year",
        "first half or next year",
        "late 2026 or early next year",
        "between Q4 2026 and next year",
        "FY2027",
        "soon",
    ],
)
def test_words_that_do_not_write_their_year(phrase: str) -> None:
    """Missing, counted from the document, or not read as a year at all: a person checks it."""
    assert writes_year(phrase, PUBLISHED) is False


def test_a_february_29_without_a_year_does_not_write_its_year() -> None:
    """Read a year later the day does not exist; that must never count as a written year (round 4)."""
    released = date(2028, 1, 10)
    assert parse_window("February 29", released) is not None
    assert writes_year("February 29", released) is False
    assert writes_year("February 29, 2028", released) is True
    assert writes_year("December 15", date(2028, 2, 29)) is False
    assert writes_year("December 15, 2028", date(2028, 2, 29)) is True


FISCAL = "are part of a fiscal-year date, not a calendar one"


@pytest.mark.parametrize(
    ("quote", "words"),
    [
        ("Data are expected in fiscal 2027.", "2027"),
        ("Data are expected in FY2027.", "FY2027"),
        ("Data are expected in FY 2027.", "2027"),
        ("Data are expected in FY-2027.", "2027"),
        ("Data are expected in F.Y. 2027.", "2027"),
        ("Data are expected in F.Y.2027.", "F.Y.2027"),
        ("Data are expected in FY Q4 2027.", "Q4 2027"),
        ("Data are expected in FY-Q4 2027.", "Q4 2027"),
        ("Data are expected in fy q4 2027.", "q4 2027"),
        ("Data are expected in FY 4Q27.", "4Q27"),
        ("Data are expected in FY '27.", "'27"),
    ],
)
def test_every_spelling_of_a_fiscal_year_holds_the_date_words(quote: str, words: str) -> None:
    """A fiscal year need not be the calendar year, so its dates are held (CP4 review, O2)."""
    assert cut_short(quote, words, PUBLISHED) == FISCAL


@pytest.mark.parametrize(
    ("quote", "words"),
    [
        ("Data for Fyarro are expected in Q4 2027.", "Q4 2027"),
        ("FYI: data are expected in Q4 2027.", "Q4 2027"),
        ("Data are expected in Q4 2027.", "Q4 2027"),
    ],
)
def test_words_that_only_start_like_fy_are_not_a_fiscal_year(quote: str, words: str) -> None:
    assert cut_short(quote, words, PUBLISHED) is None


# --- ADR 0061 (owner, 2026-11-23): more date words read as windows, with no person in the loop ---

MONTH_NAMES = [name for name in calendar.month_name if name]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Ordinal days, a weekday that matches, and a time of day after a day.
        ("October 9th, 2027", _window(date(2027, 10, 9), date(2027, 10, 9), DatePrecision.DAY)),
        ("November 19th-20th, 2026", _window(date(2026, 11, 19), date(2026, 11, 20), DatePrecision.MONTH)),
        ("Saturday, December 12, 2026", _window(date(2026, 12, 12), date(2026, 12, 12), DatePrecision.DAY)),
        ("on or about September 11, 2027", _window(date(2027, 9, 11), date(2027, 9, 11), DatePrecision.DAY)),
        (
            "Friday, October 23rd at 8:15 AM CEST",
            _window(date(2026, 10, 23), date(2026, 10, 23), DatePrecision.DAY),
        ),
        (
            "Friday, October 23rd 10:25 \u2013 10:35 CEST",
            _window(date(2026, 10, 23), date(2026, 10, 23), DatePrecision.DAY),
        ),
        # Part of a quarter: early is its first two months, late its last two, mid and end one month.
        ("early Q1 2027", _window(date(2027, 1, 1), date(2027, 2, 28), DatePrecision.QUARTER)),
        ("Early Q2 2027", _window(date(2027, 4, 1), date(2027, 5, 31), DatePrecision.QUARTER)),
        ("late Q3 2027", _window(date(2027, 8, 1), date(2027, 9, 30), DatePrecision.QUARTER)),
        ("mid-Q2 2027", _window(date(2027, 5, 1), date(2027, 5, 31), DatePrecision.MONTH)),
        (
            "early in the first quarter of 2027",
            _window(date(2027, 1, 1), date(2027, 2, 28), DatePrecision.QUARTER),
        ),
        (
            "at the end of the third quarter of 2027",
            _window(date(2027, 9, 1), date(2027, 9, 30), DatePrecision.MONTH),
        ),
        ("around the end of Q3 2027", _window(date(2027, 9, 1), date(2027, 9, 30), DatePrecision.MONTH)),
        ("late Q3 or early Q4 2027", _window(date(2027, 8, 1), date(2027, 11, 30), DatePrecision.QUARTER)),
        # Part of a month: early is days 1 to 15, late and "end" 16 to the last, mid 8 to 22.
        ("early December 2026", _window(date(2026, 12, 1), date(2026, 12, 15), DatePrecision.MONTH)),
        ("mid-December 2026", _window(date(2026, 12, 8), date(2026, 12, 22), DatePrecision.MONTH)),
        ("End Nov. 2026", _window(date(2026, 11, 16), date(2026, 11, 30), DatePrecision.MONTH)),
        (
            "late August or early September 2027",
            _window(date(2027, 8, 16), date(2027, 9, 15), DatePrecision.MONTH),
        ),
        # The word "calendar" changes nothing; a fiscal year is still held by `cut_short`.
        (
            "first quarter of calendar year 2027",
            _window(date(2027, 1, 1), date(2027, 3, 31), DatePrecision.QUARTER),
        ),
        ("calendar H1 2027", _window(date(2027, 1, 1), date(2027, 6, 30), DatePrecision.HALF)),
        ("calendar H2 2027", _window(date(2027, 7, 1), date(2027, 12, 31), DatePrecision.HALF)),
        # Typographic marks: a hyphen before the year, a curly apostrophe, a non-breaking hyphen.
        ("Q4-2026", _window(date(2026, 10, 1), date(2026, 12, 31), DatePrecision.QUARTER)),
        ("Q3 \u201828", _window(date(2028, 7, 1), date(2028, 9, 30), DatePrecision.QUARTER)),
        ("Q4 \u201926", _window(date(2026, 10, 1), date(2026, 12, 31), DatePrecision.QUARTER)),
        ("mid\u20112027", _window(date(2027, 4, 1), date(2027, 9, 30), DatePrecision.HALF)),
        ("mid-year 2027", _window(date(2027, 4, 1), date(2027, 9, 30), DatePrecision.HALF)),
        # Ranges: an unspaced hyphen between a year and a quarter; "near"; a trailing "timeframe".
        ("4Q 2026-Q1 2027", _window(date(2026, 10, 1), date(2027, 3, 31), DatePrecision.QUARTER)),
        ("Q4 2026-Q1 2027", _window(date(2026, 10, 1), date(2027, 3, 31), DatePrecision.QUARTER)),
        (
            "near the end of 2026 or early 2027",
            _window(date(2026, 10, 1), date(2027, 6, 30), DatePrecision.HALF),
        ),
        ("near year end 2026", _window(date(2026, 10, 1), date(2026, 12, 31), DatePrecision.QUARTER)),
        (
            "December 2026 to February 2027 timeframe",
            _window(date(2026, 12, 1), date(2027, 2, 28), DatePrecision.MONTH),
        ),
        ("throughout 2027", _window(date(2027, 1, 1), date(2027, 12, 31), DatePrecision.YEAR)),
        # Seasons, four months each so both ways of counting them fit: fall is September to December.
        ("the Fall of 2026", _window(date(2026, 9, 1), date(2026, 12, 31), DatePrecision.HALF)),
        ("Fall 2026", _window(date(2026, 9, 1), date(2026, 12, 31), DatePrecision.HALF)),
        ("spring 2027", _window(date(2027, 3, 1), date(2027, 6, 30), DatePrecision.HALF)),
        ("late summer 2027", _window(date(2027, 8, 1), date(2027, 9, 30), DatePrecision.QUARTER)),
        # "Before the end of" is "by the end of".
        ("before the end of 2026", _window(PUBLISHED, date(2026, 12, 31), DatePrecision.YEAR)),
        ("before the end of this year", _window(PUBLISHED, date(2026, 12, 31), DatePrecision.YEAR)),
        # A deadline on a day runs over many days, so its precision is month, as for a range of days.
        ("by December 15, 2027", _window(PUBLISHED, date(2027, 12, 15), DatePrecision.MONTH)),
    ],
)
def test_more_phrases_that_state_a_calendar_window(
    text: str, expected: tuple[date, date, DatePrecision]
) -> None:
    assert _parsed(text) == expected


@pytest.mark.parametrize(
    ("text", "published", "expected"),
    [
        ("in December", PUBLISHED, _window(date(2026, 12, 1), date(2026, 12, 31), DatePrecision.MONTH)),
        ("November", PUBLISHED, _window(date(2026, 11, 1), date(2026, 11, 30), DatePrecision.MONTH)),
        # The month the document came out in has not ended, so it is that one.
        ("in October", PUBLISHED, _window(date(2026, 10, 1), date(2026, 10, 31), DatePrecision.MONTH)),
        ("January", PUBLISHED, _window(date(2027, 1, 1), date(2027, 1, 31), DatePrecision.MONTH)),
        ("March 26", PUBLISHED, _window(date(2027, 3, 26), date(2027, 3, 26), DatePrecision.DAY)),
        ("Friday, October 23", PUBLISHED, _window(date(2026, 10, 23), date(2026, 10, 23), DatePrecision.DAY)),
        ("Monday, October 26", PUBLISHED, _window(date(2026, 10, 26), date(2026, 10, 26), DatePrecision.DAY)),
        (
            "August 28-31",
            date(2026, 8, 10),
            _window(date(2026, 8, 28), date(2026, 8, 31), DatePrecision.MONTH),
        ),
        (
            "August 7th-9th",
            date(2026, 8, 6),
            _window(date(2026, 8, 7), date(2026, 8, 9), DatePrecision.MONTH),
        ),
        (
            "October 23\u201327",
            date(2026, 7, 28),
            _window(date(2026, 10, 23), date(2026, 10, 27), DatePrecision.MONTH),
        ),
        (
            "early September",
            date(2026, 8, 11),
            _window(date(2026, 9, 1), date(2026, 9, 15), DatePrecision.MONTH),
        ),
        (
            "by early January",
            date(2026, 8, 6),
            _window(date(2026, 8, 6), date(2027, 1, 15), DatePrecision.MONTH),
        ),
        ("this fall", date(2026, 8, 10), _window(date(2026, 9, 1), date(2026, 12, 31), DatePrecision.HALF)),
    ],
)
def test_a_month_or_day_without_a_year_is_the_first_one_on_or_after_the_document_date(
    text: str, published: date, expected: tuple[date, date, DatePrecision]
) -> None:
    assert _parsed(text, published) == expected


@pytest.mark.parametrize(
    "text",
    [
        "September",  # the next one is eleven months ahead: a past event, or a year written elsewhere
        "October 9",  # five days before the document's date
        "Thursday, October 23",  # October 23, 2026 is a Friday
        "Tuesday, December 12, 2026",  # a written year does not excuse a wrong weekday
        "Friday, October 2026",  # a weekday needs a day
        "October 2026 at 8:15 AM",  # so does a time of day
        "winter 2026",  # which end of the year?
        "early H1 2027",
        "before December 2026",  # ends before the month, not with it
        "ESMO 2026",  # a meeting, not a date (the conference list will give its days)
        "2026 ESMO Congress",
        "with our third quarter 2026 results",
        "third quarter earnings update",
        "mid- and late 2026, respectively",  # two events, two dates
        "Q3 and Q4 2026, respectively",
        "within 30 days",
        "potentially in 2026",
        "as early as this year",
        "Oct. 19-21, Boston",
    ],
)
def test_words_that_still_name_no_window(text: str) -> None:
    assert parse_window(text, PUBLISHED) is None


def _first_on_or_after(month: int, day: int, published: date) -> date:
    this_year = date(published.year, month, day)
    return this_year if this_year >= published else date(published.year + 1, month, day)


@given(
    month=st.integers(min_value=1, max_value=12),
    day=st.integers(min_value=1, max_value=28),
    published=published_days,
)
def test_a_day_without_a_year_is_the_next_such_day_or_nothing(month: int, day: int, published: date) -> None:
    expected = _first_on_or_after(month, day, published)
    window = parse_window(f"{MONTH_NAMES[month - 1]} {day}", published)
    if (expected - published).days > AHEAD_DAYS:
        assert window is None
    else:
        assert window is not None
        assert (window.start, window.end, window.precision) == (expected, expected, DatePrecision.DAY)


@given(month=st.integers(min_value=1, max_value=12), published=published_days)
def test_a_month_without_a_year_is_the_next_one_not_yet_over_or_nothing(month: int, published: date) -> None:
    window = parse_window(MONTH_NAMES[month - 1], published)
    year = published.year if month >= published.month else published.year + 1
    start = date(year, month, 1)
    if (start - published).days > AHEAD_DAYS:
        assert window is None
    else:
        assert window is not None
        assert (window.start, window.precision) == (start, DatePrecision.MONTH)
        assert window.end == date(year, month, calendar.monthrange(year, month)[1]) >= published


@given(
    day=st.dates(min_value=date(2001, 1, 1), max_value=date(2098, 12, 31)),
    before=st.integers(min_value=0, max_value=AHEAD_DAYS),
    shift=st.integers(min_value=1, max_value=6),
    year_written=st.booleans(),
    short=st.booleans(),
)
def test_a_written_weekday_must_be_the_weekday_of_the_day(
    day: date, before: int, shift: int, year_written: bool, short: bool
) -> None:
    """The right weekday gives the day; any other gives no window, with or without the year."""
    names = calendar.day_abbr if short else calendar.day_name
    rest = f"{day:%B} {day.day}" + (f", {day.year}" if year_written else "")
    published = day - timedelta(days=before)
    right = parse_window(f"{names[day.weekday()]}, {rest}", published)
    assert right is not None
    assert (right.start, right.end, right.precision) == (day, day, DatePrecision.DAY)
    assert parse_window(f"{names[(day.weekday() + shift) % 7]}, {rest}", published) is None


@given(
    year=years,
    month=st.integers(min_value=1, max_value=12),
    day=st.integers(min_value=1, max_value=28),
    suffix=st.sampled_from(["st", "nd", "rd", "th"]),
)
def test_an_ordinal_day_is_the_same_day(year: int, month: int, day: int, suffix: str) -> None:
    plain = f"{MONTH_NAMES[month - 1]} {day}, {year}"
    assert _parsed(f"{MONTH_NAMES[month - 1]} {day}{suffix}, {year}") == _parsed(plain)


@given(
    year=years,
    period=st.sampled_from(
        ["{y}", "Q1 {y}", "Q2 {y}", "Q3 {y}", "Q4 {y}", "summer {y}", "fall {y}", "March {y}", "February {y}"]
    ),
    part=st.sampled_from(["early", "mid", "late"]),
)
def test_a_part_of_a_period_lies_inside_it(year: int, period: str, part: str) -> None:
    """Early starts where the period starts, late ends where it ends, and mid touches neither end."""
    whole = _parsed(period.format(y=year))
    words = f"{part}-{year}" if period == "{y}" else f"{part} {period.format(y=year)}"
    start, end, _ = _parsed(words)
    assert whole[0] <= start <= end <= whole[1]
    assert (start == whole[0]) is (part == "early")
    assert (end == whole[1]) is (part == "late")


# Date words cut from a longer date: code takes the whole date from the quote (ADR 0061, part 3).
@pytest.mark.parametrize(
    ("quote", "words", "whole"),
    [
        ("Topline data are expected by mid-2027.", "mid-2027", "by mid-2027"),
        ("We expect to file by the end of 2026.", "the end of 2026", "by the end of 2026"),
        ("We expect to file by end of 2026.", "end of 2026", "by end of 2026"),
        ("Data are expected by 1H 2027.", "1H 2027", "by 1H 2027"),
        ("Data are expected in early 2027.", "2027", "early 2027"),
        (
            "Data are expected in the second half of calendar 2027.",
            "second half",
            "second half of calendar 2027",
        ),
        ("Data are expected in late 2026 or early 2027.", "late 2026", "late 2026 or early 2027"),
        ("Data are expected in Q4 2026-Q1 2027.", "Q4 2026", "Q4 2026-Q1 2027"),
        ("Data are expected in Q4 2026-Q1 2027.", "Q1 2027", "Q4 2026-Q1 2027"),
        ("Meetings will be held December 1-15, 2026.", "December 1", "December 1-15, 2026"),
        (
            "Results will be shown at a meeting held November 16 to December 2, 2027.",
            "November 16",
            "November 16 to December 2, 2027",
        ),
        ("Data are expected in early December 2026.", "December 2026", "early December 2026"),
    ],
)
def test_words_cut_from_a_longer_date_are_read_whole(quote: str, words: str, whole: str) -> None:
    assert whole_date(quote, words, PUBLISHED) == whole
    assert whole in quote
    assert cut_short(quote, whole, PUBLISHED) is None


@pytest.mark.parametrize(
    ("quote", "words"),
    [
        # Already whole: nothing longer around the words reads as another window.
        ("Topline data are expected in Q4 2026.", "Q4 2026"),
        ("Data for SPY001 in Q4 2026 and for SPY072 in Q1 2027.", "Q4 2026"),
        # The words appear twice, and code cannot tell which place was meant.
        ("Data in the second half of 2027, after enrollment closes in the second half.", "second half"),
        # The longer date does not write its year, so it settles nothing.
        ("Data are expected in the second half of next year.", "second half"),
        ("Data are expected by year-end.", "year-end"),
        # "From ... to" may be a change of date rather than a range.
        ("Guidance moved from Q4 2026 to 2027.", "2027"),
        # A fiscal year is not read at all.
        ("Data are expected in the fourth quarter of fiscal 2027.", "fourth quarter"),
        # A year code cannot read.
        ("Results at a meeting on December 15, 2O27.", "December 15"),
    ],
)
def test_words_code_cannot_safely_widen_are_left_as_they_are(quote: str, words: str) -> None:
    assert whole_date(quote, words, PUBLISHED) == words


WHOLE_PHRASES = (
    ("by the end of Q{q} {y}", "Q{q} {y}"),
    ("by Q{q} {y}", "Q{q} {y}"),
    ("early Q{q} {y}", "Q{q} {y}"),
    ("late Q{q} {y}", "Q{q} {y}"),
    ("Q{q} {y} or Q{q} {z}", "Q{q} {y}"),
    ("the end of Q{q} {y}", "Q{q} {y}"),
)


@given(
    phrase=st.sampled_from(WHOLE_PHRASES),
    quarter=st.integers(min_value=1, max_value=4),
    year=st.integers(min_value=2027, max_value=2090),
    lead=st.sampled_from(["Topline data are expected", "The company plans to report results", "Data:"]),
)
def test_the_whole_date_is_the_quotes_own_words_and_gives_the_whole_window(
    phrase: tuple[str, str], quarter: int, year: int, lead: str
) -> None:
    whole, cut = (part.format(q=quarter, y=year, z=year + 1) for part in phrase)
    quote = f"{lead} {whole}, the company said."
    found = whole_date(quote, cut, PUBLISHED)
    assert found in quote
    assert _parsed(found) == _parsed(whole)
    assert _parsed(found) != _parsed(cut)
    assert cut_short(quote, found, PUBLISHED) is None


@given(words=st.text(max_size=20), quote=st.text(max_size=60), published=published_days)
def test_the_whole_date_is_always_words_of_the_quote_or_the_words_given(
    words: str, quote: str, published: date
) -> None:
    found = whole_date(quote, words, published)
    assert found == words or (has_words(quote, found) and parse_window(found, published) is not None)


def test_the_whole_date_keeps_one_space_where_the_quote_breaks_a_line() -> None:
    quote = "Topline data are expected in late 2026 or early\n2027."
    assert whole_date(quote, "early 2027", PUBLISHED) == "late 2026 or early 2027"


# Date words that do not write their year: code settles it from the document's date, or says why
# it cannot (ADR 0061, part 2).
@pytest.mark.parametrize(
    ("quote", "words", "published"),
    [
        ("Acme Bio expects to finish its filing in November.", "November", PUBLISHED),
        (
            "Acme Bio will show RAINIER results at its 2026 investor day in October.",
            "in October",
            date(2026, 8, 6),
        ),
        (
            "Acme Bio will host a RAINIER webcast on Friday, October 23 at noon.",
            "Friday, October 23",
            PUBLISHED,
        ),
        ("Acme Bio will host a RAINIER webcast on Friday, October 23 at noon.", "October 23", PUBLISHED),
        ("Results at a medical meeting on December 15 in Boston.", "December 15", PUBLISHED),
        ("On December 15, the company will hold an investor day.", "December 15", PUBLISHED),
        ("Acme Bio expects to finish enrollment by early January.", "by early January", date(2026, 11, 5)),
        ("Topline data are expected this year.", "this year", PUBLISHED),
        ("We plan to submit the NDA later this year.", "later this year", PUBLISHED),
        ("We expect to file in the fourth quarter of this year.", "fourth quarter of this year", PUBLISHED),
        ("Acme Bio expects to file in September of this year.", "September of this year", date(2026, 8, 19)),
        ("Topline data are expected this quarter.", "this quarter", date(2026, 8, 18)),
        ("Data are expected this fall.", "this fall", date(2026, 8, 10)),
    ],
)
def test_a_year_code_can_settle_from_the_document_date(quote: str, words: str, published: date) -> None:
    assert parse_window(words, published) is not None
    assert writes_year(words, published) is False
    assert unsettled_year(quote, words, published) is None


@pytest.mark.parametrize(
    ("quote", "words", "published", "why"),
    [
        # No year of their own, and no month: a heading or another sentence may give the year.
        ("Topline data are expected in the second half.", "second half", PUBLISHED, "name no year"),
        ("Enrollment completes by year-end.", "by year-end", PUBLISHED, "name no year"),
        ("We expect to file by the end of the year.", "by the end of the year", PUBLISHED, "name no year"),
        ("Data are expected in the fourth quarter.", "fourth quarter", PUBLISHED, "name no year"),
        ("Data are expected in the fall.", "the fall", date(2026, 8, 10), "name no year"),
        # A year counted from the release.
        ("Data are expected next year.", "next year", PUBLISHED, "next year"),
        (
            "Enrollment completes in 2027, with data in the next year.",
            "the next year",
            PUBLISHED,
            "next year",
        ),
        ("Data are expected in March of next year.", "March of next year", PUBLISHED, "next year"),
        # "This year" and "this quarter" in a release filed in the first days of one.
        ("Topline data are expected this year.", "this year", date(2027, 1, 5), "first 14 days of the year"),
        (
            "Data are expected this quarter.",
            "this quarter",
            date(2026, 10, 1),
            "first 14 days of the quarter",
        ),
        # A fiscal year anywhere in the quote.
        ("In fiscal 2027 terms, data are expected in December.", "December", PUBLISHED, "fiscal"),
        # Another year in the quote, however far from the words.
        ("For calendar 2027, the company expects data in December.", "December", PUBLISHED, "2027"),
        ("Data in December, after the 2025 interim look.", "December", PUBLISHED, "2025"),
        ("Data are expected in December ('27).", "December", PUBLISHED, "'27"),
        # The quote goes on with what may be the year.
        ("Results at a meeting on December 15, 2O27.", "December 15", PUBLISHED, "goes on"),
        (
            "Results at a meeting on December 15, \uff12\uff10\uff12\uff17.",
            "December 15",
            PUBLISHED,
            "goes on",
        ),
        ("Results at a meeting on December 15 of next year.", "December 15", PUBLISHED, "goes on"),
        ("Results at a meeting on December 15 of the subsequent annum.", "December 15", PUBLISHED, "goes on"),
        ("Data are expected in December (2O27).", "December", PUBLISHED, "goes on"),
        ("Data are expected in December MMXXVII.", "December", PUBLISHED, "goes on"),
        ("Data are expected in December twenty twenty-seven.", "December", PUBLISHED, "goes on"),
        # A weekday beside the words that is not that day's.
        (
            "Acme Bio will host a RAINIER webcast on Thursday, October 23 in Boston.",
            "October 23",
            PUBLISHED,
            "weekday",
        ),
    ],
)
def test_a_year_code_cannot_settle_says_why(quote: str, words: str, published: date, why: str) -> None:
    reason = unsettled_year(quote, words, published)
    assert reason is not None
    assert why in reason


@given(
    published=published_days,
    words=st.sampled_from(["this year", "later this year", "fourth quarter of this year", "this quarter"]),
)
def test_this_year_is_settled_only_after_the_first_two_weeks_of_the_year_or_quarter(
    published: date, words: str
) -> None:
    """A release filed in the first days of a year may have been written in the year before."""
    first = date(published.year, 3 * ((published.month - 1) // 3) + 1 if words[5:] == "quarter" else 1, 1)
    early = (published - first).days < 14
    assert (unsettled_year(f"Data are expected {words}.", words, published) is not None) is early
