"""Rule checks: plain-code tests every claim must pass before it can publish (spec: Verification; ADR 0018).

Each check answers pass, fail (with a reason a person can read), or not applicable. The rules
come from regulation and from our own records, never from a model's output.
"""

from datetime import date
from typing import Any
from uuid import uuid4

import pytest

from catalyst.schemas import TrialPhase
from catalyst.verify.rules import ClaimFacts, Match, Outcome, rule_checks, stated_phases

TODAY = date(2026, 10, 17)
PUBLISHED = date(2026, 10, 1)
COMPANY, DRUG, TRIAL = uuid4(), uuid4(), uuid4()


def _value(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "catalyst_type": "topline_readout",
        "drug": "SPY001",
        "date_text": "Q4 2026",
        "window_start": "2026-10-01",
        "window_end": "2026-12-31",
        "window_precision": "quarter",
        "problems": [],
    }
    return value | changes


def _facts(quote: str = "Phase 3 AZURE-1 topline data expected in Q4 2026.", **changes: Any) -> ClaimFacts:
    matches = changes.pop(
        "matches",
        (
            Match("company", "resolved", COMPANY),
            Match("drug", "resolved", DRUG),
            Match("trial", "resolved", TRIAL),
        ),
    )
    phases = changes.pop("trial_phases", {TRIAL: TrialPhase.PHASE_3})
    return ClaimFacts(
        id=uuid4(),
        value=_value(**changes),
        quote=quote,
        published=PUBLISHED,
        matches=matches,
        trial_phases=phases,
    )


def _outcomes(facts: ClaimFacts) -> dict[str, tuple[Outcome, str | None]]:
    return {c.check: (c.outcome, c.reason) for c in rule_checks(facts, TODAY)}


def test_a_clean_claim_passes_every_check() -> None:
    outcomes = _outcomes(_facts())
    assert {name for name, (outcome, _) in outcomes.items() if outcome is Outcome.FAIL} == set()
    assert set(outcomes) == {
        "extractor_checks",
        "has_window",
        "window_not_past",
        "names_matched",
        "phase_matches_registry",
        "pdufa_date_plausible",
    }


def test_a_problem_the_extractor_found_fails_with_its_reason() -> None:
    outcome, reason = _outcomes(_facts(problems=["date_not_in_quote"]))["extractor_checks"]
    assert outcome is Outcome.FAIL
    assert reason == "the extractor flagged: date_not_in_quote"


def test_no_window_fails_and_the_past_check_does_not_apply() -> None:
    outcomes = _outcomes(_facts(window_start=None, window_end=None, window_precision=None))
    assert outcomes["has_window"][0] is Outcome.FAIL
    assert outcomes["window_not_past"][0] is Outcome.NOT_APPLICABLE


def test_a_window_that_ended_before_today_fails() -> None:
    outcome, reason = _outcomes(_facts(window_start="2026-07-01", window_end="2026-09-30"))["window_not_past"]
    assert (outcome, reason) == (Outcome.FAIL, "the window ended on 2026-09-30, before 2026-10-17")


@pytest.mark.parametrize(
    ("entity", "status"),
    [("company", "unknown"), ("drug", "ambiguous"), ("trial", "unknown")],
)
def test_any_name_not_matched_to_exactly_one_record_fails(entity: str, status: str) -> None:
    matches = tuple(
        Match(e, status if e == entity else "resolved", None if e == entity else uuid4())
        for e in ("company", "drug", "trial")
    )
    outcome, reason = _outcomes(_facts(matches=matches))["names_matched"]
    assert outcome is Outcome.FAIL
    assert reason is not None
    assert entity in reason
    assert status in reason


def test_a_claim_with_no_company_match_at_all_fails() -> None:
    assert _outcomes(_facts(matches=()))["names_matched"][0] is Outcome.FAIL


@pytest.mark.parametrize(
    ("quote", "registry", "expected"),
    [
        ("Phase 3 AZURE-1 data are expected in Q4 2026.", TrialPhase.PHASE_3, Outcome.PASS),
        ("Phase III AZURE-1 data are expected in Q4 2026.", TrialPhase.PHASE_3, Outcome.PASS),
        ("Phase 2b AZURE-1 data are expected in Q4 2026.", TrialPhase.PHASE_2, Outcome.PASS),
        ("Phase 2 AZURE-1 data are expected in Q4 2026.", TrialPhase.PHASE_2_3, Outcome.PASS),
        ("Phase 1/2 AZURE-1 data are expected in Q4 2026.", TrialPhase.PHASE_1_2, Outcome.PASS),
        ("Phase 3 AZURE-1 data are expected in Q4 2026.", TrialPhase.PHASE_2, Outcome.FAIL),
        ("Phase 1 AZURE-1 data are expected in Q4 2026.", TrialPhase.PHASE_3, Outcome.FAIL),
        ("AZURE-1 data are expected in Q4 2026.", TrialPhase.PHASE_3, Outcome.NOT_APPLICABLE),
        ("Phase 3 AZURE-1 data are expected in Q4 2026.", None, Outcome.NOT_APPLICABLE),
    ],
)
def test_a_phase_in_the_quote_must_agree_with_the_registry(
    quote: str, registry: TrialPhase | None, expected: Outcome
) -> None:
    outcome, _ = _outcomes(_facts(quote, trial_phases={TRIAL: registry}))["phase_matches_registry"]
    assert outcome is expected


def test_phase_words_are_read_in_every_common_spelling() -> None:
    assert stated_phases("a Phase 2a and a phase III trial, plus a Phase 1/2 study") == {
        TrialPhase.PHASE_2,
        TrialPhase.PHASE_3,
        TrialPhase.PHASE_1_2,
    }


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"window_start": "2027-03-03", "window_end": "2027-03-03", "window_precision": "day"}, Outcome.PASS),
        ({"window_start": "2028-03-03", "window_end": "2028-03-03", "window_precision": "day"}, Outcome.FAIL),
        ({"window_start": "2026-09-01", "window_end": "2026-09-01", "window_precision": "day"}, Outcome.FAIL),
        (
            {"window_start": "2027-01-01", "window_end": "2027-03-31", "window_precision": "quarter"},
            Outcome.FAIL,
        ),
    ],
    ids=["five months out", "seventeen months out", "before the release", "a quarter, not a day"],
)
def test_a_pdufa_date_is_a_day_after_the_release_and_within_fifteen_months(
    changes: dict[str, Any], expected: Outcome
) -> None:
    facts = _facts("The PDUFA target action date is set.", catalyst_type="pdufa_date", **changes)
    assert _outcomes(facts)["pdufa_date_plausible"][0] is expected


def test_the_pdufa_rule_does_not_apply_to_other_catalysts() -> None:
    assert _outcomes(_facts())["pdufa_date_plausible"][0] is Outcome.NOT_APPLICABLE
