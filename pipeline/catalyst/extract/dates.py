"""Read a date phrase such as "2H26", "mid-2027" or "by year-end" as a window (spec: Date precision).

The phrase is the source's own words; the window is what code, not a model, makes of them.
Only ASCII digits count: "2\u09ea" (a Bengali four) may look like 28 or 24, so it names no window.
A phrase without a year takes the year of the document it came from. "By" phrases run from
the document's date to the end of the period. Anything vague ("soon", "upon approval") gives
no window. ADR 0012 records the conventions (for example, "mid-2027" is April to September).
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from catalyst.schemas.enums import DatePrecision


@dataclass(frozen=True)
class DateWindow:
    """A catalyst's date window: first and last possible day, precision, and the words as written."""

    start: date
    end: date
    precision: DatePrecision
    raw_text: str


_MONTHS = {
    **{name.lower(): number for number, name in enumerate(calendar.month_name) if name},
    **{name.lower(): number for number, name in enumerate(calendar.month_abbr) if name},
    "sept": 9,
}
_MONTH = "(?P<month>" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
# A two-digit year needs an apostrophe ("April '27"), except in compact forms such as 2H26 and
# 1Q27: alone, "April 27" or "26" is a day or a number, not a year (review of PR #14).
_YEAR = r"(?P<year>\d{4}|'\d{2}|this year|next year|the year)"
_COMPACT_YEAR = r"(?P<year>\d{4}|'?\d{2})"
_PREFIX = re.compile(r"^(?:(?:in|during|around|the)\s+)+", re.ASCII)

_Period = tuple[date, date, DatePrecision]


def _year(written: str | None, published: date) -> int:
    if written is None or written in ("this year", "the year"):
        return published.year
    if written == "next year":
        return published.year + 1
    digits = written.lstrip("'")
    return 2000 + int(digits) if len(digits) == 2 else int(digits)


def _quarter(year: int, quarter: int) -> _Period:
    end_month = 3 * quarter
    return (
        date(year, end_month - 2, 1),
        date(year, end_month, calendar.monthrange(year, end_month)[1]),
        DatePrecision.QUARTER,
    )


def _half(year: int, half: int) -> _Period:
    if half == 1:
        return date(year, 1, 1), date(year, 6, 30), DatePrecision.HALF
    return date(year, 7, 1), date(year, 12, 31), DatePrecision.HALF


def _number(found: dict[str, str | None]) -> int:
    """Return the quarter or half number, written as a digit or as an ordinal word."""
    digit, word = found.get("n"), found.get("ord")
    return int(digit) if digit else _ORDINALS[word or ""]


def _quarter_of(found: dict[str, str | None], year: int, published: date) -> _Period:
    return _quarter(year, _number(found))


def _this_quarter(found: dict[str, str | None], year: int, published: date) -> _Period:
    return _quarter(published.year, (published.month - 1) // 3 + 1)


def _half_of(found: dict[str, str | None], year: int, published: date) -> _Period:
    return _half(year, _number(found))


def _year_end(found: dict[str, str | None], year: int, published: date) -> _Period:
    return _quarter(year, 4)


def _day(found: dict[str, str | None], year: int, published: date) -> _Period:
    day = date(year, _MONTHS[found["month"] or ""], int(found["day"] or ""))
    return day, day, DatePrecision.DAY


def _day_this_year(found: dict[str, str | None], year: int, published: date) -> _Period | None:
    """Read "November 12" as that day in the document's year, if it is still ahead; else no window.

    A day already past could be a past event or the same day next year; code does not guess.
    """
    start, end, precision = _day(found, published.year, published)
    return (start, end, precision) if start >= published else None


def _month(found: dict[str, str | None], year: int, published: date) -> _Period:
    month = _MONTHS[found["month"] or ""]
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last), DatePrecision.MONTH


def _part_of_year(found: dict[str, str | None], year: int, published: date) -> _Period:
    if found["part"] == "mid":
        return date(year, 4, 1), date(year, 9, 30), DatePrecision.HALF
    return _half(year, 1 if found["part"] == "early" else 2)


def _whole_year(found: dict[str, str | None], year: int, published: date) -> _Period:
    return date(year, 1, 1), date(year, 12, 31), DatePrecision.YEAR


_Builder = Callable[[dict[str, str | None], int, date], _Period | None]
_OPTIONAL_YEAR = "(?: " + _YEAR + ")?"
_PATTERNS: list[tuple[re.Pattern[str], _Builder]] = [
    (re.compile(_MONTH + r" (?P<day>\d{1,2}), (?P<year>\d{4})", re.ASCII), _day),
    (re.compile(_MONTH + r" (?P<day>\d{1,2})", re.ASCII), _day_this_year),
    (re.compile(_MONTH + r" (?:of )?" + _YEAR, re.ASCII), _month),
    (re.compile(r"q(?P<n>[1-4]) ?" + _COMPACT_YEAR, re.ASCII), _quarter_of),
    (re.compile(r"(?P<n>[1-4])q ?" + _COMPACT_YEAR, re.ASCII), _quarter_of),
    (
        re.compile(r"(?P<ord>first|second|third|fourth) quarter(?: of)?" + _OPTIONAL_YEAR, re.ASCII),
        _quarter_of,
    ),
    (re.compile(r"this quarter", re.ASCII), _this_quarter),
    (re.compile(r"(?P<n>[12])h ?" + _COMPACT_YEAR, re.ASCII), _half_of),
    (re.compile(r"h(?P<n>[12]) ?" + _COMPACT_YEAR, re.ASCII), _half_of),
    (re.compile(r"(?P<ord>first|second) half(?: of)?" + _OPTIONAL_YEAR, re.ASCII), _half_of),
    (re.compile(r"(?P<part>early|mid|late)[- ]" + _YEAR, re.ASCII), _part_of_year),
    (re.compile(r"(?:year[- ]end|(?:the )?end of(?: the)? year)" + _OPTIONAL_YEAR, re.ASCII), _year_end),
    (re.compile(r"(?:the )?end of " + _YEAR, re.ASCII), _year_end),
    (re.compile(_YEAR, re.ASCII), _whole_year),
]


def _read(phrase: str, published: date) -> tuple[_Period | None, bool]:
    """Return the calendar period a phrase names, or None, and whether the phrase fixes its year.

    A phrase fixes its year when it writes one, or when it names the document's own period ("this
    quarter"). "First quarter" or "November 16" alone does not: it borrows the document's year.
    """
    phrase = _PREFIX.sub("", phrase)
    for pattern, build in _PATTERNS:
        match = pattern.fullmatch(phrase)
        if match is not None:
            found = match.groupdict()
            fixed = build is _this_quarter or found.get("year") is not None
            return build(found, _year(found.get("year"), published), published), fixed
    return None, False


def _period(phrase: str, published: date) -> _Period | None:
    """Return the calendar period a phrase names, or None."""
    return _read(phrase, published)[0]


_BY = re.compile(r"^by (?:(?:the )?end of )?(?P<rest>.+)$", re.ASCII)
_YEAR_END = re.compile(r"^(?:year[- ]end|(?:the )?year|year)(?: (?P<year>\d{4}|'\d{2}))?$", re.ASCII)


def _deadline(phrase: str, published: date) -> _Period | None:
    """Return the window for "by ..." and "later this year", or None if the phrase is neither."""
    if phrase == "later this year":
        end, precision = date(published.year, 12, 31), DatePrecision.YEAR
    else:
        match = _BY.match(phrase)
        if match is None:
            return None
        rest = match.group("rest")
        year_end = _YEAR_END.match(rest)
        if year_end:
            year = _year(year_end.group("year"), published)
            end, precision = date(year, 12, 31), DatePrecision.YEAR
        else:
            period = _period(rest, published)
            if period is None:
                return None
            _, end, precision = period
    if end < published:
        return None
    if end == published:
        return end, end, DatePrecision.DAY
    return published, end, precision


# A range's precision is the coarsest of its parts (ADR 0018, part 3).
_COARSENESS = [
    DatePrecision.DAY,
    DatePrecision.MONTH,
    DatePrecision.QUARTER,
    DatePrecision.HALF,
    DatePrecision.YEAR,
]
_DAY_RANGE = re.compile(
    _MONTH
    + r" (?P<day>\d{1,2}) ?[-\u2010-\u2015] ?(?:(?P<month2>"
    + "|".join(sorted(_MONTHS, key=len, reverse=True))
    + r")\.? )?(?P<day2>\d{1,2}),? (?P<year>\d{4})$",
    re.ASCII,
)
_SHARED_NOUN = re.compile(
    r"^(?P<a>first|second|third|fourth) (?:or|to|and|through) (?P<b>first|second|third|fourth) "
    r"(?P<rest>(?:quarter|half)(?: of)?(?: .+)?)$",
    re.ASCII,
)
_JOINER = re.compile(r"\s*[\u2013\u2014/]\s*|\s+(?:or|to|through|and)\s+|\s+-\s+", re.ASCII)


def _span(first: _Period, second: _Period) -> _Period | None:
    """Join two periods into one window, if the second does not start before the first."""
    if second[0] < first[0] or second[1] < first[1]:
        return None
    precision = max(first[2], second[2], key=_COARSENESS.index)
    if precision is DatePrecision.DAY and first[0] != second[1]:
        precision = DatePrecision.MONTH  # a range of days is not one day
    return first[0], second[1], precision


def _range(phrase: str, published: date) -> _Period | None:
    """Return the window for "November 16-19, 2026", "late 2027 or early 2028", "Q3 to Q4 2027"."""
    days = _DAY_RANGE.fullmatch(phrase)
    if days is not None:
        year = int(days["year"])
        first = date(year, _MONTHS[days["month"]], int(days["day"]))
        second = date(year, _MONTHS[days["month2"] or days["month"]], int(days["day2"]))
        return _span((first, first, DatePrecision.DAY), (second, second, DatePrecision.DAY))
    shared = _SHARED_NOUN.fullmatch(phrase)
    if shared is not None:
        parts = [f"{shared['a']} {shared['rest']}", f"{shared['b']} {shared['rest']}"]
    else:
        between = phrase.startswith("between ")
        parts = _JOINER.split(phrase.removeprefix("between "))
        if len(parts) != 2 or (" and " in phrase) != between:
            return None
    second, second_fixed = _read(parts[1], published)
    first, fixed = _read(parts[0], published)
    if not fixed and second is not None and second_fixed:
        # "Q3 to Q4 2027", "November 16 to December 2, 2027", "first to second quarter of next
        # year": the year is written once, at the end, and the first part takes it, never the
        # document's year (reviews of PR #40).
        year = second[0].year
        first = _period(f"{parts[0]} {year}", published) or _period(f"{parts[0]}, {year}", published)
    elif fixed and first is not None and not second_fixed:
        # "Q3 2026 to year-end", "November 2027 to December": the year is written once, at the
        # start, and the last part takes it. "Q4 2026 to first quarter" then runs backwards and gives
        # no window; code does not guess the next year (Claude review of PR #40).
        year = first[0].year
        second = _period(f"{parts[1]} {year}", published) or _period(f"{parts[1]}, {year}", published)
    if first is None or second is None:
        return None
    return _span(first, second)


def parse_window(text: str, published: date) -> DateWindow | None:
    """Return the window `text` names, reading it as of `published`, or None if it names none."""
    phrase = re.sub(r"\s+", " ", text.lower().replace("\u2019", "'")).strip().rstrip(".")
    try:
        found = _deadline(phrase, published) if phrase.startswith(("by ", "later ")) else None
        if found is None and not phrase.startswith("by "):
            found = _period(phrase, published) or _range(phrase, published)
    except ValueError:  # a day or year that does not exist, such as February 30
        return None
    if found is None:
        return None
    start, end, precision = found
    return DateWindow(start, end, precision, text)


# --- Date words cut from a longer date (CP4 review of part 5c, B2) ---

_TOKEN = re.compile(r"\S+")
_REACH = 6  # how many words either side may belong to the same date
_DATE_PART = (
    r"(?:\d|q[1-4]|[1-4]q|h[12]|[12]h|early|mid|late|first|second|third|fourth|year|end|"
    + "|".join(sorted(_MONTHS, key=len, reverse=True))
    + r")"
)
# An unspaced hyphen, slash, or en dash that joins the words to another date part: "Q4 2026-Q1 2027".
_JOINED_AFTER = re.compile(r"[-/\u2013]" + _DATE_PART, re.IGNORECASE | re.ASCII)
_JOINED_BEFORE = re.compile(r"(?<![^\W_])" + _DATE_PART + r"[\w.']*[-/\u2013]$", re.IGNORECASE | re.ASCII)
_FISCAL = re.compile(r"\bfiscal\b|\bFY\s?'?\d", re.IGNORECASE)


def _key(window: DateWindow | None) -> tuple[date, date, DatePrecision] | None:
    return (window.start, window.end, window.precision) if window else None


def cut_short(quote: str, words: str, published: date) -> str | None:
    """Say why the date words, at some place in the quote, are part of a longer date; None when whole.

    Each place the words appear is read with up to six words either side. The words are cut
    short when a longer phrase there names another window ("second half" of "second half of
    2027"), when an unspaced joiner ties them to another date part ("Q4 2026-Q1 2027"), or when
    the date is a fiscal one. One cut place is enough: code cannot tell which place a reader meant.
    """
    parts = words.split()
    if not parts:
        return None
    own = _key(parse_window(words, published))
    pattern = r"(?<!\w)" + r"\s+".join(map(re.escape, parts)) + r"(?!\w)"
    for found in re.finditer(pattern, quote):
        before_text, after_text = quote[: found.start()], quote[found.end() :]
        if _JOINED_AFTER.match(after_text) or _JOINED_BEFORE.search(before_text):
            return "are joined to more of a date in the quote"
        before = _TOKEN.findall(before_text)[-_REACH:]
        after = _TOKEN.findall(after_text)[:_REACH]
        if _FISCAL.search(" ".join([*before[-3:], found.group(0), *after[:3]])):
            return "are part of a fiscal-year date, not a calendar one"
        for left in range(len(before) + 1):
            for right in range(len(after) + 1):
                if left == right == 0:
                    continue
                phrase = " ".join([*before[len(before) - left :], found.group(0), *after[:right]])
                phrase = re.sub(r"\s+(?=[,;])", "", phrase).strip(" ,;:.()")
                window = parse_window(phrase, published)
                if window is not None and _key(window) != own:
                    return f"are cut from the longer date '{phrase}' in the quote"
    return None


# --- Date words with no year of their own (CP4 final review, B1 and round 3, B1') ---

# Words that count the year from the document itself: it is their own year, not a borrowed one.
_COUNTED = re.compile(r"\b(?:this|next)\s+year\b|\bthis\s+quarter\b", re.IGNORECASE)


def _a_year_later(published: date) -> date:
    return date(published.year + 1, published.month, min(published.day, 28 if published.month == 2 else 31))


def borrows_year(words: str, published: date) -> bool:
    """Say whether the date words name a window only by taking the document's year.

    "Second half", "December 15", and "by year-end" do: read a year later, they give a window a
    year later. Such words cannot publish on their own, because the real year may be written
    anywhere, in any form: beside them in a form code cannot read ("2O27", "MMXXVII"), counted from
    something else ("of the following year"), further away, or only in a heading. Words that write
    a year ("2027", "'27", "2H26") or count it from the document ("this year", "next year", "this
    quarter") do not borrow it.
    """
    if _COUNTED.search(words):
        return False
    here, later = parse_window(words, published), parse_window(words, _a_year_later(published))
    return here is not None and later is not None and here.end != later.end
