"""A newer source of the same rank updates the entry instead of holding it (ADR 0069).

Spec, Which claim wins: among sources of equal rank, the most recently published wins. So a
claim from a newer release of the same tier (a later New York day) that passes every other check
replaces the window, even when the two windows do not overlap; the move is a change row citing
the new claim, and the older window stays in the history with its own source. An older one loses
and writes nothing.

Holds stay for real conflicts: two sources of the same day, a newer source of a lower tier, a
move of more than 6 months, a forecast window that has since passed (B2, in
`test_publish_writer.py`), and two indications that are both stated and differ.

The owner's NextCure case is modelled on the real filings, in our own words: an 8-K of 25
September 2026 said the merger would close in November 2026; a later 8-K set the stockholder vote
for 9 December 2026.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from catalyst.api.access import Access
from catalyst.api.read import published
from catalyst.outreach.digest import build_digest, horizon
from catalyst.publish.sanity import NOT_OVERLAPPING, check_safe_to_write
from catalyst.publish.write import publish_claims
from catalyst.schemas import CatalystChange, ClaimCheck, SourceTier
from catalyst.snapshots import LocalSnapshotStore
from test_publish_writer import OCT_14, OCT_16, VERIFIER_MODEL, World

NOVEMBER_24 = datetime(2026, 11, 24, 17, tzinfo=UTC)
SEP_25 = datetime(2026, 9, 25, 20, 30, tzinfo=UTC)
NOV_10 = datetime(2026, 11, 10, 21, 5, tzinfo=UTC)
CLOSE_IN_NOVEMBER = (
    "The merger is expected to close in November 2026, subject to the approval of NextCure stockholders."
)
VOTE_ON_DECEMBER_9 = (
    "The special meeting of NextCure stockholders to vote on the merger will be held on December 9, 2026."
)


@pytest.fixture
def world(session_factory: sessionmaker[Session], tmp_path: Path) -> World:
    return World(session_factory, LocalSnapshotStore(tmp_path))


def _publish(world: World, at: datetime) -> dict[str, Any]:
    return publish_claims(world.factory, world.store, lambda: at, verifier_model=VERIFIER_MODEL).summary()


def _check(world: World, claim: UUID) -> tuple[str, str | None]:
    (row,) = [r for r in world.rows(ClaimCheck) if r.claim_id == claim and r.check_name == "safe_to_write"]
    return row.outcome, row.reason


def _deal(world: World, date_text: str, sentence: str, published: datetime, **extra: Any) -> UUID:
    return world.claim(
        date_text,
        published=published,
        sentence=sentence,
        catalyst_type="merger_acquisition",
        drug=None,
        trial=False,
        indication=None,
        **extra,
    )


def _sections(world: World, issue: date) -> dict[str, list[tuple[str, str]]]:
    """Return the windows each section of the paid issue lists, as start and end."""
    with world.factory() as session:
        details = published(session)
    digest = build_digest(details, issue, access=Access.PAID)
    last = horizon(issue)
    listed = [*digest.upcoming, *(move.catalyst for move in digest.moved), *digest.new]
    spans = [(c.window.start, c.window.end) for c in listed]
    return {
        "due": [(s.isoformat(), e.isoformat()) for s, e in spans if e <= last],
        "open": [(s.isoformat(), e.isoformat()) for s, e in spans if s <= last < e],
        "later": [(s.isoformat(), e.isoformat()) for s, e in spans if s > last],
    }


# --- The owner's case, and the calendar ---


def test_nextcure_the_newer_8k_moves_the_close_past_november_and_keeps_the_old_window_in_history(
    world: World,
) -> None:
    old = _deal(world, "November 2026", CLOSE_IN_NOVEMBER, SEP_25)
    _publish(world, SEP_25 + timedelta(days=1))
    assert _sections(world, date(2026, 11, 23))["due"] == [("2026-11-01", "2026-11-30")]
    new = _deal(world, "December 9, 2026", VOTE_ON_DECEMBER_9, NOV_10)
    check_safe_to_write(world.factory, lambda: NOVEMBER_24)
    assert _check(world, new) == ("pass", None)
    summary = _publish(world, NOVEMBER_24)
    assert (summary["catalysts_updated"], summary["held_by_writer"]) == (1, 0)
    (detail,) = _details(world)
    assert (detail.window.start.isoformat(), detail.window.end.isoformat()) == ("2026-12-09", "2026-12-09")
    rows = sorted(world.rows(CatalystChange), key=lambda c: c.learned_at)
    ends = [(c.old_value, c.new_value, c.claim_id) for c in rows if c.field == "window_end"]
    assert ends == [(None, "2026-11-30", old), ("2026-11-30", "2026-12-09", new)]  # both sources kept
    history = [c for c in detail.history if c.field == "window_end"]
    assert [c.source.quote for c in history] == [CLOSE_IN_NOVEMBER, VOTE_ON_DECEMBER_9]
    sections = _sections(world, date(2026, 11, 30))
    assert ("2026-11-01", "2026-11-30") not in sections["due"] + sections["open"]
    assert sections["due"] == [("2026-12-09", "2026-12-09")]


def test_an_entry_whose_window_has_fully_ended_is_in_no_section(world: World) -> None:
    _deal(world, "October 2026", "The merger is expected to close in October 2026.", SEP_25)
    _publish(world, SEP_25 + timedelta(days=1))
    assert _sections(world, date(2026, 10, 5))["due"] == [("2026-10-01", "2026-10-31")]
    assert _sections(world, date(2026, 11, 23)) == {"due": [], "open": [], "later": []}


def _details(world: World) -> list[Any]:
    with world.factory() as session:
        return published(session)


# --- What still waits for a person ---


def test_a_newer_source_of_the_same_day_still_waits(world: World) -> None:
    world.claim("second half of 2027")
    _publish(world, OCT_16)
    same_day = world.claim("first half of 2028", published=OCT_14 + timedelta(hours=2))
    summary = _publish(world, OCT_16)
    assert (summary["held_by_writer"], summary["catalysts_updated"]) == (1, 0)
    assert _check(world, same_day)[1] == (
        f"{NOT_OVERLAPPING} (from 2027-07-01..2027-12-31 to 2028-01-01..2028-06-30)"
    )


def test_a_newer_source_of_a_lower_tier_still_waits(world: World) -> None:
    world.claim("second half of 2027")
    _publish(world, OCT_16)
    wire = world.claim("first half of 2028", published=OCT_16, tier=SourceTier.WIRE)
    check_safe_to_write(world.factory, lambda: OCT_16 + timedelta(hours=1))
    assert (
        _check(world, wire)[1] == f"{NOT_OVERLAPPING} (from 2027-07-01..2027-12-31 to 2028-01-01..2028-06-30)"
    )


def test_a_newer_move_of_more_than_six_months_still_waits(world: World) -> None:
    world.claim("October 2027")
    _publish(world, OCT_16)
    far = world.claim("second half of 2028", published=OCT_16)
    assert _publish(world, OCT_16 + timedelta(hours=1))["held_by_writer"] == 1
    assert _check(world, far)[1] == (
        "the date would move more than 6 months (from 2027-10-01..2027-10-31 to 2028-07-01..2028-12-31)"
    )


def test_an_older_source_of_the_same_rank_loses_and_writes_nothing(world: World) -> None:
    world.claim("second half of 2027", published=OCT_16)
    _publish(world, OCT_16 + timedelta(hours=1))
    older = world.claim("first half of 2028", published=OCT_14)
    summary = _publish(world, OCT_16 + timedelta(hours=2))
    assert (summary["held_by_writer"], summary["not_written_lower_rank"], summary["catalysts_updated"]) == (
        0,
        1,
        0,
    )
    assert _check(world, older) == ("pass", None)


# --- Another indication ---


def test_a_newer_source_that_names_no_indication_updates_the_entry(world: World) -> None:
    world.claim("second half of 2027", indication="MASH")
    _publish(world, OCT_16)
    world.claim("first half of 2028", published=OCT_16, indication=None)
    assert _publish(world, OCT_16 + timedelta(hours=1))["catalysts_updated"] == 1


def test_a_newer_source_that_first_names_the_indication_updates_the_entry(world: World) -> None:
    world.claim("second half of 2027", indication=None)
    _publish(world, OCT_16)
    world.claim("fourth quarter of 2027", published=OCT_16, indication="MASH")
    assert _publish(world, OCT_16 + timedelta(hours=1))["catalysts_updated"] == 1


def test_an_older_source_that_names_no_indication_still_waits(world: World) -> None:
    world.claim("second half of 2027", indication="MASH", published=OCT_16)
    _publish(world, OCT_16 + timedelta(hours=1))
    older = world.claim("first half of 2028", indication=None)
    assert _publish(world, OCT_16 + timedelta(hours=2))["held_by_writer"] == 1
    assert _check(world, older) == ("fail", "the indication differs from the published one")
