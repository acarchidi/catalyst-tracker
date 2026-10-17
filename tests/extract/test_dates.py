"""Date phrases become windows (spec: Date precision). Written before the parser (AGENTS.md)."""

from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from catalyst.extract.dates import DateWindow, parse_window
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
        "March 26",  # a day before the document's date with no year: next year or a past event?
        "June 15",
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
        assert window.start == date(published.year, month, day)
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
