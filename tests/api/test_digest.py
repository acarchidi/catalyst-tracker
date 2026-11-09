"""The weekly digest (ADR 0047), judged against the spec's free tier and its Provenance rules.

A digest is built by plain code from published catalysts as the read API shows them. It holds
only what a free caller may see: the next 30 days of the calendar, the summary fields, every
source, and the latest date change. Everything outside that is a count, never a record.
"""

import json
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy.orm import Session, sessionmaker

from catalyst import cli
from catalyst.api.access import NOTICE, Access, calendar_window
from catalyst.api.views import (
    CatalystDetail,
    CatalystSummary,
    ChangeView,
    CompanyView,
    SourceView,
    WindowView,
)
from catalyst.outreach.digest import (
    MoveKind,
    build_digest,
    latest_issue_date,
    move_kind,
    render_html,
    week_of,
)
from catalyst.schemas import CatalystStatus, CatalystType, DateConfidence, DatePrecision, SourceTier

from .conftest import Seed

ISSUE = date(2026, 10, 5)  # a Monday; the week it reports is 28 September to 4 October
IN_WEEK = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
BEFORE_WEEK = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
SEC_URL = "https://www.sec.gov/Archives/edgar/data/1/ex99-1.htm"
dates = st.dates(min_value=date(2020, 1, 1), max_value=date(2035, 12, 31))


def _source(quote: str = "topline data in the fourth quarter of 2026", url: str = SEC_URL) -> SourceView:
    return SourceView(
        claim_id=uuid4(),
        url=url,
        publisher="SEC EDGAR",
        document_type="8-K EX-99.1",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        published_at=BEFORE_WEEK,
        retrieved_at=BEFORE_WEEK,
        quote=quote,
        fields=["window_start", "window_end", "window_raw_text"],
    )


def _change(field: str, old: str | None, new: str, learned: datetime, source: SourceView) -> ChangeView:
    return ChangeView(
        field=field, old_value=old, new_value=new, learned_at=learned, is_correction=False, source=source
    )


def _detail(
    *,
    start: date = date(2026, 10, 20),
    end: date | None = None,
    raw: str = "October 20, 2026",
    status: CatalystStatus = CatalystStatus.UPCOMING,
    first_learned: datetime = BEFORE_WEEK,
    moved_from: tuple[date, date, str] | None = None,
    moved_at: datetime = IN_WEEK,
    name: str = "Example Bio, Inc.",
    title: str = "Topline readout: exampumab",
    quote: str = "topline data in the fourth quarter of 2026",
    url: str = SEC_URL,
) -> CatalystDetail:
    """Return one published catalyst; `moved_from` adds a date move from that earlier window."""
    last = end or start
    source = _source(quote, url)
    history = [_change("window_start", None, start.isoformat(), first_learned, source)]
    move: list[ChangeView] = []
    if moved_from:
        old_start, old_end, old_raw = moved_from
        move = [
            _change("window_start", old_start.isoformat(), start.isoformat(), moved_at, source),
            _change("window_end", old_end.isoformat(), last.isoformat(), moved_at, source),
            _change("window_raw_text", old_raw, raw, moved_at, source),
        ]
    return CatalystDetail(
        id=uuid4(),
        catalyst_type=CatalystType.TOPLINE_READOUT,
        title=title,
        company=CompanyView(id=uuid4(), name=name, cik=1, ticker="EXB"),
        indication=None,
        drugs=[],
        trials=[],
        window=WindowView(start=start, end=last, precision=DatePrecision.DAY, raw_text=raw),
        date_confidence=DateConfidence.COMPANY_CONFIRMED,
        status=status,
        latest_date_change=move,
        sources=[source],
        history=[*history, *move],
    )


Q3 = (date(2026, 7, 1), date(2026, 9, 30), "3Q26")


def _slipped(
    *,
    start: date = date(2026, 10, 1),
    end: date = date(2026, 12, 31),
    raw: str = "4Q26",
    status: CatalystStatus = CatalystStatus.SLIPPED,
    moved_at: datetime = IN_WEEK,
    name: str = "Example Bio, Inc.",
    title: str = "Topline readout: exampumab",
    quote: str = "topline data now expected in the fourth quarter of 2026",
    url: str = SEC_URL,
) -> CatalystDetail:
    """Return a readout moved from 3Q26 to 4Q26 inside the reported week."""
    return _detail(
        start=start,
        end=end,
        raw=raw,
        status=status,
        moved_from=Q3,
        moved_at=moved_at,
        name=name,
        title=title,
        quote=quote,
        url=url,
    )


# --- The week an issue reports ---


def test_an_issue_reports_the_seven_days_before_its_monday_in_new_york_time() -> None:
    start, end = week_of(ISSUE)
    assert start == datetime(2026, 9, 28, 4, 0, tzinfo=UTC)  # Monday 00:00 in New York (UTC-4)
    assert end == datetime(2026, 10, 5, 4, 0, tzinfo=UTC)


def test_the_week_follows_the_clock_change() -> None:
    start, end = week_of(date(2026, 11, 2))  # clocks went back on Sunday 1 November 2026
    assert start == datetime(2026, 10, 26, 4, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 2, 5, 0, tzinfo=UTC)


@pytest.mark.parametrize("day", [date(2026, 10, 4), date(2026, 10, 6), date(2026, 10, 10)])
def test_an_issue_date_that_is_not_a_monday_is_refused(day: date) -> None:
    # The digest keeps a fixed weekly schedule and is never built early for a market event (ADR 0020).
    with pytest.raises(ValueError, match="Monday"):
        week_of(day)
    with pytest.raises(ValueError, match="Monday"):
        build_digest([], day)


def test_the_latest_issue_date_is_the_monday_on_or_before_now_in_new_york() -> None:
    assert latest_issue_date(datetime(2026, 10, 5, 3, 59, tzinfo=UTC)) == date(2026, 9, 28)  # still Sunday
    assert latest_issue_date(datetime(2026, 10, 5, 4, 0, tzinfo=UTC)) == ISSUE
    assert latest_issue_date(datetime(2026, 10, 11, 23, 0, tzinfo=UTC)) == ISSUE


# --- How a date window moved (spec: Date precision, "opens, narrows, or moves") ---


@pytest.mark.parametrize(
    ("old", "new", "kind"),
    [
        ((date(2026, 7, 1), date(2026, 9, 30)), (date(2026, 10, 1), date(2026, 12, 31)), MoveKind.LATER),
        ((date(2026, 10, 1), date(2026, 12, 31)), (date(2026, 7, 1), date(2026, 9, 30)), MoveKind.EARLIER),
        ((date(2026, 7, 1), date(2026, 12, 31)), (date(2026, 10, 1), date(2026, 12, 31)), MoveKind.NARROWED),
        ((date(2026, 7, 1), date(2026, 12, 31)), (date(2026, 11, 5), date(2026, 11, 5)), MoveKind.NARROWED),
        ((date(2026, 10, 1), date(2026, 12, 31)), (date(2026, 7, 1), date(2027, 6, 30)), MoveKind.WIDENED),
        ((date(2026, 7, 1), date(2026, 12, 31)), (date(2026, 7, 1), date(2026, 12, 31)), MoveKind.RESTATED),
        # A window that only grew at one end is widened: "later" and "earlier" need both ends to move.
        ((date(2026, 7, 1), date(2026, 9, 30)), (date(2026, 7, 1), date(2026, 12, 31)), MoveKind.WIDENED),
        ((date(2026, 7, 1), date(2026, 12, 31)), (date(2026, 4, 1), date(2026, 12, 31)), MoveKind.WIDENED),
    ],
)
def test_move_kinds(old: tuple[date, date], new: tuple[date, date], kind: MoveKind) -> None:
    assert move_kind(old, new) is kind


@given(a=dates, b=dates, c=dates, d=dates)
def test_a_move_read_backwards_is_its_opposite(a: date, b: date, c: date, d: date) -> None:
    old, new = (min(a, b), max(a, b)), (min(c, d), max(c, d))
    opposite = {
        MoveKind.LATER: MoveKind.EARLIER,
        MoveKind.EARLIER: MoveKind.LATER,
        MoveKind.NARROWED: MoveKind.WIDENED,
        MoveKind.WIDENED: MoveKind.NARROWED,
        MoveKind.RESTATED: MoveKind.RESTATED,
    }
    assert move_kind(new, old) is opposite[move_kind(old, new)]
    assert (move_kind(old, new) is MoveKind.RESTATED) == (old == new)


# --- What an issue lists ---


def test_a_date_moved_in_the_week_is_listed_with_its_earlier_window_and_its_source() -> None:
    slipped = _slipped()
    digest = build_digest([slipped], ISSUE)
    assert [m.catalyst.id for m in digest.moved] == [slipped.id]
    move = digest.moved[0]
    assert move.kind is MoveKind.LATER
    assert (move.before.start, move.before.end, move.before.raw_text) == Q3
    assert move.learned_at == IN_WEEK
    assert move.source == slipped.latest_date_change[0].source
    assert digest.new == []
    assert digest.upcoming == []  # listed once, under the heading that says the most


def test_a_move_keeps_the_unchanged_end_of_its_window() -> None:
    source = _source()
    moved = _detail(start=date(2026, 10, 1), end=date(2026, 12, 31), raw="4Q26").model_copy(
        update={
            "latest_date_change": [_change("window_start", "2026-07-01", "2026-10-01", IN_WEEK, source)],
        }
    )
    move = build_digest([moved], ISSUE).moved[0]
    assert (move.before.start, move.before.end, move.before.raw_text) == (
        date(2026, 7, 1),
        date(2026, 12, 31),
        None,
    )
    assert move.kind is MoveKind.NARROWED


def test_an_earlier_date_that_cannot_be_read_falls_back_to_the_window_now() -> None:
    source = _source()
    odd = _detail(start=date(2026, 10, 1), end=date(2026, 12, 31), raw="4Q26").model_copy(
        update={
            "latest_date_change": [_change("window_start", "third quarter", "2026-10-01", IN_WEEK, source)]
        }
    )
    move = build_digest([odd], ISSUE).moved[0]
    assert (move.before.start, move.before.end) == (date(2026, 10, 1), date(2026, 12, 31))
    assert move.kind is MoveKind.RESTATED  # never a guessed direction


@pytest.mark.parametrize(
    ("moved_at", "listed"),
    [
        (datetime(2026, 9, 28, 3, 59, tzinfo=UTC), False),  # Sunday 23:59 in New York, the week before
        (datetime(2026, 9, 28, 4, 0, tzinfo=UTC), True),
        (datetime(2026, 10, 5, 3, 59, tzinfo=UTC), True),
        (datetime(2026, 10, 5, 4, 0, tzinfo=UTC), False),  # Monday 00:00 in New York: the next issue
    ],
)
def test_a_move_belongs_to_the_week_we_learned_of_it(moved_at: datetime, listed: bool) -> None:
    digest = build_digest([_slipped(moved_at=moved_at)], ISSUE)
    assert bool(digest.moved) is listed
    assert len(digest.upcoming) == (0 if listed else 1)  # an older move is still on the calendar


def test_a_catalyst_first_published_in_the_week_is_new() -> None:
    fresh = _detail(first_learned=IN_WEEK)
    old = _detail(start=date(2026, 10, 21))
    digest = build_digest([old, fresh], ISSUE)
    assert [c.id for c in digest.new] == [fresh.id]
    assert [c.id for c in digest.upcoming] == [old.id]
    assert digest.moved == []


def test_the_calendar_is_the_next_30_days_in_date_order_and_only_open_catalysts() -> None:
    last_day = _detail(start=ISSUE + timedelta(days=30), name="Last Day Bio")
    past_horizon = _detail(start=ISSUE + timedelta(days=31))
    already_past = _detail(start=ISSUE - timedelta(days=1))
    resolved = _detail(status=CatalystStatus.RESOLVED)
    first = _detail(start=ISSUE, name="First Day Bio")
    overlapping = _detail(start=date(2026, 7, 1), end=date(2026, 12, 31), raw="2H26", name="Half Year Bio")
    digest = build_digest([last_day, past_horizon, already_past, resolved, first, overlapping], ISSUE)
    assert [c.id for c in digest.upcoming] == [overlapping.id, first.id, last_day.id]
    assert digest.calendar_to == ISSUE + timedelta(days=30)
    assert calendar_window(Access.FREE, ISSUE, None, None) == (
        ISSUE,
        digest.calendar_to,
    )  # the API's own rule
    assert (digest.week_from, digest.week_to) == (date(2026, 9, 28), date(2026, 10, 4))


def test_what_lies_outside_the_free_calendar_is_counted_never_listed() -> None:
    far_move = _slipped(start=date(2027, 4, 1), end=date(2027, 6, 30), raw="2Q27")
    far_new = _detail(start=date(2027, 3, 15), first_learned=IN_WEEK)
    resolved_move = _slipped(status=CatalystStatus.RESOLVED)
    digest = build_digest([far_move, far_new, resolved_move], ISSUE)
    assert (digest.moved, digest.new, digest.upcoming) == ([], [], [])
    assert digest.counts.moved_outside_free_calendar == 1  # a resolved catalyst is not on any calendar
    assert digest.counts.new_outside_free_calendar == 1
    dumped = json.dumps(digest.model_dump(mode="json"))
    for hidden in (far_move, far_new, resolved_move):
        assert str(hidden.id) not in dumped


def test_a_long_calendar_is_cut_to_the_limit_and_the_rest_counted() -> None:
    many = [_detail(start=ISSUE + timedelta(days=n)) for n in range(1, 8)]
    digest = build_digest(many, ISSUE, calendar_limit=5)
    assert [c.id for c in digest.upcoming] == [d.id for d in many[:5]]
    assert digest.counts.model_dump() == {
        "moved": 0,
        "new": 0,
        "upcoming": 5,
        "upcoming_not_listed": 2,
        "moved_outside_free_calendar": 0,
        "new_outside_free_calendar": 0,
    }
    with pytest.raises(ValueError, match="calendar_limit"):
        build_digest(many, ISSUE, calendar_limit=-1)


def test_the_digest_carries_only_the_free_shape_and_the_notice() -> None:
    digest = build_digest([_slipped(), _detail(first_learned=IN_WEEK), _detail()], ISSUE)
    assert digest.notice == NOTICE
    for shown in [*digest.new, *digest.upcoming, *(m.catalyst for m in digest.moved)]:
        assert type(shown) is CatalystSummary

    assert '"history"' not in json.dumps(digest.model_dump(mode="json"))


def test_the_same_records_in_any_order_give_the_same_digest() -> None:
    records = [_slipped(), _detail(first_learned=IN_WEEK), _detail(), _detail(start=date(2026, 10, 9))]
    first = build_digest(records, ISSUE)
    assert build_digest(list(reversed(records)), ISSUE) == first
    assert render_html(build_digest(list(reversed(records)), ISSUE)) == render_html(first)


# --- The HTML an issue is shown as ---


class _Tags(HTMLParser):
    """Collect every tag, every link address, and the visible text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.links: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.links += [value or "" for name, value in attrs if name == "href"]
        assert all(name == "href" for name, _ in attrs)  # no styles, scripts, or handlers

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _parsed(html: str) -> _Tags:
    parser = _Tags()
    parser.feed(html)
    parser.close()
    return parser


ALLOWED_TAGS = {"h1", "h2", "p", "ul", "li", "strong", "a", "blockquote"}


def test_the_html_shows_each_listed_catalyst_with_its_quote_and_deep_link() -> None:
    slipped = _slipped(quote="topline data now expected in the fourth quarter of 2026")
    fresh = _detail(first_learned=IN_WEEK, quote="data on October 20, 2026", name="Fresh Bio")
    page = _parsed(render_html(build_digest([slipped, fresh], ISSUE)))
    text = " ".join(" ".join(page.text).split())
    assert "week of 28 September to 4 October 2026" in text
    assert text.count(NOTICE) == 2  # above and below the records
    assert "Example Bio, Inc. (EXB)" in text
    assert "Now: 4Q26 (1 October 2026 to 31 December 2026)" in text
    assert "Before: 3Q26 (1 July 2026 to 30 September 2026)" in text
    assert "moved later" in text
    assert "company-confirmed" in text
    assert "topline data now expected in the fourth quarter of 2026" in text
    assert "October 20, 2026 (20 October 2026)" in text  # a one-day window is written once
    assert "data on October 20, 2026" in text
    assert page.links == [SEC_URL, SEC_URL]
    assert set(page.tags) <= ALLOWED_TAGS


def test_the_html_states_the_counts_outside_the_free_calendar_and_an_empty_week() -> None:
    far_move = _slipped(start=date(2027, 4, 1), end=date(2027, 6, 30), raw="2Q27")
    text = " ".join(" ".join(_parsed(render_html(build_digest([far_move], ISSUE))).text).split())
    assert "No date on the next 30 days of the calendar moved this week." in text
    assert "Outside the next 30 days: 1 date moved and 0 catalysts were added." in text
    assert "2Q27" not in text
    empty = " ".join(" ".join(_parsed(render_html(build_digest([], ISSUE))).text).split())
    assert "Outside the next 30 days" not in empty
    assert "And " not in empty
    many = [_detail(start=ISSUE + timedelta(days=n)) for n in range(1, 4)]
    cut = " ".join(" ".join(_parsed(render_html(build_digest(many, ISSUE, calendar_limit=1))).text).split())
    assert "And 2 more in the next 30 days." in cut


@given(
    hostile=st.text(min_size=1, max_size=200),
    where=st.sampled_from(["quote", "title", "name", "raw"]),
)
def test_source_text_can_never_add_a_tag_or_a_link(hostile: str, where: str) -> None:
    # Quotes are verbatim source text, so they are untrusted (spec: Prompt-injection defenses).
    words = {"quote": "a quote", "title": "A title", "name": "Example Bio, Inc.", "raw": "4Q26"}
    words[where] = hostile
    record = _slipped(quote=words["quote"], title=words["title"], name=words["name"], raw=words["raw"])
    page = _parsed(render_html(build_digest([record], ISSUE)))
    assert page.links == [SEC_URL]
    assert set(page.tags) <= ALLOWED_TAGS
    assert page.tags.count("a") == 1


@pytest.mark.parametrize(
    "hostile",
    [
        '<a href="https://evil.example/">click</a>',
        "<script>alert(1)</script>",
        '" onmouseover="x',
        "<img src=x onerror=alert(1)>",
        "&lt;b&gt; already escaped",
    ],
)
def test_markup_in_a_quote_is_shown_as_the_words_it_is(hostile: str) -> None:
    page = _parsed(render_html(build_digest([_slipped(quote=hostile)], ISSUE)))
    assert page.links == [SEC_URL]
    assert set(page.tags) <= ALLOWED_TAGS
    assert hostile in "".join(page.text)  # verbatim, character for character


@pytest.mark.parametrize(
    "url",
    [
        "http://www.sec.gov/Archives/edgar/data/1/ex99-1.htm",
        "javascript:alert(1)",
        "https:///no-host",
        "data:text/html,<script>alert(1)</script>",
        'https://www.sec.gov/a" onclick="x',
        "https://www.sec.gov/a b",
    ],
)
def test_an_address_that_is_not_a_plain_https_link_is_shown_as_text_never_as_a_link(url: str) -> None:
    page = _parsed(render_html(build_digest([_slipped(url=url)], ISSUE)))
    assert page.links == []
    assert "a" not in page.tags
    assert url in "".join(page.text)


def test_the_html_has_no_advice_words() -> None:
    html = render_html(build_digest([_slipped(), _detail(first_learned=IN_WEEK), _detail()], ISSUE))
    words = {word.strip(".,:;()").lower() for word in " ".join(_parsed(html).text).split()}
    notice_words = {word.strip(".,:;()").lower() for word in NOTICE.split()}
    assert not (words - notice_words) & {"buy", "sell", "hold", "bullish", "bearish", "target", "upside"}


# --- The command ---


def test_the_command_writes_the_digest_and_prints_only_counts(
    session_factory: sessionmaker[Session],
    seed: Seed,
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CATALYST_DATABASE_URL", database_url)
    out, html = tmp_path / "digest.json", tmp_path / "digest.html"
    assert cli.main(["digest", "--issue-date", "2026-10-05", "--out", str(out), "--html", str(html)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "issue_date": "2026-10-05",
        "moved": 1,
        "new": 0,
        "upcoming": 0,
        "upcoming_not_listed": 0,
        "moved_outside_free_calendar": 0,
        "new_outside_free_calendar": 0,
    }
    written = json.loads(out.read_text(encoding="utf-8"))
    assert [UUID(m["catalyst"]["id"]) for m in written["moved"]] == [seed.slipped]
    assert written["moved"][0]["kind"] == "later"
    assert written["notice"] == NOTICE
    page = _parsed(html.read_text(encoding="utf-8"))
    assert "topline data now expected in the fourth quarter of 2026" in "".join(page.text)
    # The advisory committee on 5 November is one day past this issue's 30 days; never shown unsourced.
    assert str(seed.adcom) not in out.read_text(encoding="utf-8")
    assert str(seed.unsourced) not in out.read_text(encoding="utf-8")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["digest.html", "digest.json"]


def test_the_next_issue_lists_the_calendar(
    seed: Seed, database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CATALYST_DATABASE_URL", database_url)
    out = tmp_path / "digest.json"
    assert cli.main(["digest", "--issue-date", "2026-10-12", "--out", str(out)]) == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert [UUID(c["id"]) for c in written["upcoming"]] == [seed.slipped, seed.adcom]
    assert written["moved"] == []


def test_the_command_refuses_a_day_that_is_not_a_monday(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CATALYST_DATABASE_URL", "postgresql+psycopg://postgres@127.0.0.1:1/nothing_test")
    assert cli.main(["digest", "--issue-date", "2026-10-06", "--out", str(tmp_path / "d.json")]) == 1
    assert "Monday" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_the_command_says_plainly_when_the_database_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CATALYST_DATABASE_URL", "postgresql+psycopg://postgres@127.0.0.1:1/nothing_test")
    assert cli.main(["digest", "--issue-date", "2026-10-05", "--out", str(tmp_path / "d.json")]) == 1
    captured = capsys.readouterr()
    assert "cannot reach the database" in captured.err
    assert "Traceback" not in captured.err
    assert list(tmp_path.iterdir()) == []
