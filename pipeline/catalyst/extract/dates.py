"""Read a date phrase such as "2H26", "mid-2027" or "by year-end" as a window (spec: Date precision).

The phrase is the source's own words; the window is what code, not a model, makes of them.
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
_PREFIX = re.compile(r"^(?:(?:in|during|around|the)\s+)+")

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
    (re.compile(_MONTH + r" (?P<day>\d{1,2}), (?P<year>\d{4})"), _day),
    (re.compile(_MONTH + r" (?P<day>\d{1,2})"), _day_this_year),
    (re.compile(_MONTH + r" (?:of )?" + _YEAR), _month),
    (re.compile(r"q(?P<n>[1-4]) ?" + _COMPACT_YEAR), _quarter_of),
    (re.compile(r"(?P<n>[1-4])q ?" + _COMPACT_YEAR), _quarter_of),
    (re.compile(r"(?P<ord>first|second|third|fourth) quarter(?: of)?" + _OPTIONAL_YEAR), _quarter_of),
    (re.compile(r"this quarter"), _this_quarter),
    (re.compile(r"(?P<n>[12])h ?" + _COMPACT_YEAR), _half_of),
    (re.compile(r"h(?P<n>[12]) ?" + _COMPACT_YEAR), _half_of),
    (re.compile(r"(?P<ord>first|second) half(?: of)?" + _OPTIONAL_YEAR), _half_of),
    (re.compile(r"(?P<part>early|mid|late)[- ]" + _YEAR), _part_of_year),
    (re.compile(r"(?:year[- ]end|(?:the )?end of(?: the)? year)" + _OPTIONAL_YEAR), _year_end),
    (re.compile(r"(?:the )?end of " + _YEAR), _year_end),
    (re.compile(_YEAR), _whole_year),
]


def _period(phrase: str, published: date) -> _Period | None:
    """Return the calendar period a phrase names, or None."""
    phrase = _PREFIX.sub("", phrase)
    for pattern, build in _PATTERNS:
        match = pattern.fullmatch(phrase)
        if match is not None:
            found = match.groupdict()
            return build(found, _year(found.get("year"), published), published)
    return None


_BY = re.compile(r"^by (?:(?:the )?end of )?(?P<rest>.+)$")
_YEAR_END = re.compile(r"^(?:year[- ]end|(?:the )?year|year)(?: (?P<year>\d{4}|'\d{2}))?$")


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
    + r")\.? )?(?P<day2>\d{1,2}),? (?P<year>\d{4})$"
)
_SHARED_NOUN = re.compile(
    r"^(?P<a>first|second|third|fourth) (?:or|to|and|through) (?P<b>first|second|third|fourth) "
    r"(?P<rest>(?:quarter|half)(?: of)?(?: .+)?)$"
)
_JOINER = re.compile(r"\s*[\u2013\u2014/]\s*|\s+(?:or|to|through|and)\s+|\s+-\s+")
_TRAILING_YEAR = re.compile(r"(\d{4}|'\d{2})$")


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
    second = _period(parts[1], published)
    first = _period(parts[0], published)
    year = _TRAILING_YEAR.search(parts[1])
    if first is None and year is not None:  # "Q3 to Q4 2027": the year is written once, at the end
        first = _period(f"{parts[0]} {year.group(1)}", published)
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
