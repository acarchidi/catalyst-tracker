"""The weekly digest: what changed in the published data, built by plain code (ADR 0047).

No model writes any of it. Each issue is computed from published catalysts as the read API shows
them (`catalyst.api.read.published`), so nothing from the review queue can enter it. An issue
holds only what a free caller may see (spec: Offering): the next 30 days of the calendar, the
summary fields, every source with its quote, and the latest date change. What lies outside the
free calendar is a count, never a record.

The paid issue (ADR 0049) is the same code with the paid shape: the full calendar, every date
move of the week, and each moved catalyst's date history. It is for paid subscribers only and is
never printed anywhere public.

An issue is dated on a Monday and reports the seven days before it in New York time. The date is
never chosen to follow a market event (spec: Compliance and legal; ADR 0020). The same records
give the same issue, so building one again changes nothing.

Nothing here sends or posts. The newsletter sender, the website, and any feed read the digest
this module builds.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from html import escape
from itertools import chain, groupby
from re import compile as compile_pattern
from typing import Any, Literal, Self, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import Field, JsonValue, model_validator

from catalyst.api.access import NOTICE, Access, calendar_window, shape
from catalyst.api.read import DATE_FIELDS, OPEN_STATUSES
from catalyst.api.views import CatalystDetail, CatalystSummary, ChangeView, Shaped, SourceView, View
from catalyst.schemas import DateConfidence

NEW_YORK = ZoneInfo("America/New_York")
WEEK = timedelta(days=7)
MONDAY = 0
DEFAULT_CALENDAR_LIMIT = 25
PAID_CALENDAR_LIMIT = 200
# An address may become a link only when it is a plain https address with nothing that could
# end an HTML attribute or start another one.
_PLAIN_HTTPS = compile_pattern(r"https://[^\s\"'<>`\\]+")
_CONFIDENCE_LABELS = {
    DateConfidence.COMPANY_CONFIRMED: "company-confirmed",
    DateConfidence.REGULATOR_SCHEDULED: "regulator-scheduled",
    DateConfidence.REGISTRY_ESTIMATE: "registry estimate",
    DateConfidence.INFERRED: "inferred",
}


class MoveKind(StrEnum):
    """How a date window changed (spec: Date precision)."""

    LATER = "later"  # both ends moved later
    EARLIER = "earlier"  # both ends moved earlier
    NARROWED = "narrowed"  # the new window lies inside the old one
    WIDENED = "widened"  # the old window lies inside the new one
    RESTATED = "restated"  # the same days, written another way


_MOVE_SENTENCES = {
    MoveKind.LATER: "The window moved later.",
    MoveKind.EARLIER: "The window moved earlier.",
    MoveKind.NARROWED: "The window narrowed.",
    MoveKind.WIDENED: "The window widened.",
    MoveKind.RESTATED: "The date was written another way; the days are the same.",
}


class WindowBefore(View):
    """The date window as it stood before the move."""

    start: date = Field(description="Earliest day, before the move.")
    end: date = Field(description="Latest day, before the move.")
    raw_text: str | None = Field(description="The date as the earlier source wrote it, if that changed.")


class DateMove(View):
    """One catalyst whose date window moved in the reported week."""

    catalyst: Shaped = Field(description="The catalyst as it now stands, in the issue's shape.")
    kind: MoveKind = Field(description="How the window changed.")
    before: WindowBefore = Field(description="The window before the move.")
    learned_at: datetime = Field(description="When we learned of the move.")
    source: SourceView = Field(description="The document and quote that moved the date.")


class DigestCounts(View):
    """How many records an issue lists, and how many it leaves out by the free-tier rule."""

    moved: int = Field(description="Date moves listed.")
    new: int = Field(description="Newly published catalysts listed.")
    upcoming: int = Field(description="Other catalysts listed from the issue's calendar.")
    upcoming_not_listed: int = Field(description="Catalysts on the issue's calendar past the list limit.")
    moved_outside_free_calendar: int = Field(description="Free issue: date moves past the next 30 days.")
    new_outside_free_calendar: int = Field(description="Free issue: new catalysts past the next 30 days.")


class Digest(View):
    """One weekly issue."""

    notice: str = Field(description="Nothing here is financial advice.")
    access: Literal["free", "paid"] = Field(description="Who the issue is for; it holds only that shape.")
    issue_date: date = Field(description="The Monday the issue is dated.")
    week_from: date = Field(description="First day of the reported week (a Monday, New York time).")
    week_to: date = Field(description="Last day of the reported week (a Sunday, New York time).")
    calendar_to: date | None = Field(description="Last day of the calendar the issue lists; none if open.")
    moved: list[DateMove] = Field(description="Catalysts whose date window moved in the week.")
    new: list[Shaped] = Field(description="Catalysts first published in the week.")
    upcoming: list[Shaped] = Field(description="The rest of the calendar, earliest first.")
    counts: DigestCounts = Field(description="Totals, including what the free calendar leaves out.")

    @model_validator(mode="after")
    def _free_issue_holds_only_the_free_shape(self) -> Self:
        """Refuse a free issue that holds the paid shape, however it was put together (ADR 0025)."""
        shown = [*self.new, *self.upcoming, *(move.catalyst for move in self.moved)]
        if self.access == Access.FREE.value and any(
            type(catalyst) is not CatalystSummary for catalyst in shown
        ):
            raise ValueError("a free issue may hold only the free shape of a catalyst")
        return self


def details_from_export(
    export: Mapping[str, object], *, issue_date: date | None = None
) -> list[CatalystDetail]:
    """Read the published catalysts out of the private export (`catalyst export-catalysts`; ADR 0034).

    The export lists them exactly as the read API shows a paid caller, plus registry values on
    each trial that no quote states (ADR 0039). Those extras are dropped; anything else that is
    not a published catalyst's shape refuses the whole file. Its held claims are never read.

    With `issue_date`, the export must have been made after that issue's week ended. An older
    one lacks the last days' changes and would give a short issue without saying so.
    """
    if issue_date is not None:
        _check_export_time(export.get("generated_at"), week_of(issue_date)[1])
    items = export.get("catalysts")
    if not isinstance(items, list):
        raise ValueError("the file is not an export: it has no list of catalysts")
    details: list[CatalystDetail] = []
    for item in cast(list[object], items):
        try:
            if not isinstance(item, Mapping):
                raise TypeError("a catalyst is not an object")
            record = dict(cast(Mapping[str, Any], item))
            trials = cast(list[Mapping[str, Any]], record["trials"])
            record["trials"] = [
                {key: value for key, value in trial.items() if key != "registry"} for trial in trials
            ]
            detail = CatalystDetail.model_validate(record)
            if any(
                change.learned_at.tzinfo is None for change in (*detail.history, *detail.latest_date_change)
            ):
                raise ValueError("a time has no time zone")
            details.append(detail)
        except (TypeError, ValueError, AttributeError, KeyError) as error:
            kind = type(error).__name__
            raise ValueError(
                f"the file is not an export: a catalyst lacks the published shape ({kind})"
            ) from error
    return details


def _check_export_time(generated_at: object, week_end: datetime) -> None:
    """Refuse an export with no readable time, or one made before the issue's week ended."""
    try:
        made = datetime.fromisoformat(generated_at) if isinstance(generated_at, str) else None
    except ValueError:
        made = None
    if made is None or made.tzinfo is None:
        raise ValueError("the file is not an export: it does not say when it was made")
    if made < week_end:
        raise ValueError(
            f"the export was made {made.astimezone(UTC).isoformat()}, before the week of this issue ended "
            f"({week_end.isoformat()}); make a new export"
        )


def week_of(issue_date: date) -> tuple[datetime, datetime]:
    """Return the start and end of the week an issue reports: the seven days before its Monday.

    Both are midnights in New York, given in UTC; the end is not part of the week.
    """
    if issue_date.weekday() != MONDAY:
        raise ValueError(f"an issue is dated on a Monday; {issue_date.isoformat()} is not one")
    end = datetime.combine(issue_date, time.min, NEW_YORK)
    start = datetime.combine(issue_date - WEEK, time.min, NEW_YORK)
    return start.astimezone(UTC), end.astimezone(UTC)


def latest_issue_date(now: datetime) -> date:
    """Return the Monday on or before `now` in New York time."""
    today = now.astimezone(NEW_YORK).date()
    return today - timedelta(days=today.weekday())


def move_kind(old: tuple[date, date], new: tuple[date, date]) -> MoveKind:
    """Say how a window changed from `old` to `new`, each given as (first day, last day)."""
    if old == new:
        return MoveKind.RESTATED
    if new[0] >= old[0] and new[1] <= old[1]:
        return MoveKind.NARROWED
    if new[0] <= old[0] and new[1] >= old[1]:
        return MoveKind.WIDENED
    return MoveKind.LATER if new[0] > old[0] else MoveKind.EARLIER


def build_digest(
    details: Sequence[CatalystDetail],
    issue_date: date,
    *,
    access: Access = Access.FREE,
    calendar_limit: int | None = None,
) -> Digest:
    """Build the issue dated `issue_date` (a Monday) from every published catalyst.

    A catalyst is listed once: as a date move, else as new, else on the calendar. Only open
    catalysts on the caller's calendar are listed: the next 30 days for the free issue, and
    everything from the issue date on for the paid one. The rest are counted, never named.
    The calendar list is cut at `calendar_limit` (25 free, 200 paid, unless given).
    """
    if calendar_limit is None:
        calendar_limit = DEFAULT_CALENDAR_LIMIT if access is Access.FREE else PAID_CALENDAR_LIMIT
    if calendar_limit < 0:
        raise ValueError("calendar_limit must be 0 or more")
    week = week_of(issue_date)
    window = calendar_window(access, issue_date, None, None)
    first, last = window if window else (issue_date, issue_date)
    moved: list[DateMove] = []
    new: list[Shaped] = []
    calendar: list[Shaped] = []
    outside = {"moved": 0, "new": 0}
    for detail in sorted(details, key=_order):
        if detail.status not in OPEN_STATUSES:
            continue
        listed = detail.window.end >= first and (last is None or detail.window.start <= last)
        kind = _news(detail, week)
        if not listed:
            # Only what lies past the calendar's last day is "outside"; a window already past is neither.
            if kind in outside and last is not None and detail.window.start > last:
                outside[kind] += 1
        elif kind == "moved":
            moved.append(_move(detail, access))
        elif kind == "new":
            new.append(shape(detail, access))
        else:
            calendar.append(shape(detail, access))
    upcoming = calendar[:calendar_limit]
    return Digest(
        notice=NOTICE,
        access=access.value,
        issue_date=issue_date,
        week_from=issue_date - WEEK,
        week_to=issue_date - timedelta(days=1),
        calendar_to=last,
        moved=moved,
        new=new,
        upcoming=upcoming,
        counts=DigestCounts(
            moved=len(moved),
            new=len(new),
            upcoming=len(upcoming),
            upcoming_not_listed=len(calendar) - len(upcoming),
            moved_outside_free_calendar=outside["moved"],
            new_outside_free_calendar=outside["new"],
        ),
    )


def _order(detail: CatalystDetail) -> tuple[date, str, str]:
    return (detail.window.start, detail.company.name, str(detail.id))


def _news(detail: CatalystDetail, week: tuple[datetime, datetime]) -> str:
    """Return "moved", "new", or "calendar": what the week brought for this catalyst."""
    start, end = week
    move = detail.latest_date_change
    if move and start <= max(change.learned_at for change in move) < end:
        return "moved"
    if detail.history and start <= min(change.learned_at for change in detail.history) < end:
        return "new"
    return "calendar"


def _move(detail: CatalystDetail, access: Access) -> DateMove:
    """Describe the latest date move; an end of the window the move left alone keeps its value."""
    move = detail.latest_date_change
    old = {change.field: change.old_value for change in move if change.field in DATE_FIELDS}
    before_start = _as_date(old.get("window_start")) or detail.window.start
    before_end = _as_date(old.get("window_end")) or detail.window.end
    raw = old.get("window_raw_text")
    return DateMove(
        catalyst=shape(detail, access),
        kind=move_kind((before_start, before_end), (detail.window.start, detail.window.end)),
        before=WindowBefore(
            start=before_start, end=before_end, raw_text=raw if isinstance(raw, str) and raw else None
        ),
        learned_at=max(change.learned_at for change in move),
        source=move[0].source,
    )


def _as_date(value: JsonValue | None) -> date | None:
    """Read a change row's date (`YYYY-MM-DD`, ADR 0025); anything else is no date."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


# --- HTML ---


def render_html(digest: Digest) -> str:
    """Return the issue as an HTML fragment with no styles, scripts, or attributes but link addresses.

    Every word that comes from a record is escaped, quotes included: source text is untrusted.
    The only links are source addresses that are plain https addresses.
    """
    week = f"{_day(digest.week_from, year=False)} to {_day(digest.week_to)}"
    paid = digest.access == Access.PAID.value
    calendar = f" through {_day(digest.calendar_to)}" if digest.calendar_to else ""
    where = "the calendar" if paid else "the next 30 days of the calendar"
    parts = [
        f"<h1>Catalyst digest: week of {week}</h1>",
        *(["<p>Full issue for paid subscribers.</p>"] if paid else []),
        f"<p>{escape(digest.notice)}</p>",
        f"<h2>Dates that moved ({digest.counts.moved})</h2>",
        _list([_move_item(move) for move in digest.moved]) or f"<p>No date on {where} moved this week.</p>",
        f"<h2>New on the calendar ({digest.counts.new})</h2>",
        _list([_item(catalyst) for catalyst in digest.new])
        or f"<p>No catalyst was added to {where} this week.</p>",
        f"<h2>Also on the calendar{calendar} ({digest.counts.upcoming})</h2>",
        _list([_item(catalyst) for catalyst in digest.upcoming]) or "<p>Nothing else is listed.</p>",
    ]
    if digest.counts.upcoming_not_listed:
        rest = "on the calendar" if paid else "in the next 30 days"
        parts.append(f"<p>And {digest.counts.upcoming_not_listed} more {rest}.</p>")
    moved, added = digest.counts.moved_outside_free_calendar, digest.counts.new_outside_free_calendar
    if moved or added:
        parts.append(
            f"<p>Outside the next 30 days: {moved} date{'' if moved == 1 else 's'} moved and "
            f"{added} catalyst{' was' if added == 1 else 's were'} added.</p>"
        )
    parts.append(f"<p>{escape(digest.notice)}</p>")
    return "\n".join(parts) + "\n"


def render_page(digest: Digest) -> str:
    """Return the issue as a complete web page, for the owner to open, copy, and paste (ADR 0049)."""
    week = f"{_day(digest.week_from, year=False)} to {_day(digest.week_to)}"
    title = escape(f"Catalyst digest: week of {week} ({digest.access} issue)")
    head = f'<head>\n<meta charset="utf-8">\n<title>{title}</title>\n</head>'
    return f'<!doctype html>\n<html lang="en">\n{head}\n<body>\n{render_html(digest)}</body>\n</html>\n'


def _list(items: list[str]) -> str:
    return "<ul>\n" + "\n".join(items) + "\n</ul>" if items else ""


def _day(day: date, *, year: bool = True) -> str:
    return f"{day.day} {day:%B} {day.year}" if year else f"{day.day} {day:%B}"


def _span(start: date, end: date) -> str:
    return _day(start) if start == end else f"{_day(start)} to {_day(end)}"


def _heading(catalyst: CatalystSummary) -> str:
    ticker = f" ({catalyst.company.ticker})" if catalyst.company.ticker else ""
    return f"<strong>{escape(catalyst.company.name + ticker)}</strong>: {escape(catalyst.title)}"


def _label(catalyst: CatalystSummary) -> str:
    return f"Date label: {_CONFIDENCE_LABELS[catalyst.date_confidence]}."


def _is_plain_https(url: str) -> bool:
    """Say whether an address may become a link: plain https, a host, and nothing that could mislead.

    A sign-in part ("https://sec.gov@other.example/") and characters that do not print, such as
    ones that reverse the reading order, are refused; so is anything `urlsplit` cannot read.
    """
    if not _PLAIN_HTTPS.fullmatch(url) or not url.isprintable():
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    return parts.scheme == "https" and bool(host) and "@" not in parts.netloc


def _source_lines(source: SourceView) -> list[str]:
    label = escape(
        f"{source.publisher}, {source.document_type}, published {_day(source.published_at.date())}"
    )
    if _is_plain_https(source.url):
        cited = f'<a href="{escape(source.url, quote=True)}">{label}</a>'
    else:
        cited = f"{label}, {escape(source.url)}"
    return [f"<blockquote>{escape(source.quote)}</blockquote>", f"<p>Source: {cited}</p>"]


def _move_item(move: DateMove) -> str:
    window = move.catalyst.window
    before = _span(move.before.start, move.before.end)
    if move.before.raw_text:
        before = f"{move.before.raw_text} ({before})"
    lines = [
        _heading(move.catalyst),
        "<p>"
        + escape(
            f"Now: {window.raw_text} ({_span(window.start, window.end)}). Before: {before}. "
            f"{_MOVE_SENTENCES[move.kind]} {_label(move.catalyst)}"
        )
        + "</p>",
        *_source_lines(move.source),
        *_history_lines(move.catalyst),
    ]
    return "<li>" + "\n".join(lines) + "</li>"


def _history_lines(catalyst: CatalystSummary) -> list[str]:
    """Return the catalyst's earlier date moves, oldest first; only the paid shape has them."""
    if not isinstance(catalyst, CatalystDetail):
        return []
    state: dict[str, JsonValue] = {}
    moves: list[str] = []
    rows = [change for change in catalyst.history if change.field in DATE_FIELDS]
    for (_, first_value), group in groupby(rows, key=_move_key):
        changes = list(group)
        before = _window_text(state)
        state.update({change.field: change.new_value for change in changes})
        if not first_value:
            learned = max(change.learned_at for change in changes).astimezone(NEW_YORK).date()
            moves.append(f"<li>{escape(f'{_day(learned)}: {before} to {_window_text(state)}')}</li>")
    return ["<p>Date history:</p>", _list(moves)] if moves else []


def _move_key(change: ChangeView) -> tuple[str, bool]:
    """Rows of one move cite one claim; a first value has nothing before it (ADR 0025, decision 2)."""
    return (str(change.source.claim_id), change.old_value is None)


def _window_text(state: dict[str, JsonValue]) -> str:
    start, end = _as_date(state.get("window_start")), _as_date(state.get("window_end"))
    span = _span(start, end) if start and end else ""
    raw = state.get("window_raw_text")
    if isinstance(raw, str) and raw:
        return f"{raw} ({span})" if span else raw
    return span or "not stated"


def _item(catalyst: CatalystSummary) -> str:
    window = catalyst.window
    dated = [source for source in catalyst.sources if DATE_FIELDS.intersection(source.fields)]
    source = next(chain(dated, catalyst.sources), None)
    lines = [
        _heading(catalyst),
        "<p>"
        + escape(f"When: {window.raw_text} ({_span(window.start, window.end)}). {_label(catalyst)}")
        + "</p>",
        *(_source_lines(source) if source else []),
        *_history_lines(catalyst),
    ]
    return "<li>" + "\n".join(lines) + "</li>"
