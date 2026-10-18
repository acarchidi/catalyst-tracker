"""Rule checks: plain code that every claim must pass before it can publish (ADR 0018).

The rules come from regulation and from our own records, never from a model's output. Each check
answers pass, fail with a reason a person can read, or not applicable.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, cast
from uuid import UUID

from catalyst.extract.agent import has_words
from catalyst.extract.dates import parse_window
from catalyst.resolve import names_trial
from catalyst.schemas import CheckOutcome, TrialPhase

PDUFA_MONTHS = 15  # longest standard review (12 months from submission) plus a 3-month extension


Outcome = CheckOutcome  # stored as ClaimCheck.outcome


@dataclass(frozen=True)
class Match:
    """One name in the claim and what the entity resolver matched it to."""

    entity: str
    status: str
    matched_id: UUID | None


@dataclass(frozen=True)
class TrialNames:
    """What a trial is called: its registry number and, if it has one, the company's study name."""

    nct_id: str
    study_name: str | None


@dataclass(frozen=True)
class ClaimFacts:
    """Everything the rule checks read about one claim.

    A matched trial missing from `trial_names` fails `trial_named_in_quote`, so leaving the
    names out can only hold a claim back, never let one through.
    """

    id: UUID
    value: Mapping[str, Any]
    quote: str
    published: date
    matches: tuple[Match, ...]
    trial_phases: Mapping[UUID, TrialPhase | None]
    trial_names: Mapping[UUID, TrialNames] = field(default_factory=dict[UUID, TrialNames])


@dataclass(frozen=True)
class CheckResult:
    """One check's conclusion about one claim."""

    check: str
    outcome: Outcome
    reason: str | None = None


def _passed(check: str) -> CheckResult:
    return CheckResult(check, Outcome.PASS)


def _failed(check: str, reason: str) -> CheckResult:
    return CheckResult(check, Outcome.FAIL, reason)


def _not_applicable(check: str, reason: str) -> CheckResult:
    return CheckResult(check, Outcome.NOT_APPLICABLE, reason)


def _day(value: object) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _date_text(facts: ClaimFacts) -> str | None:
    text = facts.value.get("date_text")
    return text if isinstance(text, str) and text.strip() else None


def _extractor_checks(facts: ClaimFacts, _today: date) -> CheckResult:
    listed: object = facts.value.get("problems")
    if not isinstance(listed, list):
        return _failed("extractor_checks", "the claim has no list of the extractor's problems")
    problems = [str(p) for p in cast(list[object], listed)]
    if problems:
        return _failed("extractor_checks", "the extractor flagged: " + ", ".join(problems))
    return _passed("extractor_checks")


# The next two redo the extractor's date checks in this module, so they hold even if a stored
# value was never checked or was changed later (review of PR #29).
def _date_words_in_quote(facts: ClaimFacts, _today: date) -> CheckResult:
    text = _date_text(facts)
    if text is None:
        return _failed("date_words_in_quote", "the claim has no date words")
    if not has_words(facts.quote, text):
        return _failed("date_words_in_quote", f"the date words '{text}' are not in the quote")
    return _passed("date_words_in_quote")


def _window_matches_words(facts: ClaimFacts, _today: date) -> CheckResult:
    text = _date_text(facts)
    if text is None:
        return _failed("window_matches_words", "the claim has no date words")
    window = parse_window(text, facts.published)
    as_of = f"'{text}' as of {facts.published.isoformat()}"
    if window is None:
        return _failed("window_matches_words", f"{as_of} names no window")
    if window.end < facts.published:
        return _failed("window_matches_words", f"{as_of} names a window that had already ended")
    stored = (
        _day(facts.value.get("window_start")),
        _day(facts.value.get("window_end")),
        facts.value.get("window_precision"),
    )
    if stored != (window.start, window.end, window.precision.value):
        start, end, precision = (str(part) for part in stored)
        return _failed(
            "window_matches_words",
            f"{as_of} reads as {window.start.isoformat()} to {window.end.isoformat()} "
            f"({window.precision.value}), not the stored {start} to {end} ({precision})",
        )
    return _passed("window_matches_words")


def _has_window(facts: ClaimFacts, _today: date) -> CheckResult:
    if _day(facts.value.get("window_start")) and _day(facts.value.get("window_end")):
        return _passed("has_window")
    return _failed("has_window", "the date words name no window")


def _window_not_past(facts: ClaimFacts, today: date) -> CheckResult:
    end = _day(facts.value.get("window_end"))
    if end is None:
        return _not_applicable("window_not_past", "no window")
    if end < today:
        return _failed(
            "window_not_past", f"the window ended on {end.isoformat()}, before {today.isoformat()}"
        )
    return _passed("window_not_past")


def _names_matched(facts: ClaimFacts, _today: date) -> CheckResult:
    if not any(m.entity == "company" for m in facts.matches):
        return _failed("names_matched", "no company is linked to the source document")
    open_names = [f"{m.entity} {m.status}" for m in facts.matches if m.status != "resolved"]
    if open_names:
        return _failed("names_matched", "not matched to exactly one record: " + ", ".join(open_names))
    return _passed("names_matched")


_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}
# "Phase 1/2", "Phase 2-3" and "Phase 2\u20133" are one combined phase; "Phase 3-ready" is Phase 3.
_PHASE = re.compile(
    r"\bphase\s*(?P<first>iv|i{1,3}|[1-4])[ab]?"
    r"(?:\s*[/\-\u2010-\u2015]\s*(?P<second>i{1,3}|[1-3])[ab]?(?![a-z0-9]))?(?![a-z0-9])",
    re.IGNORECASE,
)
_SINGLE = {1: TrialPhase.PHASE_1, 2: TrialPhase.PHASE_2, 3: TrialPhase.PHASE_3, 4: TrialPhase.PHASE_4}
_COMBINED = {(1, 2): TrialPhase.PHASE_1_2, (2, 3): TrialPhase.PHASE_2_3}
_PARTS: dict[TrialPhase, frozenset[int]] = {
    TrialPhase.EARLY_PHASE_1: frozenset({1}),
    TrialPhase.PHASE_1: frozenset({1}),
    TrialPhase.PHASE_1_2: frozenset({1, 2}),
    TrialPhase.PHASE_2: frozenset({2}),
    TrialPhase.PHASE_2_3: frozenset({2, 3}),
    TrialPhase.PHASE_3: frozenset({3}),
    TrialPhase.PHASE_4: frozenset({4}),
    TrialPhase.NOT_APPLICABLE: frozenset(),
}
_WORDS = {
    TrialPhase.EARLY_PHASE_1: "Early Phase 1",
    TrialPhase.PHASE_1: "Phase 1",
    TrialPhase.PHASE_1_2: "Phase 1/2",
    TrialPhase.PHASE_2: "Phase 2",
    TrialPhase.PHASE_2_3: "Phase 2/3",
    TrialPhase.PHASE_3: "Phase 3",
    TrialPhase.PHASE_4: "Phase 4",
    TrialPhase.NOT_APPLICABLE: "no phase",
}


def _number(text: str) -> int:
    return _ROMAN[text.lower()] if text.lower() in _ROMAN else int(text)


def stated_phases(text: str) -> set[TrialPhase]:
    """Return every trial phase the text names: 'Phase 3', 'Phase III', 'Phase 2b', 'Phase 1/2'."""
    phases: set[TrialPhase] = set()
    for match in _PHASE.finditer(text):
        first = _number(match.group("first"))
        second = match.group("second")
        combined = _COMBINED.get((first, _number(second))) if second else None
        phases.add(combined or _SINGLE[first])
    return phases


def phases_overlap(stated: TrialPhase, registered: TrialPhase) -> bool:
    """Say whether two phases share a number: Phase 3 overlaps Phase 2/3; no phase overlaps nothing."""
    return bool(_PARTS[stated] & _PARTS[registered])


def _phase_matches_registry(facts: ClaimFacts, _today: date) -> CheckResult:
    stated = stated_phases(facts.quote)
    registered = [
        facts.trial_phases.get(m.matched_id)
        for m in facts.matches
        if m.entity == "trial" and m.status == "resolved" and m.matched_id
    ]
    known = [phase for phase in registered if phase is not None]
    if not stated or not known:
        return _not_applicable("phase_matches_registry", "no stated phase, or no matched trial with a phase")
    for phase in stated:
        if not any(phases_overlap(phase, trial) for trial in known):
            names = ", ".join(sorted({_WORDS[p] for p in known}))
            return _failed(
                "phase_matches_registry", f"the quote says {_WORDS[phase]}; the registry says {names}"
            )
    return _passed("phase_matches_registry")


def _trial_named_in_quote(facts: ClaimFacts, _today: date) -> CheckResult:
    """Check that every trial the claim is matched to is one its quote names.

    A trial counts as named by its registry number or its study name, found the way the resolver
    finds them. Unknown and ambiguous trial names are left to `names_matched`.
    """
    resolved = [m.matched_id for m in facts.matches if m.entity == "trial" and m.status == "resolved"]
    if not resolved:
        return _not_applicable("trial_named_in_quote", "no matched trial")
    for trial in resolved:
        names = facts.trial_names.get(trial) if trial is not None else None
        if names is None:
            return _failed("trial_named_in_quote", "a matched trial has no registry record")
        if not names_trial(facts.quote, names.nct_id, names.study_name):
            shown = f"{names.nct_id} ({names.study_name})" if names.study_name else names.nct_id
            return _failed("trial_named_in_quote", f"the quote does not name the matched trial {shown}")
    return _passed("trial_named_in_quote")


def months_after(start: date, months: int) -> date:
    """Return the same day `months` later, or that month's last day if it is shorter (Oct 31 → Jan 31)."""
    month = start.month - 1 + months
    year, month = start.year + month // 12, month % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def _pdufa_date_plausible(facts: ClaimFacts, _today: date) -> CheckResult:
    """Check that an FDA decision date can fall after the release and within 15 months of it.

    A release may give a single day, or only the month or quarter.
    """
    if facts.value.get("catalyst_type") != "pdufa_date":
        return _not_applicable("pdufa_date_plausible", "not an FDA decision date")
    start, end = _day(facts.value.get("window_start")), _day(facts.value.get("window_end"))
    if start is None or end is None:
        return _failed("pdufa_date_plausible", "the decision date has no window")
    if end <= facts.published:
        return _failed("pdufa_date_plausible", "the decision date is not after the release")
    latest = months_after(facts.published, PDUFA_MONTHS)
    if start > latest:
        return _failed(
            "pdufa_date_plausible",
            f"the decision date is more than {PDUFA_MONTHS} months after the release "
            f"(latest {latest.isoformat()})",
        )
    return _passed("pdufa_date_plausible")


RULES: tuple[Callable[[ClaimFacts, date], CheckResult], ...] = (
    _extractor_checks,
    _date_words_in_quote,
    _window_matches_words,
    _has_window,
    _window_not_past,
    _names_matched,
    _phase_matches_registry,
    _trial_named_in_quote,
    _pdufa_date_plausible,
)


def rule_checks(facts: ClaimFacts, today: date) -> list[CheckResult]:
    """Run every rule check on one claim."""
    return [rule(facts, today) for rule in RULES]
