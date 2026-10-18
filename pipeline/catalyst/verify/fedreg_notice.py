"""Read an FDA advisory committee meeting notice from the Federal Register in plain code (ADR 0018, part 3).

The rule check "an advisory meeting needs a Federal Register notice" compares a claimed meeting with
the notices we stored. This module turns one stored notice into facts: the committee, the meeting's
first and last day, and each drug or device named with its application number and company. No model
reads the notice.

Every fact carries the sentence it came from, with its character offsets in the notice text (spec:
Provenance). The notice text is the stored copy's own fields, one per line, in a fixed order:
`title`, `abstract`, `dates`. Structured fields come first: the committee and the drug are read from
the title, which FDA writes in a fixed form, and the meeting days from the DATES section. Where a
fact cannot be found it is None or an empty list, never a guess.

The search answer the collector stores today has no DATES section, so no stored notice gives its
meeting days yet. Search excerpts are never read: they are cut mid-sentence, and in an amendment
they quote the date the amendment changed.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import pairwise
from typing import Any, cast

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from catalyst.collectors.documents import new_york_midnight
from catalyst.db.tables import TABLES
from catalyst.logs import log_event
from catalyst.schemas import SourceDocument
from catalyst.snapshots import SnapshotStore, SnapshotStoreError

SOURCE_PREFIX = "fedreg:"
TEXT_FIELDS = ("title", "abstract", "dates")
MAX_MEETING_DAYS = 7  # advisory meetings run one to three days; a longer span is two dates mixed up
MAX_DATE_SENTENCE = 1000  # a DATES sentence is one line; anything longer is not read for days

_log = logging.getLogger(__name__)


class NoticeError(ValueError):
    """The stored copy is not a Federal Register document this reader understands."""


@dataclass(frozen=True)
class Evidence:
    """The exact sentence a value came from: the field it is in, and its place in the notice text."""

    field: str
    start: int
    end: int
    quote: str


@dataclass(frozen=True)
class Fact[T]:
    """One value read from the notice, with its sentence."""

    value: T
    evidence: Evidence


@dataclass(frozen=True)
class Application:
    """An FDA application as the notice numbers it, for example NDA 219694."""

    kind: str
    number: str

    def __str__(self) -> str:
        """Return the application the way FDA writes it."""
        return f"{self.kind} {self.number}"


@dataclass(frozen=True)
class MeetingDates:
    """The first and last day of the meeting (the same day for a one-day meeting)."""

    start: date
    end: date


@dataclass(frozen=True)
class Subject:
    """One drug or device the meeting is about, with its application and company where named."""

    product: Fact[str] | None
    application: Fact[Application] | None
    sponsor: Fact[str] | None


@dataclass(frozen=True)
class MeetingNotice:
    """What plain code could read from one notice."""

    document_number: str
    publication_date: date
    text: str
    committee: Fact[str] | None
    topic: Fact[str] | None
    meeting: Fact[MeetingDates] | None
    subjects: tuple[Subject, ...]
    amendment: Evidence | None

    @property
    def products(self) -> list[Fact[str]]:
        """Every drug or device named, once each, as the notice writes it."""
        return _unique(s.product for s in self.subjects)

    @property
    def applications(self) -> list[Fact[Application]]:
        """Every application number named, once each."""
        return _unique(s.application for s in self.subjects)

    @property
    def sponsors(self) -> list[Fact[str]]:
        """Every company named as an application's sponsor, once each."""
        return _unique(s.sponsor for s in self.subjects)

    def missing(self) -> list[str]:
        """Name each kind of fact this notice did not give."""
        found = {
            "committee": self.committee is not None,
            "meeting dates": self.meeting is not None,
            "products": bool(self.products),
            "applications": bool(self.applications),
            "sponsors": bool(self.sponsors),
        }
        return [name for name, present in found.items() if not present]


def _unique[T](facts: Iterator[Fact[T] | None]) -> list[Fact[T]]:
    seen: set[T] = set()
    kept: list[Fact[T]] = []
    for fact in facts:
        if fact is not None and fact.value not in seen:
            seen.add(fact.value)
            kept.append(fact)
    return kept


# --- Sentences ----------------------------------------------------------------------------------------

# Words whose period never ends a sentence in these notices.
_ABBREVIATIONS = frozenset(
    {
        "inc.", "ltd.", "co.", "corp.", "no.", "nos.", "dr.", "mr.", "ms.", "st.",
        "ave.", "bldg.", "rm.", "u.s.", "e.g.", "i.e.", "vs.", "jan.", "feb.", "mar.", "apr.", "jun.",
        "jul.", "aug.", "sep.", "sept.", "oct.", "nov.", "dec.",
    }
)  # fmt: skip
_SENTENCE_END = re.compile(r"[.?!](?=\s+(?P<next>[A-Z0-9(\"\u201c]\w*))")
# "5 p.m." ends a sentence unless a time zone follows: "from 9 a.m. to 5 p.m. Eastern Time."
_CLOCK = frozenset({"a.m.", "p.m."})
_TIME_ZONE_WORDS = frozenset({"Eastern", "Central", "Mountain", "Pacific", "ET", "EST", "EDT", "Time"})


@dataclass(frozen=True)
class _Unit:
    """A piece of the notice text that patterns read, and the evidence it gives."""

    text: str
    evidence: Evidence


def _sentences(field_name: str, value: str, offset: int) -> Iterator[_Unit]:
    """Split one field into sentences, each with its offsets in the notice text."""
    start = 0
    for end_mark in _SENTENCE_END.finditer(value):
        last_word = value[: end_mark.end()].rsplit(None, 1)[-1].lower()
        if last_word in _ABBREVIATIONS or (last_word in _CLOCK and end_mark["next"] in _TIME_ZONE_WORDS):
            continue
        yield from _unit(field_name, value, offset, start, end_mark.end())
        start = end_mark.end()
    yield from _unit(field_name, value, offset, start, len(value))


def _unit(field_name: str, value: str, offset: int, start: int, end: int) -> Iterator[_Unit]:
    piece = value[start:end]
    stripped = piece.strip()
    if stripped:
        begin = offset + start + (len(piece) - len(piece.lstrip()))
        yield _Unit(stripped, Evidence(field_name, begin, begin + len(stripped), stripped))


# --- Meeting days ------------------------------------------------------------------------------------

_MONTH_NUMBERS = {
    name: number
    for number, name in enumerate(
        ["January", "February", "March", "April", "May", "June", "July", "August", "September",
         "October", "November", "December"],
        start=1,
    )
}  # fmt: skip
_MONTH_NUMBERS.update(
    {"Jan.": 1, "Feb.": 2, "Mar.": 3, "Apr.": 4, "Jun.": 6, "Jul.": 7, "Aug.": 8, "Sep.": 9, "Sept.": 9}
)
_MONTH_NUMBERS.update({"Oct.": 10, "Nov.": 11, "Dec.": 12})
_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_WEEKDAY = "(?:" + "|".join(_WEEKDAY_NAMES) + ")"
_MONTH = "(?P<month>" + "|".join(re.escape(m) for m in sorted(_MONTH_NUMBERS, key=len, reverse=True)) + ")"
_LIST = r"(?:\s*,\s*(?:and\s+)?|\s+and\s+)"
# A day number is never a clock time ("16, 9 a.m.") or the first digits of a year.
_DAY = r"\d{1,2}(?!\d)(?:st|nd|rd|th)?(?!\s*(?:[ap]\.m\.|:\d))"
_DAY_SEPARATOR = rf"(?:{_LIST}|\s*[-\u2013\u2014]\s*|\s+(?:through|to)\s+)"
_DATE_GROUP = re.compile(
    rf"(?:\b(?P<weekdays>{_WEEKDAY}(?:{_LIST}{_WEEKDAY})*),?\s+)?\b{_MONTH}(?![A-Za-z])\s+"
    rf"(?P<days>{_DAY}(?:{_DAY_SEPARATOR}{_DAY})*)(?:,?\s+(?P<year>\d{{4}})(?!\d))?"
)
# A sentence about a change names two dates, and only one of them is the meeting.
_CHANGE = re.compile(
    r"\b(?:instead of|rather than|originally|previously|rescheduled|postponed|was announced|would be held"
    r"|scheduled for|changed|not|or)\b|\breplac",
    re.IGNORECASE,
)
# Two groups of days belong to one meeting only when plain words join them: "16, 2026, and
# November 17, 2026", "30 through December 1", or the first day's hours between: ", from 9 a.m. to
# 5 p.m., and". Any other words between ("and the docket closes") mean a second, different date.
_HOURS = (
    r",\s*from\s+[^,;]{1,60}?(?:[ap]\.m\.|noon)"
    r"(?:\s+(?:Eastern|Central|Mountain|Pacific)\s+(?:(?:Standard|Daylight)\s+)?Time|\s+(?:ET|EST|EDT))?,"
)
_JOINER = re.compile(rf"(?:{_HOURS})?\s*,?\s*(?:(?:and|through|to)\s+|[-\u2013\u2014]\s*)?(?:on\s+)?")
# A day number straight after a year ("November 16, 2026 to 18") is a range written oddly.
_DANGLING_DAY = re.compile(rf"{_DAY_SEPARATOR}{_DAY}")
_MEETING_SENTENCE = re.compile(r"\b(?:will|is scheduled to)\s+(?:be\s+held|take\s+place|meet|convene)\b")


@dataclass(frozen=True)
class _Group:
    month: int
    days: list[int]
    weekdays: list[str]
    year: int | None


def _groups(sentence: str) -> list[_Group] | None:
    """Return the sentence's groups of days, or None when it names days that are not one meeting."""
    matches = list(_DATE_GROUP.finditer(sentence))
    for before, after in pairwise(matches):
        if not _JOINER.fullmatch(sentence, before.end(), after.start()):
            return None
    if matches and _DANGLING_DAY.match(sentence, matches[-1].end()):
        return None
    return [
        _Group(
            month=_MONTH_NUMBERS[m["month"]],
            days=[int(d) for d in re.findall(r"\d{1,2}", m["days"])],
            weekdays=re.findall(_WEEKDAY, m["weekdays"] or ""),
            year=int(m["year"]) if m["year"] else None,
        )
        for m in matches
    ]


def _with_years(groups: list[_Group]) -> list[tuple[int, _Group]] | None:
    """Give each group the year written after it (one year earlier across New Year), else none."""
    dated: list[tuple[int, _Group]] = []
    following: tuple[int, int] | None = None  # (year, month) of the next group that has a year
    for group in reversed(groups):
        if group.year is not None:
            following = (group.year, group.month)
            dated.append((group.year, group))
        elif following is not None:
            year, month = following
            dated.append((year - 1 if group.month > month else year, group))
        else:
            return None
    return dated[::-1]


def _weekdays_agree(day: date, group: _Group, days: list[date]) -> bool:
    if not group.weekdays:
        return True
    if len(group.weekdays) == len(days):
        return all(_WEEKDAY_NAMES[d.weekday()] == w for d, w in zip(days, group.weekdays, strict=True))
    return len(group.weekdays) == 1 and _WEEKDAY_NAMES[day.weekday()] == group.weekdays[0]


def _calendar_days(groups: list[_Group]) -> list[date] | None:
    dated = _with_years(groups)
    if dated is None:
        return None
    every: list[date] = []
    for year, group in dated:
        try:
            days = [date(year, group.month, d) for d in group.days]
        except ValueError:
            return None
        if not _weekdays_agree(days[0], group, days):
            return None
        every += days
    # Days are written in order; "November 17-16" is a mistake, not a meeting.
    if any(later <= earlier for earlier, later in pairwise(every)):
        return None
    return every


def parse_meeting_dates(sentence: str) -> tuple[date, date] | None:
    """Read the meeting's first and last day from a sentence such as "...held on November 16, 2026, ...".

    Every day must be written with its month and, somewhere after it, its year. A named weekday must
    agree with its date. Groups of days count together only when plain words join them. Anything
    else (no year, an impossible day, days out of order, a change of date, another date that is not
    the meeting, days more than a week apart) gives None.
    """
    if len(sentence) > MAX_DATE_SENTENCE or _CHANGE.search(sentence):
        return None
    groups = _groups(sentence)
    days = _calendar_days(groups) if groups else None
    if not days:
        return None
    start, end = min(days), max(days)
    if (end - start).days > MAX_MEETING_DAYS:
        return None
    return start, end


def _meeting(units: list[_Unit]) -> Fact[MeetingDates] | None:
    """Return the meeting days, when the meeting sentences give exactly one reading."""
    found: list[Fact[MeetingDates]] = []
    for unit in units:
        if unit.evidence.field == "title" or not _MEETING_SENTENCE.search(unit.text):
            continue
        parsed = parse_meeting_dates(unit.text)
        if parsed is not None:
            found.append(Fact(MeetingDates(*parsed), unit.evidence))
    readings = {fact.value for fact in found}
    return found[0] if len(readings) == 1 else None


# --- Committee, topic, and amendment -----------------------------------------------------------------

_TOPIC = re.compile(r"(?:Request for Comments|Amendment of Notice)\s*[-\u2013\u2014]\s*(?P<topic>\S.*?)\s*$")
_ABSTRACT_COMMITTEE = re.compile(
    r"meeting of the (?P<name>[A-Z][^()]*?(?:Committee|Panel)(?: \([A-Z]{2,10}\))?)"
    r"(?= \(the Committee\)|[.,]| \()"
)
_AMENDMENT = re.compile(r"\bamendment to the notice of meeting\b")


def _committee(title: _Unit | None, sentences: list[_Unit]) -> Fact[str] | None:
    if title is not None:
        for segment in title.text.split(";"):
            name = segment.strip()
            if ("Committee" in name or "Panel" in name) and name != "Advisory Committee":
                return Fact(name, title.evidence)
    for unit in sentences:
        match = _ABSTRACT_COMMITTEE.search(unit.text)
        if match:
            return Fact(match["name"], unit.evidence)
    return None


def _topic(title: _Unit | None) -> Fact[str] | None:
    match = _TOPIC.search(title.text) if title is not None else None
    return Fact(match["topic"], title.evidence) if match and title is not None else None


def _amendment(title: _Unit | None, sentences: list[_Unit]) -> Evidence | None:
    if title is not None and "Amendment of Notice" in title.text:
        return title.evidence
    return next((u.evidence for u in sentences if _AMENDMENT.search(u.text)), None)


# --- Drugs, application numbers, and companies -------------------------------------------------------

_KIND = r"sNDA|sBLA|ANDA|NDA|BLA|sPMA|PMA|De Novo|510\(k\)"
_LONG_KIND = (
    r"(?i:(?:supplemental\s+)?(?:new\s+drug|biologics\s+license|abbreviated\s+new\s+drug|premarket\s+approval)"
    r"\s+application)"
)
_APPLICATION = re.compile(
    rf"(?:{_LONG_KIND}\s*\(\s*(?P<kind>{_KIND})\s*\)|(?<!\w)(?P<bare>{_KIND}))\s*(?:(?i:no)\.\s*)?"
    r"(?P<number>[A-Z]{0,3}\d{5,7}(?:/S-?\d{1,4})?)(?![\w/])"
)
_SUFFIX_WORD = (
    r"(?:Inc|LLC|L\.L\.C|Ltd|Limited|Corp|Corporation|Co|Company|plc|PLC|AG|GmbH|S\.A|SA|N\.V|NV|A/S|B\.V"
    r"|L\.P|LP|Pty|K\.K|S\.p\.A)"
)
_SUFFIX = rf"{_SUFFIX_WORD}\.?"
_ENDS_IN_SUFFIX = re.compile(rf"\b{_SUFFIX_WORD}\.$")
_NAME_WORD = r"[A-Z0-9][\w&'.\-/]*"
# "and" never joins words of one name: "Lexicon and Sanofi, Inc." is two companies, read as none.
_COMPANY = rf"{_NAME_WORD}(?:\s+(?:{_NAME_WORD}|&|of|de|du|la))*?"
_SPONSOR = rf"{_COMPANY}(?:,?\s+{_SUFFIX})?"
_BY = r"(?:[Ff]rom|[Ss]ubmitted\s+by|[Ss]ponsored\s+by)"
_SPONSOR_END = r"(?=,|\.(?:\s|$)|;|\s*$|\s+for\b)"
# A product name runs to the company, the next clause, the next application, or the sentence's end.
_PRODUCT_END = (
    rf"(?=,?\s+{_BY}\s|,\s+(?:for|to|in|which)\s|,?\s+and\s+(?:{_KIND}|{_LONG_KIND})"
    r"|\s+to\s+treat\b|,?\s+in\s+combination\b|\.(?:\s|$)|;|$)"
)
# "for patients with ...", "for pediatric use": who or what the drug is for, not the drug.
_NOT_A_PRODUCT = r"(?:the|a|an|use|treatment)\s|(?i:patients|adults|children|pediatric|people|use)\b"
_AFTER_APPLICATION = re.compile(
    rf",?\s+(?:{_BY}\s+(?P<sponsor_before>{_SPONSOR}),?\s+)?for\s+"
    rf"(?!{_NOT_A_PRODUCT})(?P<product>[^;]{{1,150}}?){_PRODUCT_END}"
    rf"(?:,?\s+{_BY}\s+(?P<sponsor_after>{_SPONSOR}){_SPONSOR_END})?"
)
# A device topic names the company, with its legal suffix, and then the device: "GRAIL, Inc. Galleri".
_COMPANY_AND_DEVICE = re.compile(rf"(?P<sponsor>{_COMPANY},?\s+{_SUFFIX})\s+(?P<product>[A-Z0-9][^;]*)")
# Outside the title, only a sentence about the meeting's own agenda names its subjects.
_AGENDA = re.compile(r"\bwill\s+(?:also\s+)?(?:discuss|consider)\b")


def _sponsor_name(name: str | None) -> str | None:
    """Drop a sentence's full stop from a company name, keeping one that ends a suffix ("Inc.")."""
    if name is None:
        return None
    name = name.strip()
    if name.endswith(".") and not _ENDS_IN_SUFFIX.search(name):
        name = name[:-1]
    return name or None


def _subjects_in(text: str, evidence: Evidence, is_topic: bool) -> list[Subject]:
    subjects: list[Subject] = []
    for match in _APPLICATION.finditer(text):
        application = Application(match["kind"] or match["bare"], match["number"])
        after = _AFTER_APPLICATION.match(text, match.end())
        product = sponsor = None
        if after is not None:
            product = after["product"].strip()
            sponsor = _sponsor_name(after["sponsor_before"] or after["sponsor_after"])
        subjects.append(_subject(product, application, sponsor, evidence))
    if not subjects and is_topic:
        device = _COMPANY_AND_DEVICE.fullmatch(text)
        if device is not None:
            subjects.append(_subject(device["product"].strip(), None, device["sponsor"], evidence))
    return subjects


def _subject(
    product: str | None, application: Application | None, sponsor: str | None, evidence: Evidence
) -> Subject:
    return Subject(
        product=Fact(product, evidence) if product else None,
        application=Fact(application, evidence) if application else None,
        sponsor=Fact(sponsor, evidence) if sponsor else None,
    )


def _subjects(topic: Fact[str] | None, sentences: list[_Unit]) -> tuple[Subject, ...]:
    found: list[Subject] = []
    if topic is not None:
        found += _subjects_in(topic.value, topic.evidence, is_topic=True)
    for unit in sentences:
        if _AGENDA.search(unit.text):
            found += _subjects_in(unit.text, unit.evidence, is_topic=False)
    kept: dict[object, Subject] = {}
    for subject in found:
        key = (
            subject.application.value
            if subject.application
            else (subject.product and subject.product.value, subject.sponsor and subject.sponsor.value)
        )
        kept.setdefault(key, subject)
    return tuple(kept.values())


# --- One notice --------------------------------------------------------------------------------------


def _fields(content: bytes) -> dict[str, Any]:
    try:
        parsed: object = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise NoticeError("the stored copy is not a Federal Register JSON document") from error
    if not isinstance(parsed, dict):
        raise NoticeError("the stored copy is not a Federal Register JSON document")
    fields = cast(dict[str, Any], parsed)
    if not isinstance(fields.get("document_number"), str) or not isinstance(
        fields.get("publication_date"), str
    ):
        raise NoticeError("the stored copy has no document number or publication date")
    return fields


def notice_text(fields: Mapping[str, Any]) -> tuple[str, list[tuple[str, str, int]]]:
    """Return the notice text, and each field in it with its starting offset."""
    parts: list[tuple[str, str, int]] = []
    offset = 0
    for name in TEXT_FIELDS:
        value = fields.get(name)
        if isinstance(value, str) and value.strip():
            parts.append((name, value, offset))
            offset += len(value) + 1
    return "\n".join(value for _, value, _ in parts), parts


def read_notice(content: bytes) -> MeetingNotice:
    """Read one stored Federal Register notice (the collector's JSON copy) into facts."""
    fields = _fields(content)
    text, parts = notice_text(fields)
    title: _Unit | None = None
    sentences: list[_Unit] = []
    for name, value, offset in parts:
        if name == "title":
            title = _Unit(value, Evidence(name, offset, offset + len(value), value))
        else:
            sentences += _sentences(name, value, offset)
    try:
        published = date.fromisoformat(fields["publication_date"])
    except ValueError as error:
        raise NoticeError("the stored copy's publication date is not a date") from error
    topic = _topic(title)
    return MeetingNotice(
        document_number=fields["document_number"],
        publication_date=published,
        text=text,
        committee=_committee(title, sentences),
        topic=topic,
        meeting=_meeting(sentences),
        subjects=_subjects(topic, sentences),
        amendment=_amendment(title, sentences),
    )


# --- Stored notices whose meeting falls in a window --------------------------------------------------


@dataclass(frozen=True)
class StoredNotice:
    """A stored source document and what was read from it."""

    document: SourceDocument
    notice: MeetingNotice


@dataclass
class NoticesInWindow:
    """Stored notices for a window, in four lists.

    `matching` holds those whose meeting overlaps the window, `undated` those with no readable
    meeting date, `superseded` those an amendment published on the same day or later replaces
    (same committee and topic), and `unreadable` the source keys of copies that could not be read.
    """

    matching: list[StoredNotice] = field(default_factory=list[StoredNotice])
    undated: list[StoredNotice] = field(default_factory=list[StoredNotice])
    superseded: list[StoredNotice] = field(default_factory=list[StoredNotice])
    unreadable: list[str] = field(default_factory=list[str])


def _meeting_key(notice: MeetingNotice) -> tuple[str | None, str | None]:
    """Return the committee and topic, which an amendment repeats from the notice it amends."""
    return (
        notice.committee.value if notice.committee else None,
        notice.topic.value if notice.topic else None,
    )


def _is_superseded(stored: StoredNotice, every: list[StoredNotice]) -> bool:
    """Whether an amendment, published on the same day or later, replaces this notice.

    An amendment changes the meeting it amends, so the older notice's days may be wrong. When two
    notices could each replace the other, both are set apart: an empty answer is safe, a stale one
    is not.
    """
    key = _meeting_key(stored.notice)
    return any(
        other is not stored
        and other.notice.amendment is not None
        and _meeting_key(other.notice) == key
        and other.document.published_at >= stored.document.published_at
        for other in every
    )


def notices_for(
    session: Session, store: SnapshotStore, window_start: date, window_end: date
) -> NoticesInWindow:
    """Return the stored notices whose meeting days overlap `window_start` to `window_end`.

    A notice is published before its meeting, so notices published after the window ends are not
    read. Notices with no readable meeting date, and notices a later amendment replaces, are
    listed apart, never matched by guess.
    """
    if window_end < window_start:
        raise ValueError("the window must not end before it starts")
    table = TABLES[SourceDocument]
    query = (
        select(table)
        .where(
            table.c.source_key.startswith(SOURCE_PREFIX, autoescape=True),
            table.c.published_at < new_york_midnight(window_end + timedelta(days=1)),
        )
        .order_by(table.c.published_at, table.c.source_key)
    )
    found = NoticesInWindow()
    readable: list[StoredNotice] = []
    for row in session.execute(query).mappings():
        document = SourceDocument.model_validate(dict(row))
        try:
            notice = read_notice(store.get(document.snapshot_key))
        except (NoticeError, SnapshotStoreError, httpx.HTTPError) as error:
            found.unreadable.append(document.source_key)
            log_event(
                _log, "fedreg_notice_unreadable", source_key=document.source_key, error=type(error).__name__
            )
            continue
        readable.append(StoredNotice(document, notice))
    for stored in readable:
        meeting = stored.notice.meeting
        if _is_superseded(stored, readable):
            found.superseded.append(stored)
        elif meeting is None:
            found.undated.append(stored)
        elif meeting.value.start <= window_end and meeting.value.end >= window_start:
            found.matching.append(stored)
    return found
