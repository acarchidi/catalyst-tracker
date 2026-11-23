"""Read a date phrase such as "2H26", "mid-2027" or "by year-end" as a window (spec: Date precision).

The phrase is the source's own words; the window is what code, not a model, makes of them.
Only ASCII digits count: "2\u09ea" (a Bengali four) may look like 28 or 24, so it names no window.
A phrase without a year takes the year of the document it came from; a month or a day without
one is the first on or after the document's date, if that is near (ADR 0061). "By" phrases run
from the document's date to the end of the period. Anything vague ("soon", "upon approval")
gives no window. ADR 0012 records the conventions (for example, "mid-2027" is April to September).
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
_PREFIX = re.compile(r"^(?:(?:on or about|in|during|around|the|on|at|near|throughout)\s+)+", re.ASCII)
_WEEKDAYS = {
    **{name.lower(): number for number, name in enumerate(calendar.day_name)},
    **{name.lower(): number for number, name in enumerate(calendar.day_abbr)},
    "tues": 1,
    "thur": 3,
    "thurs": 3,
}
_WEEKDAY = re.compile(
    "(?P<weekday>" + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")\.?,? ", re.ASCII
)
# A time of day after a day: "at 8:15 AM CEST", "10:25 \u2013 10:35 CEST", "from 1:45 - 3:00 PM CEST".
_CLOCK = re.compile(
    r"(?:,| at| from)? \d{1,2}:\d{2}(?: ?[ap]m)?(?: ?(?:-|\u2013|to) ?\d{1,2}:\d{2}(?: ?[ap]m)?)?"
    r"(?: (?:[ecmp][sd]?t|gmt|utc|bst|ces?t))?$",
    re.ASCII,
)
# A curly apostrophe is an apostrophe ("Q3 \u201828") and a non-breaking hyphen a hyphen ("mid\u20112027").
_SAME_MARKS = str.maketrans({"\u2018": "'", "\u2019": "'", "\u2010": "-", "\u2011": "-"})
# A month or a day with no year is the first one on or after the document's date, when it starts
# within this many days of it. Further off, it is more likely a past event, or a year written
# somewhere else, so it names no window (ADR 0061).
AHEAD_DAYS = 183
_SEASONS = {"spring": 3, "summer": 6, "fall": 9, "autumn": 9}  # first month; four months each

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


def _ahead(build: Callable[[int], _Period | None], published: date) -> _Period | None:
    """Return the first such period not over on the document's date, if it starts near enough."""
    for year in (published.year, published.year + 1):
        try:
            period = build(year)
        except ValueError:  # February 29 in a year without one
            continue
        if period is not None and period[1] >= published:
            return period if (period[0] - published).days <= AHEAD_DAYS else None
    return None


def _day_ahead(found: dict[str, str | None], year: int, published: date) -> _Period | None:
    """Read "November 12" as the first November 12 on or after the document's date, if it is near."""
    return _ahead(lambda year: _day(found, year, published), published)


def _month(found: dict[str, str | None], year: int, published: date) -> _Period:
    month = _MONTHS[found["month"] or ""]
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last), DatePrecision.MONTH


def _month_ahead(found: dict[str, str | None], year: int, published: date) -> _Period | None:
    """Read "December" as the first December not over on the document's date, if it is near."""
    return _ahead(lambda year: _month(found, year, published), published)


def _season(found: dict[str, str | None], year: int, published: date) -> _Period:
    first = _SEASONS[found["season"] or ""]
    return (
        date(year, first, 1),
        date(year, first + 3, calendar.monthrange(year, first + 3)[1]),
        DatePrecision.HALF,
    )


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
    (re.compile(_MONTH + r" (?P<day>\d{1,2})", re.ASCII), _day_ahead),
    (re.compile(_MONTH + r" (?:of )?" + _YEAR, re.ASCII), _month),
    (re.compile(_MONTH, re.ASCII), _month_ahead),
    (re.compile(r"q(?P<n>[1-4])(?: ?|-(?=\d{4}))" + _COMPACT_YEAR, re.ASCII), _quarter_of),
    (re.compile(r"(?P<n>[1-4])q ?" + _COMPACT_YEAR, re.ASCII), _quarter_of),
    (
        re.compile(r"(?P<ord>first|second|third|fourth) quarter(?: of)?" + _OPTIONAL_YEAR, re.ASCII),
        _quarter_of,
    ),
    (re.compile(r"this quarter", re.ASCII), _this_quarter),
    (re.compile(r"(?P<n>[12])h ?" + _COMPACT_YEAR, re.ASCII), _half_of),
    (re.compile(r"h(?P<n>[12]) ?" + _COMPACT_YEAR, re.ASCII), _half_of),
    (re.compile(r"(?P<ord>first|second) half(?: of)?" + _OPTIONAL_YEAR, re.ASCII), _half_of),
    (re.compile(r"(?P<part>early|mid|late)[- ](?:year )?" + _YEAR, re.ASCII), _part_of_year),
    (
        re.compile(r"(?:this )?(?P<season>spring|summer|fall|autumn)(?: of)?" + _OPTIONAL_YEAR, re.ASCII),
        _season,
    ),
    (re.compile(r"(?:year[- ]end|(?:the )?end of(?: the)? year)" + _OPTIONAL_YEAR, re.ASCII), _year_end),
    (re.compile(r"(?:the )?end of " + _YEAR, re.ASCII), _year_end),
    (re.compile(_YEAR, re.ASCII), _whole_year),
]


# "Early", "mid", "late", or "end of" a month, a quarter, or a season: "early Q1 2027", "late summer
# 2026", "at the end of the third quarter of 2026", "End Nov. 2026" (ADR 0061).
_PART = re.compile(r"(?P<part>early|mid|late|end)(?:-| (?:of |in )?(?:the )?)(?P<rest>.+)", re.ASCII)
# Which months of a three-month quarter or a four-month season each word takes, first to last.
_PART_MONTHS = {
    3: {"early": (0, 1), "mid": (1, 1), "late": (1, 2), "end": (2, 2)},
    4: {"early": (0, 1), "mid": (1, 2), "late": (2, 3), "end": (3, 3)},
}
_PART_DAYS = {"early": (1, 15), "mid": (8, 22), "late": (16, 31), "end": (16, 31)}


def _part_of(period: _Period, part: str) -> _Period | None:
    """Cut a whole month, quarter, or season down to its early, middle, late, or end part."""
    start, end, _ = period
    months = 12 * (end.year - start.year) + end.month - start.month + 1
    if start.day != 1 or end.day != calendar.monthrange(end.year, end.month)[1]:
        return None
    if months == 1:
        first, last = _PART_DAYS[part]
        return start.replace(day=first), start.replace(day=min(last, end.day)), DatePrecision.MONTH
    if months not in _PART_MONTHS:
        return None
    first, last = _PART_MONTHS[months][part]
    month = start.month + last
    cut_end = date(start.year, month, calendar.monthrange(start.year, month)[1])
    precision = DatePrecision.MONTH if first == last else DatePrecision.QUARTER
    return start.replace(month=start.month + first), cut_end, precision


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
    part = _PART.fullmatch(phrase)
    if part is not None:
        whole, fixed = _read(part["rest"], published)
        if whole is not None:
            return _part_of(whole, part["part"]), fixed
    return None, False


def _period(phrase: str, published: date) -> _Period | None:
    """Return the calendar period a phrase names, or None."""
    return _read(phrase, published)[0]


# "Before the end of 2026" is "by the end of 2026"; "before December" ends before it, so it is not read.
_BY = re.compile(r"^(?:by (?:(?:the )?end of )?|before (?:the )?end of )(?P<rest>.+)$", re.ASCII)
_DEADLINE_STARTS = ("by ", "before ")
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
    # "By December 15" runs over many days: month precision, as for a range of days (ADR 0061).
    return published, end, DatePrecision.MONTH if precision is DatePrecision.DAY else precision


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
    + r")\.? )?(?P<day2>\d{1,2})(?:,? (?P<year>\d{4}))?$",
    re.ASCII,
)
_SHARED_NOUN = re.compile(
    r"^(?P<a>first|second|third|fourth) (?:or|to|and|through) (?P<b>first|second|third|fourth) "
    r"(?P<rest>(?:quarter|half)(?: of)?(?: .+)?)$",
    re.ASCII,
)
# An unspaced hyphen joins a year to a quarter or a half ("Q4 2026-Q1 2027"), never "mid-2027".
_JOINER = re.compile(
    r"\s*[\u2013\u2014/]\s*|\s+(?:or|to|through|and)\s+|\s+-\s+|(?<=\d)-(?=q[1-4]|[1-4]q|h[12]|[12]h)",
    re.ASCII,
)


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

        def in_year(year: int) -> _Period | None:
            first = date(year, _MONTHS[days["month"]], int(days["day"]))
            second = date(year, _MONTHS[days["month2"] or days["month"]], int(days["day2"]))
            return _span((first, first, DatePrecision.DAY), (second, second, DatePrecision.DAY))

        # "August 28-31" with no year is the first such days not over on the document's date.
        return in_year(int(days["year"])) if days["year"] else _ahead(in_year, published)
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


def _plain(text: str) -> str:
    """Return a phrase in lower case, with one kind of apostrophe and hyphen, and no "9th" or "calendar"."""
    phrase = re.sub(r"\s+", " ", text.lower().translate(_SAME_MARKS)).strip().rstrip(".")
    phrase = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", phrase, flags=re.ASCII)  # "October 9th"
    phrase = re.sub(r"\bcalendar (?:year )?(?=\S)", "", phrase)  # "calendar H1 2027", "of calendar year 2027"
    return re.sub(r" time ?frame$", "", phrase)


def parse_window(text: str, published: date) -> DateWindow | None:
    """Return the window `text` names, reading it as of `published`, or None if it names none."""
    phrase = _PREFIX.sub("", _plain(text))
    # A weekday or a time of day goes with one day, and the weekday must be that day's (ADR 0061).
    weekday = _WEEKDAY.match(phrase)
    phrase, clock = _CLOCK.subn("", phrase[weekday.end() :] if weekday else phrase)
    try:
        found = _deadline(phrase, published) if phrase.startswith((*_DEADLINE_STARTS, "later ")) else None
        if found is None and not phrase.startswith(_DEADLINE_STARTS):
            found = _period(phrase, published) or _range(phrase, published)
    except ValueError:  # a day or year that does not exist, such as February 30
        return None
    if found is None:
        return None
    start, end, precision = found
    if (weekday or clock) and precision is not DatePrecision.DAY:
        return None
    if weekday and start.weekday() != _WEEKDAYS[weekday["weekday"]]:
        return None
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
# A fiscal year in any spelling: "fiscal 2027", "FY2027", "FY '27", "FY-2027", "F.Y. 2027",
# "FY Q4 2027", "FY-Q4 2027", "FY 4Q27" (CP4 review, O2). "FYI" and "Fyarro" are not one.
_FISCAL = re.compile(
    r"\bfiscal\b|\bF\.?Y\.?(?=[\s\-\u2010-\u2013]*(?:'?\d|Q[1-4]|[1-4]Q|H[12]|[12]H))", re.IGNORECASE
)


def _key(window: DateWindow | None) -> tuple[date, date, DatePrecision] | None:
    return (window.start, window.end, window.precision) if window else None


def _places(quote: str, words: str) -> list[re.Match[str]]:
    """Return every place the date words appear in the quote as whole words."""
    parts = words.split()
    if not parts:
        return []
    return list(re.finditer(r"(?<!\w)" + r"\s+".join(map(re.escape, parts)) + r"(?!\w)", quote))


def _longer(quote: str, found: re.Match[str], published: date) -> list[tuple[int, int, str, DateWindow]]:
    """Return each longer stretch of the quote around one place of the words that reads as another window.

    A stretch takes up to six more words either side, in the quote's own characters, without the
    punctuation at its ends. Each is given as its start, its end, its text, and its window.
    """
    own = _key(parse_window(found.group(0), published))
    starts = [found.start(), *(m.start() for m in list(_TOKEN.finditer(quote, 0, found.start()))[-_REACH:])]
    ends = [found.end(), *(m.end() for m in list(_TOKEN.finditer(quote, found.end()))[:_REACH])]
    longer: dict[tuple[int, int], tuple[int, int, str, DateWindow]] = {}
    for start in starts:
        for end in ends:
            phrase = quote[start:end].strip(" ,;:.()")
            window = parse_window(phrase, published) if phrase else None
            if window is not None and _key(window) != own:
                at = quote.index(phrase, start)
                longer.setdefault((at, at + len(phrase)), (at, at + len(phrase), phrase, window))
    return list(longer.values())


def cut_short(quote: str, words: str, published: date) -> str | None:
    """Say why the date words, at some place in the quote, are part of a longer date; None when whole.

    Each place the words appear is read with up to six words either side. The words are cut
    short when a longer phrase there names another window ("second half" of "second half of
    2027"), when an unspaced joiner ties them to another date part ("2027-2028"), or when
    the date is a fiscal one. One cut place is enough: code cannot tell which place a reader meant.
    """
    for found in _places(quote, words):
        before_text, after_text = quote[: found.start()], quote[found.end() :]
        if _JOINED_AFTER.match(after_text) or _JOINED_BEFORE.search(before_text):
            return "are joined to more of a date in the quote"
        before = _TOKEN.findall(before_text)[-3:]
        if _FISCAL.search(" ".join([*before, found.group(0), *_TOKEN.findall(after_text)[:3]])):
            return "are part of a fiscal-year date, not a calendar one"
        longer = _longer(quote, found, published)
        if longer:
            phrase = re.sub(r"\s+", " ", min(longer, key=lambda stretch: stretch[1] - stretch[0])[2])
            return f"are cut from the longer date '{phrase}' in the quote"
    return None


def whole_date(quote: str, words: str, published: date) -> str:
    """Return the whole date the words are cut from, in the quote's own words, or the words as given.

    A model sometimes copies part of a date: "mid-2027" of "by mid-2027", "2027" of "early 2027".
    Code takes the whole date only when it can tell which one is meant (ADR 0061, part 3): the
    words appear once in the quote; one longer stretch around them holds every other stretch that
    reads as another window; a stretch with that window writes its year (the shortest such is
    taken); and the word before it is not "from", which may tell of a change of date ("moved from
    Q4 2026 to 2027"). Anything else is left as given, and `cut_short` holds it.
    """
    places = _places(quote, words)
    longer = _longer(quote, places[0], published) if len(places) == 1 else []
    if not longer:
        return words
    start, end = min(stretch[0] for stretch in longer), max(stretch[1] for stretch in longer)
    widest = [stretch for stretch in longer if (stretch[0], stretch[1]) == (start, end)]
    if not widest or re.search(r"\bfrom\s*$", quote[:start], re.IGNORECASE):
        return words
    same = [
        stretch
        for stretch in longer
        if _key(stretch[3]) == _key(widest[0][3]) and writes_year(stretch[2], published)
    ]
    if not same:
        return words
    return " ".join(min(same, key=lambda stretch: (stretch[1] - stretch[0], stretch[0]))[2].split())


# --- Date words must write their year (CP4 final review B1, rounds 3 and 4; owner, 2026-10-19) ---

_YEARS_EARLIER = 4  # a leap year again, so "February 29" is read the same way both times


def writes_year(words: str, published: date) -> bool:
    """Say whether the date words write their year in digits, so they publish without a person.

    "Second half of 2027", "December 15, 2027", "Q4 2027", "2H27", and "Q4 '27" do: read as of
    a document four years earlier, they give the same window. Words with no year ("second half",
    "December 15", "by year-end") or a year counted from the document ("this year", "next year",
    "the next year", "this quarter", "later this year", or a range with such a part) do not: their
    window moves with the document's date, and the real year may be written anywhere, in any form.
    Words that name no window either way ("FY2027", "soon") do not write a year either. A "by"
    phrase always starts on the document's date, so only its end must stay the same.
    """
    here = parse_window(words, published)
    try:
        moved = published.replace(year=published.year - _YEARS_EARLIER)
    except ValueError:  # February 29 in a year whose fourth year before is not a leap year (2104)
        return False
    earlier = parse_window(words, moved)
    if here is None or earlier is None or (here.end, here.precision) != (earlier.end, earlier.precision):
        return False
    # Only the end of a "by" phrase is compared: `_deadline` always starts it on the document's
    # date (or makes it that one day when the deadline is that day), so its start moves with the
    # document whatever the words say. tests/extract/test_dates.py pins this (Claude review of PR #71).
    by = _PREFIX.sub("", _plain(words)).startswith(_DEADLINE_STARTS)
    return by or here.start == earlier.start


# --- A year the words do not write, settled from the document's date (ADR 0061, part 2) ---

FILING_LAG_DAYS = 14  # a release may be filed some days after it was written (ADR 0018, part 5e)
_COUNTED = re.compile(r"\bnext\b", re.ASCII)
_THIS = re.compile(r"\bthis (?P<unit>year|quarter|spring|summer|fall|autumn)\b", re.ASCII)
_MONTH_WORD = re.compile(r"\b(?:" + "|".join(_MONTHS) + r")\b", re.ASCII)
_PERIOD_WORD = re.compile(r"\b(?:year|quarter|half|q[1-4]|[1-4]q|h[12]|[12]h)\b", re.ASCII)
_WRITTEN_YEAR = re.compile(r"(?<![\w.'])(?:19|20)\d{2}(?!\w)|(?<!\w)'\d{2}(?!\w)", re.ASCII)
# What follows the words: an optional comma, "of", and bracket, then the next word.
_NEXT_WORD = re.compile(r"\s*,?\s*(?P<of>of\b\s*)?[(\[]?(?P<word>[^\s,;:.)\]]*)")
# A next word that may be a year: it starts with a digit, is a Roman numeral, or spells a number.
_MAY_BE_YEAR = re.compile(r"'?\d|[MDCLXVI]{2,}$|(?i:two|twenty|nineteen)\b", re.ASCII)


def _goes_on(after: str, years: set[int]) -> bool:
    """Say whether the quote goes on, right after the date words, with what may be their year."""
    found = _NEXT_WORD.match(after)
    word = found["word"] if found else ""
    if word.isascii() and word.isdigit() and int(word) in years:
        return False  # the year code took, written out
    foreign = any(not ch.isascii() and ch.isalnum() for ch in word)
    return bool(found and found["of"]) or foreign or _MAY_BE_YEAR.match(word) is not None


def _this_period(unit: str, published: date) -> str | None:
    """Say why "this year" or "this quarter" may not be the document's own; None when it is."""
    unit = "quarter" if unit == "quarter" else "year"  # a season is in the document's year
    first = date(published.year, 3 * ((published.month - 1) // 3) + 1 if unit == "quarter" else 1, 1)
    if (published - first).days >= FILING_LAG_DAYS:
        return None
    return (
        f"the release came out in the first {FILING_LAG_DAYS} days of the {unit}, so it may have "
        "been written in the one before"
    )


def _other_year(quote: str, words: str, published: date) -> str | None:
    """Say why the quote may give a month or a day another year than the next one; None when it does not."""
    window = parse_window(words, published)
    years = {window.start.year, window.end.year} if window else set[int]()
    for written in _WRITTEN_YEAR.findall(quote):
        if (2000 + int(written[1:]) if written.startswith("'") else int(written)) not in years:
            return f"the quote also writes the year {written}"
    for found in _places(quote, words):
        if _goes_on(quote[found.end() :], years):
            return "the quote goes on after them with what may be their year"
        before = re.search(r"(?P<name>[A-Za-z]+)\.?,?\s*$", quote[: found.start()])
        weekday = before["name"] if before and before["name"].lower() in _WEEKDAYS else None
        if weekday and parse_window(f"{weekday}, {words}", published) is None:
            return "the quote gives a weekday that is not that day's"
    return None


def unsettled_year(quote: str, words: str, published: date) -> str | None:
    """Say why code cannot take the year of year-less date words from the document's date; None when it can.

    Code settles two kinds of words. "This year" and "this quarter" name the document's own,
    unless it came out in the first days of one, when it may have been written in the one before.
    A month or a day ("in December", "Friday, October 23") is the first on or after the document's
    date, as `parse_window` reads it, unless the quote writes another year, goes on after the words
    with what may be one, speaks of a fiscal year, or gives a weekday that is not that day's.
    Words with no year and no month ("second half", "by year-end") and words that count from the
    release ("next year") stay unsettled: a heading may give the year, and "the next year" may mean
    twelve months (ADR 0018, part 5e).
    """
    phrase = _plain(words)
    if _FISCAL.search(quote):
        return "the quote speaks of a fiscal year"
    if _COUNTED.search(phrase):
        return "'next year' may mean the calendar year or the twelve months after the release"
    this = _THIS.search(phrase)
    if this is not None:
        return _this_period(this["unit"], published)
    if _MONTH_WORD.search(phrase) is None or _PERIOD_WORD.search(phrase) is not None:
        return "they name no year of their own, and a heading or another sentence may give one"
    return _other_year(quote, words, published)
