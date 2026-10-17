"""Rule checks: plain code that every claim must pass before it can publish (ADR 0018).

The rules come from regulation and from our own records, never from a model's output. Each check
answers pass, fail with a reason a person can read, or not applicable.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, cast
from uuid import UUID

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
class ClaimFacts:
    """Everything the rule checks read about one claim."""

    id: UUID
    value: Mapping[str, Any]
    quote: str
    published: date
    matches: tuple[Match, ...]
    trial_phases: Mapping[UUID, TrialPhase | None]


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
    return date.fromisoformat(value) if isinstance(value, str) else None


def _extractor_checks(facts: ClaimFacts, _today: date) -> CheckResult:
    listed: object = facts.value.get("problems")
    problems = [str(p) for p in cast(list[object], listed)] if isinstance(listed, list) else []
    if problems:
        return _failed("extractor_checks", "the extractor flagged: " + ", ".join(problems))
    return _passed("extractor_checks")


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
_PHASE = re.compile(
    r"\bphase\s*(?P<first>iv|i{1,3}|[1-4])[ab]?(?:\s*/\s*(?P<second>i{1,3}|[1-3])[ab]?)?(?![a-z0-9])",
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
        if not any(_PARTS[phase] & _PARTS[trial] for trial in known):
            names = ", ".join(sorted(p.value for p in known))
            return _failed(
                "phase_matches_registry", f"the quote says {phase.value}; the registry says {names}"
            )
    return _passed("phase_matches_registry")


def _months_after(start: date, months: int) -> date:
    month = start.month - 1 + months
    year, month = start.year + month // 12, month % 12 + 1
    return date(year, month, min(start.day, 28))


def _pdufa_date_plausible(facts: ClaimFacts, _today: date) -> CheckResult:
    if facts.value.get("catalyst_type") != "pdufa_date":
        return _not_applicable("pdufa_date_plausible", "not an FDA decision date")
    day = _day(facts.value.get("window_start"))
    if facts.value.get("window_precision") != "day" or day is None:
        return _failed("pdufa_date_plausible", "an FDA decision date is a single day")
    if day <= facts.published:
        return _failed("pdufa_date_plausible", "the decision date is not after the release")
    latest = _months_after(facts.published, PDUFA_MONTHS)
    if day > latest:
        return _failed(
            "pdufa_date_plausible",
            f"the decision date is more than {PDUFA_MONTHS} months after the release "
            f"(latest {latest.isoformat()})",
        )
    return _passed("pdufa_date_plausible")


RULES: tuple[Callable[[ClaimFacts, date], CheckResult], ...] = (
    _extractor_checks,
    _has_window,
    _window_not_past,
    _names_matched,
    _phase_matches_registry,
    _pdufa_date_plausible,
)


def rule_checks(facts: ClaimFacts, today: date) -> list[CheckResult]:
    """Run every rule check on one claim."""
    return [rule(facts, today) for rule in RULES]
