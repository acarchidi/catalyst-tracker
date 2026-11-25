"""A deal's other dated steps never replace its closing date (ADR 0083).

For a `merger_acquisition` entry, a newer claim may move the close window only when its quote says
when the deal closes. A stockholder vote, an HSR waiting period, a tender offer's expiry or a record
date is another step of the deal: such a claim is held with the reason, and the close stays until a
person approves the move (the review the spec asks for). The NextCure case is the one the Opus review
of PR #164 found: a 9 December vote inside a fourth-quarter close moved the close to 9 December. The
sentences are shaped like the real ones, not copies.
"""

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from catalyst.publish.deal_steps import NO_CLOSING_WORDS, deal_step_hold
from catalyst.publish.live import Key, LiveCatalyst, Place, Rank
from catalyst.publish.run import decide_claims
from catalyst.publish.sanity import (
    NOT_OVERLAPPING,
    PASSED,
    TOO_FAR,
    Candidate,
    person_may_approve,
    safe_to_write,
)
from catalyst.publish.write import publish_claims
from catalyst.review import record_decision
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CheckOutcome,
    ClaimCheck,
    ClaimDecision,
    Company,
    CompanyTicker,
    ReviewAction,
    SourceTier,
)
from catalyst.snapshots import LocalSnapshotStore
from test_publish_writer import NOW, OCT_14, OCT_16, VERIFIER_MODEL, World

CLOSE_Q4 = "The merger is expected to close in the fourth quarter of 2026."
# NextCure: the 8-K that set the special meeting (shaped like it, not a copy).
NEXTCURE_VOTE = (
    "The special meeting of NextCure stockholders to vote on the merger will be held on December 9, 2026."
)
HSR = (
    "The waiting period under the Hart-Scott-Rodino Antitrust Improvements Act for the merger is "
    "expected to expire on November 30, 2026."
)
TENDER = (
    "The tender offer is scheduled to expire at one minute after 11:59 p.m., Eastern Time, on "
    "December 9, 2026, unless extended."
)
RECORD = "Stockholders of record at the close of business on November 2, 2026 may vote on the merger."
SLIP = "The merger is now expected to close in December 2026."


# --- The quote, read by plain code ---


@pytest.mark.parametrize(
    ("quote", "date_text"),
    [
        (CLOSE_Q4, "fourth quarter of 2026"),
        (SLIP, "December 2026"),
        ("NextCure expects to close the acquisition in December 2026.", "December 2026"),
        ("Closing of the merger is expected on December 15, 2026.", "December 15, 2026"),
        ("The transaction is expected to be consummated in December 2026.", "December 2026"),
        ("Completion of the acquisition is expected in December 2026.", "December 2026"),
        # A step named only as a condition after the date words does not date the deal.
        (
            "The merger is expected to close in December 2026, subject to approval by NextCure stockholders "
            "at a special meeting and the expiration of the HSR waiting period.",
            "December 2026",
        ),
    ],
)
def test_closing_words_for_the_deal_may_move_its_window(quote: str, date_text: str) -> None:
    assert deal_step_hold(quote, date_text) is None


@pytest.mark.parametrize(
    ("quote", "date_text", "step"),
    [
        (NEXTCURE_VOTE, "December 9, 2026", "a stockholder vote"),
        (HSR, "November 30, 2026", "a waiting period"),
        ("The HSR waiting period expired on November 3, 2026.", "November 3, 2026", "a waiting period"),
        (TENDER, "December 9, 2026", "a tender offer's expiry"),
        (RECORD, "November 2, 2026", "a record date"),
        # Opus review of PR #173 at b07ab37, B1: a step named without "vote" or "HSR".
        (
            "The merger is expected to close on the second business day after stockholders approve it on "
            "December 9, 2026.",
            "December 9, 2026",
            "a stockholder vote",
        ),
        (
            "Stockholders will be asked to approve the merger on December 9, 2026, with closing of the "
            "merger expected shortly thereafter.",
            "December 9, 2026",
            "a stockholder vote",
        ),
        (
            "Completion of the acquisition is expected promptly after the FTC clears the deal, expected on "
            "December 9, 2026.",
            "December 9, 2026",
            "a waiting period or regulatory clearance",
        ),
        (
            "The acquisition is expected to close after the European Commission clears it on "
            "December 9, 2026.",
            "December 9, 2026",
            "a waiting period or regulatory clearance",
        ),
        *(
            (
                f"The merger is expected to close after the {who} review ends on December 9, 2026.",
                "December 9, 2026",
                "a waiting period or regulatory clearance",
            )
            for who in ("EC", "CMA", "SAMR", "competition authorities'")
        ),
        (
            "The merger is expected to close once stockholder approval is obtained on December 9, 2026.",
            "December 9, 2026",
            "a stockholder vote",
        ),
        (
            "The merger will close after approval by the stockholders of NextCure on December 9, 2026.",
            "December 9, 2026",
            "a stockholder vote",
        ),
        (
            "The acquisition is expected to close after the Department of Justice review ends on "
            "December 9, 2026.",
            "December 9, 2026",
            "a waiting period or regulatory clearance",
        ),
        (
            "Closing of the merger will follow receipt of regulatory approvals, expected on "
            "December 9, 2026.",
            "December 9, 2026",
            "a waiting period or regulatory clearance",
        ),
    ],
)
def test_another_step_of_the_deal_is_held_with_its_name(quote: str, date_text: str, step: str) -> None:
    reason = deal_step_hold(quote, date_text)
    assert reason is not None
    assert step in reason
    assert "its closing" in reason


def test_a_step_and_the_closing_in_one_quote_is_held() -> None:
    """Plain code cannot tell whether the date is the vote's or the closing's."""
    quote = "The merger is expected to close shortly after the special meeting on December 9, 2026."
    reason = deal_step_hold(quote, "December 9, 2026")
    assert reason is not None
    assert "a stockholder vote" in reason
    assert "beside its closing" in reason


@pytest.mark.parametrize(
    "quote",
    [
        "Certain stockholders signed voting agreements, and the merger is expected to close in "
        "December 2026.",
        "The merger is expected to close by the close of business on December 15, 2026.",
    ],
)
def test_voting_agreements_or_close_of_business_do_not_hold_a_plain_close(quote: str) -> None:
    assert deal_step_hold(quote, "December 2026") is None


def test_a_condition_that_holds_the_date_words_is_not_skipped() -> None:
    """The date words sit in the condition, so they date the waiting period, not the closing."""
    quote = (
        "Completion of the merger is subject to the expiration of the HSR waiting period on "
        "November 30, 2026."
    )
    assert deal_step_hold(quote, "November 30, 2026") is not None


@pytest.mark.parametrize(
    "quote",
    [
        "The merger is expected in December 2026.",
        "The merger remains subject to customary closing conditions and is expected in December 2026.",
        "The parties expect to close their books in December 2026, after the merger agreement was signed.",
    ],
)
def test_without_closing_words_tied_to_the_deal_the_window_stays(quote: str) -> None:
    assert deal_step_hold(quote, "December 2026") == NO_CLOSING_WORDS


# --- In the writer's check ---

COMPANY = UUID(int=7)
Q4 = (date(2026, 10, 1), date(2026, 12, 31))


def _live(catalyst_type: str) -> LiveCatalyst:
    row = Catalyst.model_validate(
        {
            "catalyst_type": catalyst_type,
            "company_id": COMPANY,
            "title": "An open entry",
            "window_start": Q4[0],
            "window_end": Q4[1],
            "window_precision": "quarter",
            "window_raw_text": "fourth quarter of 2026",
            "date_confidence": "company_confirmed",
            "status": "upcoming",
        }
    )
    key = Key(COMPANY, catalyst_type, frozenset(), frozenset(), "")
    return LiveCatalyst(row, key, Rank(SourceTier.REGULATOR_OR_SEC, OCT_14), OCT_14)


def _candidate(quote: str, date_text: str, window: tuple[date, date], catalyst_type: str) -> Candidate:
    value = {
        "date_text": date_text,
        "window_start": window[0].isoformat(),
        "window_end": window[1].isoformat(),
        "window_precision": "day",
    }
    key = Key(COMPANY, catalyst_type, frozenset(), frozenset(), "")
    return Candidate(value, quote, key, Rank(SourceTier.REGULATOR_OR_SEC, OCT_16))


def test_non_merger_types_are_unchanged_in_the_check() -> None:
    dec_9 = (date(2026, 12, 9), date(2026, 12, 9))
    for catalyst_type in ("topline_readout", "pdufa_date", "advisory_committee"):
        candidate = _candidate(NEXTCURE_VOTE, "December 9, 2026", dec_9, catalyst_type)
        result = safe_to_write(candidate, Place(catalyst=_live(catalyst_type)), NOW.date())
        assert result.outcome is CheckOutcome.PASS


def _merger_result(quote: str, date_text: str, window: tuple[date, date]) -> str:
    candidate = _candidate(quote, date_text, window, "merger_acquisition")
    result = safe_to_write(candidate, Place(catalyst=_live("merger_acquisition")), NOW.date())
    assert result.outcome is CheckOutcome.FAIL
    assert result.reason is not None
    return result.reason


def test_a_held_step_is_one_a_person_may_lift() -> None:
    """A person's approval is the review the spec asks for; only the automatic move is blocked."""
    dec_9 = (date(2026, 12, 9), date(2026, 12, 9))
    reason = _merger_result(NEXTCURE_VOTE, "December 9, 2026", dec_9)
    assert reason.endswith("not its closing (from 2026-10-01..2026-12-31 to 2026-12-09..2026-12-09)")
    assert person_may_approve(reason)


def test_no_closing_words_is_one_a_person_may_lift() -> None:
    december = (date(2026, 12, 1), date(2026, 12, 31))
    reason = _merger_result("We now expect to close in December 2026.", "December 2026", december)
    assert reason == f"{NO_CLOSING_WORDS} (from 2026-10-01..2026-12-31 to 2026-12-01..2026-12-31)"
    assert person_may_approve(reason)


def test_a_step_reason_beside_one_no_person_may_lift_still_holds() -> None:
    assert not person_may_approve(f"{NO_CLOSING_WORDS}; {PASSED} (from a to b)")
    assert not person_may_approve(
        "the quote dates another step of the deal (an invented step), not its closing"
    )


@pytest.mark.parametrize(
    ("quote", "date_text", "window", "reason"),
    [
        # Closing words for the deal: the hold a person could approve before keeps its text.
        (
            "The merger is now expected to close in the first quarter of 2027.",
            "first quarter of 2027",
            (date(2027, 1, 1), date(2027, 3, 31)),
            f"{NOT_OVERLAPPING} (from 2026-10-01..2026-12-31 to 2027-01-01..2027-03-31)",
        ),
        (
            "The merger is now expected to close by the third quarter of 2027.",
            "by the third quarter of 2027",
            (date(2026, 12, 1), date(2027, 9, 30)),
            f"{TOO_FAR} (from 2026-10-01..2026-12-31 to 2026-12-01..2027-09-30)",
        ),
        # No closing words for the deal: the reason now says so too, and must be approved again
        # (Opus re-review of PR #173 at ffd136d).
        (
            "Closing is now expected in the first quarter of 2027.",
            "first quarter of 2027",
            (date(2027, 1, 1), date(2027, 3, 31)),
            f"{NOT_OVERLAPPING}; {NO_CLOSING_WORDS} (from 2026-10-01..2026-12-31 to 2027-01-01..2027-03-31)",
        ),
    ],
)
def test_every_date_reason_is_listed_and_a_person_may_approve_them(
    quote: str, date_text: str, window: tuple[date, date], reason: str
) -> None:
    found = _merger_result(quote, date_text, window)
    assert found == reason
    assert person_may_approve(found)


JUNE_VOTE = "The special meeting of NextCure stockholders to vote on the merger will be held on June 9, 2027."


def test_a_vote_held_for_a_long_move_shows_the_vote_too() -> None:
    """Opus re-review of PR #173 at ffd136d: a person approving the move must see the vote."""
    june_9 = (date(2027, 6, 9), date(2027, 6, 9))
    reason = _merger_result(JUNE_VOTE, "June 9, 2027", june_9)
    assert TOO_FAR in reason
    assert "another step of the deal (a stockholder vote)" in reason
    assert reason.endswith("(from 2026-10-01..2026-12-31 to 2027-06-09..2027-06-09)")
    assert person_may_approve(reason)


def test_a_claim_that_loses_to_the_source_still_passes() -> None:
    """It writes nothing, so it needs no hold."""
    dec_9 = (date(2026, 12, 9), date(2026, 12, 9))
    candidate = _candidate(NEXTCURE_VOTE, "December 9, 2026", dec_9, "merger_acquisition")
    earlier = Rank(SourceTier.REGULATOR_OR_SEC, OCT_14 - timedelta(days=1))
    older = Candidate(candidate.value, candidate.quote, candidate.key, earlier)
    result = safe_to_write(older, Place(catalyst=_live("merger_acquisition")), NOW.date())
    assert result.outcome is CheckOutcome.PASS


# --- Through the writer, on a database ---


@pytest.fixture
def world(session_factory: sessionmaker[Session], tmp_path: Path) -> World:
    return World(session_factory, LocalSnapshotStore(tmp_path))


def _publish(world: World, at: datetime = NOW) -> dict[str, Any]:
    return publish_claims(world.factory, world.store, lambda: at, verifier_model=VERIFIER_MODEL).summary()


def _nextcure(world: World) -> UUID:
    world.company = world.add(Company(cik=1398529, name="NextCure, Inc.", sic_code="2834"))
    world.add(
        CompanyTicker(company_id=world.company, ticker="NXTC", exchange="Nasdaq", valid_from=date(2020, 1, 1))
    )
    _deal(world, CLOSE_Q4, "fourth quarter of 2026", OCT_14)
    assert _publish(world)["catalysts_created"] == 1
    (row,) = world.rows(Catalyst)
    return row.id


def _deal(world: World, sentence: str, date_text: str, published: datetime) -> UUID:
    return world.claim(
        date_text,
        published=published,
        sentence=sentence,
        catalyst_type="merger_acquisition",
        drug=None,
        trial=False,
        indication=None,
    )


def _check(world: World, claim: UUID, name: str) -> tuple[str, str | None]:
    (row,) = [r for r in world.rows(ClaimCheck) if r.claim_id == claim and r.check_name == name]
    return row.outcome, row.reason


def _window(world: World) -> tuple[str, str, str]:
    (row,) = world.rows(Catalyst)
    return row.window_start.isoformat(), row.window_end.isoformat(), row.window_raw_text


def test_nextcure_a_newer_vote_date_does_not_replace_the_close(world: World) -> None:
    _nextcure(world)
    changes = len(world.rows(CatalystChange))
    vote = _deal(world, NEXTCURE_VOTE, "December 9, 2026", OCT_16)
    summary = _publish(world)
    assert (summary["catalysts_updated"], summary["held_by_writer"]) == (0, 1)
    assert _window(world) == ("2026-10-01", "2026-12-31", "fourth quarter of 2026")
    assert len(world.rows(CatalystChange)) == changes
    outcome, reason = _check(world, vote, "safe_to_write")
    assert outcome == "fail"
    assert reason is not None
    assert "a stockholder vote" in reason


def test_a_real_slip_of_the_close_inside_the_window_still_updates(world: World) -> None:
    _nextcure(world)
    _deal(world, SLIP, "December 2026", OCT_16)
    assert _publish(world)["catalysts_updated"] == 1
    assert _window(world) == ("2026-12-01", "2026-12-31", "December 2026")


@pytest.mark.parametrize(
    ("sentence", "date_text", "step"),
    [(HSR, "November 30, 2026", "a waiting period"), (TENDER, "December 9, 2026", "a tender offer's expiry")],
)
def test_an_hsr_or_tender_expiry_is_held(world: World, sentence: str, date_text: str, step: str) -> None:
    _nextcure(world)
    claim = _deal(world, sentence, date_text, OCT_16)
    assert _publish(world)["held_by_writer"] == 1
    assert _window(world) == ("2026-10-01", "2026-12-31", "fourth quarter of 2026")
    outcome, reason = _check(world, claim, "safe_to_write")
    assert outcome == "fail"
    assert reason is not None
    assert step in reason


def test_a_non_merger_narrowing_still_moves_the_date(world: World) -> None:
    world.claim("fourth quarter of 2026")
    _publish(world)
    world.claim("December 2026", published=OCT_16)
    assert _publish(world)["catalysts_updated"] == 1
    assert _window(world)[:2] == ("2026-12-01", "2026-12-31")


def _decide(world: World) -> dict[UUID, tuple[str, list[str]]]:
    decide_claims(world.factory, world.store, lambda: NOW)
    return {row.claim_id: (row.decision, list(row.reasons)) for row in world.rows(ClaimDecision)}


@pytest.mark.parametrize(
    ("sentence", "date_text", "window"),
    [
        # The writer's type check also needs a deal word in the quote.
        (
            "We now expect to close in December 2026, as the merger agreement provides.",
            "December 2026",
            ("2026-12-01", "2026-12-31"),
        ),
        (NEXTCURE_VOTE, "December 9, 2026", ("2026-12-09", "2026-12-09")),
        # Held for a long move and for the vote: approved with both in view, it moves.
        (JUNE_VOTE, "June 9, 2027", ("2027-06-09", "2027-06-09")),
    ],
)
def test_a_person_may_approve_a_held_deal_date_and_it_moves(
    world: World, sentence: str, date_text: str, window: tuple[str, str]
) -> None:
    _nextcure(world)
    claim = _deal(world, sentence, date_text, OCT_16)
    assert _publish(world)["held_by_writer"] == 1
    decision, reasons = _decide(world)[claim]
    assert decision == "review"
    if sentence == JUNE_VOTE:  # the person sees every reason, the vote among them
        (reason,) = reasons
        assert TOO_FAR in reason
        assert "a stockholder vote" in reason
    record_decision(world.factory, claim, ReviewAction.APPROVE, "acarchidi", reasons, lambda: NOW)
    _decide(world)
    summary = _publish(world)
    assert (summary["catalysts_updated"], summary["published_by_approval"]) == (1, 1)
    assert _window(world)[:2] == window
