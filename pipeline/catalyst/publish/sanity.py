"""The check `safe_to_write`: can the writer place a claim without guessing? (ADR 0018, part 5b).

Plain code over the claim, its matches, and the published catalysts. It fails, with the reason,
when a person must decide:
- the claim fits more than one published catalyst, or one except for the indication;
- a new event's drug role is not clear from the quote;
- its window does not overlap the published one's (a slip, or another event?);
- it outranks the published source and would change the date, but its window has already ended
  while the published one has not (B2), or either end would move more than 6 months, unless the
  new window lies inside the old one (spec: Change sanity);
- a source of the same rank gives other dates for the same event: another claim not yet
  published (both wait), or the published one's source (CP4 review, O2). A tie decides nothing.

Anything else passes, including a claim that loses to the published source: it writes nothing.

Its job runs before the decisions, so `decide-claims` sees every hold. The writer computes it
again as it writes, against the catalysts as that run leaves them, and stores what changed.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import combinations
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.extract.agent import EXTRACTOR_PROMPT, ROLE
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
from catalyst.schemas import CheckOutcome, Claim, Drug, DrugName, DrugRole, SourceDocument, SourceTier
from catalyst.verify.rules import CheckResult, Match, months_after
from catalyst.verify.run import load_matches, store_check

MOVE_MONTHS = 6  # spec, Change sanity: a date that moves more than 6 months always goes to review
NO_EVENT = "the claim names no single company and type"
ROLE_UNCLEAR = "the quote does not make clear whether the drug is tested alone or in combination"
NOT_OVERLAPPING = "the window does not overlap the published one"
PASSED = "the new window has already passed"
TOO_FAR = "the date would move more than 6 months"
TIED = "a source of the same rank gives other dates for this event"
# Holds a person's approval may lift: each asks only yes or no about the claim's own dates
# (ADR 0042). The others need a choice the writer would otherwise guess (which catalyst, which
# drug role, which of two tied sources), so an approval leaves them held.
PERSON_MAY_APPROVE: frozenset[str] = frozenset({NOT_OVERLAPPING, TOO_FAR})

_JOIN = (
    r"(?:in\s+combination\s+with|combined\s+with|plus|\+|together\s+with|added\s+to|co-?administered\s+with)"
)
_COMBINATION = re.compile(
    r"\bcombination|\bcombined\b|\bplus\b|\+|\btogether\s+with\b|\badded\s+to\b|\bco-?administered\b",
    re.IGNORECASE,
)
_ALONE = re.compile(r"\bmonotherapy\b|\balone\b", re.IGNORECASE)
# Words that say a regimen or a combination without saying which drug is in it (round 2, O1).
_UNCLEAR = re.compile(
    r"\bdoublets?\b|\btriplets?\b|\bregimens?\b|\bbackbones?\b|\bin\s+addition\s+to\b|\bon\s+top\s+of\b",
    re.IGNORECASE,
)
_WITH = re.compile(r"\bwith\s+", re.IGNORECASE)
# A code name, as the resolver reads one: letters, an optional dash or space, then digits (AB-123).
_CODE_NAME = re.compile(r"[A-Z]{1,6}[\s\-\u2010-\u2015]?\d{2,6}[A-Z]?(?!\w)")

_log = logging.getLogger(__name__)


def _with_another_drug(quote: str, other_drugs: frozenset[str]) -> bool:
    """Say whether "with" is followed by another drug on file, or by a code name."""
    for found in _WITH.finditer(quote):
        after = quote[found.end() :]
        if _CODE_NAME.match(after):
            return True
        words = after.casefold()
        if any(
            words.startswith(name) and not words[len(name) : len(name) + 1].isalnum() for name in other_drugs
        ):
            return True
    return False


def drug_role(quote: str, drug: str | None, other_drugs: frozenset[str]) -> DrugRole | None:
    """Return the drug's role as the quote states it, or None when a person must decide.

    A quote with no combination words names no combination, so the link is `alone`. A quote with
    combination words gives `in_combination` only when they join the drug itself ("X plus Y",
    "Y in combination with X") and nothing says "alone" or "monotherapy". A regimen word
    (doublet, triplet, regimen, backbone, "in addition to", "on top of"), or "with" before another
    drug on file (`other_drugs`, case-folded) or a code name, is unclear too.
    """
    if _UNCLEAR.search(quote) or _with_another_drug(quote, other_drugs):
        return None
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
    other_drugs: frozenset[str] = frozenset()  # every drug name on file but the claim's own, case-folded
    tied: bool = False  # another unpublished claim of the same rank gives this event other dates (O2)


def _fail(reason: str) -> CheckResult:
    return CheckResult(WRITER_CHECK, CheckOutcome.FAIL, reason)


def safe_to_write(candidate: Candidate, where: Place, today: date) -> CheckResult:
    """Decide whether the writer can place one claim without guessing (see the module text)."""
    if candidate.key is None:
        return _fail(NO_EVENT)
    if where.hold is not None:
        return _fail(where.hold)
    if candidate.tied:
        return _fail(TIED)
    live = where.catalyst
    if live is None:
        drug = candidate.value.get("drug")
        stated = drug if isinstance(drug, str) else None
        unclear = candidate.key.drugs and drug_role(candidate.quote, stated, candidate.other_drugs) is None
        return _fail(ROLE_UNCLEAR) if unclear else CheckResult(WRITER_CHECK, CheckOutcome.PASS)
    return _against_live(candidate, live, today)


def _against_live(candidate: Candidate, live: LiveCatalyst, today: date) -> CheckResult:
    """Decide a claim about a published catalyst: a sane date change from a better source, or nothing."""
    window = window_of(candidate.value)
    if window is None:  # `has_window` fails it; nothing here to compare
        return CheckResult(WRITER_CHECK, CheckOutcome.PASS)
    if not overlap(window, live.window):
        return _fail(NOT_OVERLAPPING)
    if same_dates(live, candidate.value):
        return CheckResult(WRITER_CHECK, CheckOutcome.PASS)
    if candidate.rank == live.source:  # neither outranks the other, so a person decides (O2)
        return _fail(TIED)
    outranks = ranks_above(
        (candidate.rank.tier, candidate.rank.published_at), (live.source.tier, live.source.published_at)
    )
    if not outranks:
        return CheckResult(WRITER_CHECK, CheckOutcome.PASS)
    if window[1] < today <= live.window[1]:
        return _fail(PASSED)
    if too_far(live.window, window):
        return _fail(TOO_FAR)
    return CheckResult(WRITER_CHECK, CheckOutcome.PASS)


@dataclass(frozen=True)
class Stated:
    """What one unpublished claim says about its event, as the tie check reads it."""

    claim_id: UUID
    key: Key
    rank: Rank
    window: tuple[object, object, object]  # start, end, and precision, as the claim stores them


def _same_event(a: Key, b: Key) -> bool:
    """Say whether two claims could state one event: as `place` matches, trials named or not."""
    same = (a.company_id, a.catalyst_type, a.drugs, a.indication) == (
        b.company_id,
        b.catalyst_type,
        b.drugs,
        b.indication,
    )
    return same and (a.trials == b.trials or not a.trials or not b.trials)


def tied_claims(stated: Iterable[Stated]) -> frozenset[UUID]:
    """Return every claim that another claim of the same rank states as the same event, with other dates.

    Without a window a claim ties with nothing: `has_window` holds it anyway.
    """
    groups: defaultdict[Rank, list[Stated]] = defaultdict(list)
    for item in stated:
        if all(item.window):
            groups[item.rank].append(item)
    tied: set[UUID] = set()
    for group in groups.values():
        for a, b in combinations(group, 2):
            if a.window != b.window and _same_event(a.key, b.key):
                tied |= {a.claim_id, b.claim_id}
    return frozenset(tied)


def load_ties(
    session: Session, matches: Mapping[UUID, Sequence[Match]], cited: Mapping[UUID, UUID]
) -> frozenset[UUID]:
    """Find the tied claims among those not yet published, from each document's latest reading.

    Only the current extractor prompt counts: the gate holds an older prompt's claims anyway, and
    a superseded reading must not hold the one that replaced it.
    """
    claim, document = TABLES[Claim], TABLES[SourceDocument]
    query = (
        select(
            claim.c.id,
            claim.c.value,
            claim.c.source_document_id,
            claim.c.extracted_at,
            document.c.source_tier,
            document.c.published_at,
        )
        .join(document, document.c.id == claim.c.source_document_id)
        .where(
            claim.c.agent == ROLE,
            claim.c.field.startswith("catalyst:"),
            claim.c.prompt_id == EXTRACTOR_PROMPT[0],
            claim.c.prompt_version == EXTRACTOR_PROMPT[1],
        )
    )
    rows = list(session.execute(query))
    latest: dict[UUID, datetime] = {}
    for row in rows:
        latest[row.source_document_id] = max(
            row.extracted_at, latest.get(row.source_document_id, row.extracted_at)
        )
    stated: list[Stated] = []
    for row in rows:
        key = claim_key(row.value, matches.get(row.id, []))
        if key is None or row.id in cited or row.extracted_at != latest[row.source_document_id]:
            continue
        value = claim_value(row.value)
        window = (value.get("window_start"), value.get("window_end"), value.get("window_precision"))
        stated.append(Stated(row.id, key, Rank(SourceTier(row.source_tier), row.published_at), window))
    return tied_claims(stated)


def drug_names(session: Session) -> dict[UUID, frozenset[str]]:
    """Return every drug's names on file, preferred and other, case-folded."""
    drug, name = TABLES[Drug], TABLES[DrugName]
    found: defaultdict[UUID, set[str]] = defaultdict(set)
    for row in session.execute(select(drug.c.id, drug.c.preferred_name)):
        found[row.id].add(row.preferred_name.casefold())
    for row in session.execute(select(name.c.drug_id, name.c.name)):
        found[row.drug_id].add(row.name.casefold())
    return {drug_id: frozenset(names) for drug_id, names in found.items()}


def others(names: dict[UUID, frozenset[str]], key: Key | None) -> frozenset[str]:
    """Return the names of every drug on file except the claim's own matched drugs."""
    own = key.drugs if key is not None else frozenset[UUID]()
    mine: set[str] = set()
    rest: set[str] = set()
    for drug_id, drug_names_ in names.items():
        (mine if drug_id in own else rest).update(drug_names_)
    return frozenset(rest - mine)


def safe_results(session: Session, today: date) -> dict[UUID, CheckResult]:
    """Compute, without storing, `safe_to_write` for every extractor claim against the published catalysts."""
    claim, document = TABLES[Claim], TABLES[SourceDocument]
    live, matches, names = load_live(session), load_matches(session), drug_names(session)
    ties = load_ties(session, matches, live.cited)
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
        rank = Rank(SourceTier(row.source_tier), row.published_at)
        candidate = Candidate(
            claim_value(row.value), row.quote, key, rank, others(names, key), row.id in ties
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
