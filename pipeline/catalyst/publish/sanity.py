"""The check `safe_to_write`: can the writer place a claim without guessing? (ADR 0018, part 5b).

Plain code over the claim, its matches, and the published catalysts. It fails, with the reason,
when a person must decide:
- the claim fits more than one published catalyst, or one except for the indication;
- a new event's drug role is not clear from the quote;
- its window does not overlap the published one's (a slip, or another event?);
- it outranks the published source and would change the date, but its window has already ended
  while the published one has not (B2), or either end would move more than 6 months, unless the
  new window lies inside the old one (spec: Change sanity).

Anything else passes, including a claim that loses to the published source: it writes nothing.

Its job runs before the decisions, so `decide-claims` sees every hold. The writer computes it
again as it writes, against the catalysts as that run leaves them, and stores what changed.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.extract.agent import ROLE
from catalyst.extract.run import NEW_YORK
from catalyst.logs import log_event
from catalyst.publish.gate import WRITER_CHECK
from catalyst.publish.live import (
    Key,
    LiveCatalyst,
    Place,
    Rank,
    claim_key,
    claim_value,
    load_live,
    ranks_above,
)
from catalyst.schemas import CheckOutcome, Claim, DrugRole, SourceDocument, SourceTier
from catalyst.verify.rules import CheckResult, months_after
from catalyst.verify.run import load_matches, store_check

MOVE_MONTHS = 6  # spec, Change sanity: a date that moves more than 6 months always goes to review
NO_EVENT = "the claim names no single company and type"
ROLE_UNCLEAR = "the quote does not make clear whether the drug is tested alone or in combination"
NOT_OVERLAPPING = "the window does not overlap the published one"
PASSED = "the new window has already passed"
TOO_FAR = "the date would move more than 6 months"

_JOIN = (
    r"(?:in\s+combination\s+with|combined\s+with|plus|\+|together\s+with|added\s+to|co-?administered\s+with)"
)
_COMBINATION = re.compile(
    r"\bcombination|\bcombined\b|\bplus\b|\+|\btogether\s+with\b|\badded\s+to\b|\bco-?administered\b",
    re.IGNORECASE,
)
_ALONE = re.compile(r"\bmonotherapy\b|\balone\b", re.IGNORECASE)

_log = logging.getLogger(__name__)


def drug_role(quote: str, drug: str | None) -> DrugRole | None:
    """Return the drug's role as the quote states it, or None when a person must decide.

    A quote with no combination words names no combination, so the link is `alone`. A quote with
    combination words gives `in_combination` only when they join the drug itself ("X plus Y",
    "Y in combination with X") and nothing says "alone" or "monotherapy". Anything else is unclear.
    """
    combined, alone = _COMBINATION.search(quote), _ALONE.search(quote)
    if not combined:
        return DrugRole.ALONE
    if alone or not drug or not drug.strip():
        return None
    name = r"\s+".join(map(re.escape, drug.split()))
    joined = rf"(?<!\w){name}(?!\w)\s*,?\s*{_JOIN}|{_JOIN}\s*(?<!\w){name}(?!\w)"
    return DrugRole.IN_COMBINATION if re.search(joined, quote, re.IGNORECASE) else None


def window_of(value: object) -> tuple[date, date] | None:
    """Return a claim's window, or None when it has none."""
    stated = claim_value(value)
    try:
        return date.fromisoformat(stated["window_start"]), date.fromisoformat(stated["window_end"])
    except (KeyError, TypeError, ValueError):
        return None


def overlap(a: tuple[date, date], b: tuple[date, date]) -> bool:
    """Say whether two windows share at least one day."""
    return a[0] <= b[1] and b[0] <= a[1]


def too_far(old: tuple[date, date], new: tuple[date, date]) -> bool:
    """Say whether a move takes either end more than 6 months; a narrowing never does."""
    if old[0] <= new[0] and new[1] <= old[1]:
        return False
    return any(
        moved > months_after(was, MOVE_MONTHS) or moved < months_after(was, -MOVE_MONTHS)
        for was, moved in zip(old, new, strict=True)
    )


def same_dates(live: LiveCatalyst, value: object) -> bool:
    """Say whether the claim gives the window the catalyst shows now: same start, end, and precision."""
    stated = claim_value(value)
    row = live.row
    return (row.window_start.isoformat(), row.window_end.isoformat(), row.window_precision.value) == (
        stated.get("window_start"),
        stated.get("window_end"),
        stated.get("window_precision"),
    )


@dataclass(frozen=True)
class Candidate:
    """What the check reads about one claim."""

    value: dict[str, Any]
    quote: str
    key: Key | None
    rank: Rank


def _fail(reason: str) -> CheckResult:
    return CheckResult(WRITER_CHECK, CheckOutcome.FAIL, reason)


def safe_to_write(candidate: Candidate, where: Place, today: date) -> CheckResult:
    """Decide whether the writer can place one claim without guessing (see the module text)."""
    if candidate.key is None:
        return _fail(NO_EVENT)
    if where.hold is not None:
        return _fail(where.hold)
    live = where.catalyst
    if live is None:
        drug = candidate.value.get("drug")
        unclear = (
            candidate.key.drugs
            and drug_role(candidate.quote, drug if isinstance(drug, str) else None) is None
        )
        return _fail(ROLE_UNCLEAR) if unclear else CheckResult(WRITER_CHECK, CheckOutcome.PASS)
    window = window_of(candidate.value)
    if window is None:  # `has_window` fails it; nothing here to compare
        return CheckResult(WRITER_CHECK, CheckOutcome.PASS)
    if not overlap(window, live.window):
        return _fail(NOT_OVERLAPPING)
    outranks = ranks_above(
        (candidate.rank.tier, candidate.rank.published_at), (live.source.tier, live.source.published_at)
    )
    if not outranks or same_dates(live, candidate.value):
        return CheckResult(WRITER_CHECK, CheckOutcome.PASS)
    if window[1] < today <= live.window[1]:
        return _fail(PASSED)
    if too_far(live.window, window):
        return _fail(TOO_FAR)
    return CheckResult(WRITER_CHECK, CheckOutcome.PASS)


def safe_results(session: Session, today: date) -> dict[UUID, CheckResult]:
    """Compute, without storing, `safe_to_write` for every extractor claim against the published catalysts."""
    claim, document = TABLES[Claim], TABLES[SourceDocument]
    live, matches = load_live(session), load_matches(session)
    query = (
        select(claim.c.id, claim.c.value, claim.c.quote, document.c.source_tier, document.c.published_at)
        .join(document, document.c.id == claim.c.source_document_id)
        .where(claim.c.agent == ROLE, claim.c.field.startswith("catalyst:"))
    )
    results: dict[UUID, CheckResult] = {}
    for row in session.execute(query):
        if row.id in live.cited:  # already written
            results[row.id] = CheckResult(WRITER_CHECK, CheckOutcome.PASS)
            continue
        key = claim_key(row.value, matches.get(row.id, []))
        candidate = Candidate(
            claim_value(row.value), row.quote, key, Rank(SourceTier(row.source_tier), row.published_at)
        )
        results[row.id] = safe_to_write(candidate, live.place(key), today)
    return results


@dataclass
class SafeReport:
    """What one run of the check found."""

    claims: int = 0
    outcomes: Counter[str] = field(default_factory=Counter[str])

    def summary(self) -> dict[str, Any]:
        """Return the counts as plain JSON."""
        return {"claims": self.claims, "outcomes": dict(self.outcomes)}


def check_safe_to_write(session_factory: sessionmaker[Session], now: Callable[[], datetime]) -> SafeReport:
    """Store `safe_to_write` for every extractor claim; safe to re-run."""
    report = SafeReport()
    at = now()
    with session_factory.begin() as session:
        for claim_id, result in safe_results(session, at.astimezone(NEW_YORK).date()).items():
            store_check(session, claim_id, result, at)
            report.claims += 1
            report.outcomes[f"{result.check}:{result.outcome.value}"] += 1
    log_event(_log, "safe_to_write_checked", **report.summary())
    return report
