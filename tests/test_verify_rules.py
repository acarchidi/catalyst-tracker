"""Rule checks: plain-code tests every claim must pass before it can publish (spec: Verification; ADR 0018).

Each check answers pass, fail (with a reason a person can read), or not applicable. The rules
come from regulation and from our own records, never from a model's output.
"""

from dataclasses import replace
from datetime import date
from typing import Any
from uuid import uuid4

import pytest

from catalyst.schemas import TrialPhase
from catalyst.verify.rules import ClaimFacts, Match, Outcome, TrialNames, rule_checks, stated_phases

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
    names = changes.pop("trial_names", {TRIAL: TrialNames("NCT06111111", "AZURE-1")})
    return ClaimFacts(
        id=uuid4(),
        value=_value(**changes),
        quote=quote,
        published=PUBLISHED,
        matches=matches,
        trial_phases=phases,
        trial_names=names,
    )


def _outcomes(facts: ClaimFacts) -> dict[str, tuple[Outcome, str | None]]:
    return {c.check: (c.outcome, c.reason) for c in rule_checks(facts, TODAY)}


def test_a_clean_claim_passes_every_check() -> None:
    outcomes = _outcomes(_facts())
    assert {name for name, (outcome, _) in outcomes.items() if outcome is Outcome.FAIL} == set()
    assert set(outcomes) == {
        "extractor_checks",
        "date_words_in_quote",
        "window_matches_words",
        "has_window",
        "window_not_past",
        "names_matched",
        "phase_matches_registry",
        "trial_named_in_quote",
        "pdufa_date_plausible",
    }


def test_a_problem_the_extractor_found_fails_with_its_reason() -> None:
    outcome, reason = _outcomes(_facts(problems=["date_not_in_quote"]))["extractor_checks"]
    assert outcome is Outcome.FAIL
    assert reason == "the extractor flagged: date_not_in_quote"


@pytest.mark.parametrize(
    "problems", [None, "date_passed", {"date_passed": True}], ids=["missing", "text", "map"]
)
def test_a_claim_without_a_list_of_extractor_problems_fails(problems: object) -> None:
    facts = _facts(problems=problems)
    if problems is None:
        facts = replace(facts, value={k: v for k, v in facts.value.items() if k != "problems"})
    outcome, reason = _outcomes(facts)["extractor_checks"]
    assert outcome is Outcome.FAIL
    assert reason == "the claim has no list of the extractor's problems"


def test_date_words_missing_from_the_quote_fail_even_if_the_extractor_saw_no_problem() -> None:
    facts = _facts("Phase 3 AZURE-1 topline data expected later this year.")
    outcome, reason = _outcomes(facts)["date_words_in_quote"]
    assert (outcome, reason) == (Outcome.FAIL, "the date words 'Q4 2026' are not in the quote")


def test_date_words_that_name_no_window_fail() -> None:
    facts = _facts("Phase 3 AZURE-1 topline data expected soon.", date_text="soon")
    outcome, reason = _outcomes(facts)["window_matches_words"]
    assert (outcome, reason) == (Outcome.FAIL, "'soon' as of 2026-10-01 names no window")


@pytest.mark.parametrize("date_text", [None, "", 7], ids=["missing", "empty", "number"])
def test_a_claim_without_date_words_fails(date_text: object) -> None:
    assert _outcomes(_facts(date_text=date_text))["date_words_in_quote"][0] is Outcome.FAIL


@pytest.mark.parametrize(
    "changes",
    [
        {"window_start": "2027-01-01", "window_end": "2027-03-31"},
        {"window_precision": "month"},
        {"window_end": "2026-12-30"},
    ],
    ids=["a different quarter", "a different precision", "a shifted end"],
)
def test_a_stored_window_that_the_date_words_do_not_give_fails(changes: dict[str, Any]) -> None:
    outcome, reason = _outcomes(_facts(**changes))["window_matches_words"]
    assert outcome is Outcome.FAIL
    assert reason is not None
    assert reason.startswith("'Q4 2026' as of 2026-10-01 reads as 2026-10-01 to 2026-12-31 (quarter)")


def test_date_words_already_past_at_publication_fail() -> None:
    facts = _facts(
        "Phase 3 AZURE-1 topline data expected in Q3 2026.",
        date_text="Q3 2026",
        window_start="2026-07-01",
        window_end="2026-09-30",
    )
    outcome, reason = _outcomes(facts)["window_matches_words"]
    assert (outcome, reason) == (
        Outcome.FAIL,
        "'Q3 2026' as of 2026-10-01 names a window that had already ended",
    )


def test_a_malformed_stored_date_fails_instead_of_stopping_the_run() -> None:
    outcomes = _outcomes(_facts(window_end="2026-13-45"))
    assert outcomes["has_window"][0] is Outcome.FAIL
    assert outcomes["window_matches_words"][0] is Outcome.FAIL


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
        ("Phase 2\u20133 AZURE-1 data are expected in Q4 2026.", TrialPhase.PHASE_3, Outcome.PASS),
        ("Phase 2-3 AZURE-1 data are expected in Q4 2026.", TrialPhase.PHASE_3, Outcome.PASS),
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
    assert stated_phases("a Phase 2\u20133 study and a Phase I-II study") == {
        TrialPhase.PHASE_2_3,
        TrialPhase.PHASE_1_2,
    }
    assert stated_phases("a Phase 3-ready program") == {TrialPhase.PHASE_3}


def test_a_phase_disagreement_is_explained_in_plain_words() -> None:
    facts = _facts("Phase 1/2 AZURE-1 data are expected in Q4 2026.")
    _, reason = _outcomes(facts)["phase_matches_registry"]
    assert reason == "the quote says Phase 1/2; the registry says Phase 3"


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"window_start": "2027-03-03", "window_end": "2027-03-03", "window_precision": "day"}, Outcome.PASS),
        ({"window_start": "2028-03-03", "window_end": "2028-03-03", "window_precision": "day"}, Outcome.FAIL),
        ({"window_start": "2026-09-01", "window_end": "2026-09-01", "window_precision": "day"}, Outcome.FAIL),
        (
            {"window_start": "2027-01-01", "window_end": "2027-03-31", "window_precision": "quarter"},
            Outcome.PASS,
        ),
        (
            {"window_start": "2028-04-01", "window_end": "2028-06-30", "window_precision": "quarter"},
            Outcome.FAIL,
        ),
        (
            {"window_start": "2026-07-01", "window_end": "2026-09-30", "window_precision": "quarter"},
            Outcome.FAIL,
        ),
    ],
    ids=[
        "five months out",
        "seventeen months out",
        "before the release",
        "a quarter within fifteen months",
        "a quarter more than fifteen months out",
        "a quarter before the release",
    ],
)
def test_a_pdufa_date_falls_after_the_release_and_within_fifteen_months(
    changes: dict[str, Any], expected: Outcome
) -> None:
    facts = _facts("The PDUFA target action date is set.", catalyst_type="pdufa_date", **changes)
    assert _outcomes(facts)["pdufa_date_plausible"][0] is expected


@pytest.mark.parametrize(
    ("day", "expected"),
    [(date(2028, 1, 31), Outcome.PASS), (date(2028, 2, 1), Outcome.FAIL)],
    ids=["the last day of the fifteenth month", "the day after"],
)
def test_the_fifteen_month_limit_runs_to_the_end_of_a_short_month(day: date, expected: Outcome) -> None:
    window = {"window_start": day.isoformat(), "window_end": day.isoformat(), "window_precision": "day"}
    facts = _facts("The PDUFA target action date is set.", catalyst_type="pdufa_date", **window)
    assert _outcomes(replace(facts, published=date(2026, 10, 31)))["pdufa_date_plausible"][0] is expected


def test_the_pdufa_rule_does_not_apply_to_other_catalysts() -> None:
    assert _outcomes(_facts())["pdufa_date_plausible"][0] is Outcome.NOT_APPLICABLE


# trial_named_in_quote: a matched trial must be one the quote names (planted-error test).
# The study names and registry numbers below are Spyre's and Mirum's, as ClinicalTrials.gov has them.
SKYLINE_UC = TrialNames("NCT07012395", "SKYLINE-UC")
VANTAGE = TrialNames("NCT05050136", "VANTAGE")
IMAGINE, IMAGINE_II = TrialNames("NCT02047318", "IMAGINE"), TrialNames("NCT02117713", "IMAGINE-II")
VANTAGE_QUOTE = (
    "Completed enrollment in the volixibat VANTAGE Phase 2b study in cholestatic pruritus due to "
    "primary biliary cholangitis (PBC); topline results expected in Q1 2027."
)


def _trial_check(quote: str, *trials: TrialNames) -> tuple[Outcome, str | None]:
    ids = [uuid4() for _ in trials]
    matches = (Match("company", "resolved", COMPANY), *(Match("trial", "resolved", i) for i in ids))
    facts = _facts(quote, matches=matches, trial_phases={}, trial_names=dict(zip(ids, trials, strict=True)))
    return _outcomes(facts)["trial_named_in_quote"]


@pytest.mark.parametrize(
    ("quote", "trial"),
    [
        (VANTAGE_QUOTE, VANTAGE),
        ("SKYLINE-UC Part A topline data for SPY003 are expected in Q4 2026.", SKYLINE_UC),
        ("SKYLINE UC Part A topline data for SPY003 are expected in Q4 2026.", SKYLINE_UC),
        ("Topline data from NCT07012395 are expected in Q4 2026.", SKYLINE_UC),
        ("Topline data from nct07012395 are expected in Q4 2026.", SKYLINE_UC),
        ("Topline data from NCT07012395 are expected in Q4 2026.", TrialNames("NCT07012395", None)),
        ("IMAGINE-II topline data are expected in Q4 2026.", IMAGINE_II),
    ],
    ids=["study name", "dashed study name", "spaced study name", "number", "lower-case number",
         "number, no study name", "longer of two names"],
)  # fmt: skip
def test_a_matched_trial_named_in_the_quote_passes(quote: str, trial: TrialNames) -> None:
    assert _trial_check(quote, trial) == (Outcome.PASS, None)


@pytest.mark.parametrize(
    ("quote", "trial"),
    [
        (VANTAGE_QUOTE, IMAGINE_II),  # another of the company's trials, swapped in
        ("SKYLINE Part A Ulcerative Colitis SPY003 Sept 2026", SKYLINE_UC),  # the release's short name
        ("IMAGINE-II topline data are expected in Q4 2026.", IMAGINE),  # part of a longer study name
        ("Imagine topline data are expected in Q4 2026.", IMAGINE),  # an ordinary word, not the study
        ("VANTAGE2 topline data are expected in Q4 2026.", VANTAGE),
        ("Topline data from NCT050501360 are expected in Q4 2026.", VANTAGE),  # nine digits
        ("Topline data from NCT05050137 are expected in Q4 2026.", VANTAGE),
        ("Topline data from the SKYLINE trial are expected in Q4 2026.", TrialNames("NCT07012395", None)),
    ],
    ids=["another trial", "near-miss short name", "near-miss longer name", "lower-case word",
         "joined to a digit", "longer number", "another number", "no study name on file"],
)  # fmt: skip
def test_a_matched_trial_the_quote_does_not_name_fails(quote: str, trial: TrialNames) -> None:
    outcome, reason = _trial_check(quote, trial)
    assert outcome is Outcome.FAIL
    assert reason is not None
    assert trial.nct_id in reason


def test_every_matched_trial_must_be_named() -> None:
    outcome, reason = _trial_check(VANTAGE_QUOTE, VANTAGE, IMAGINE_II)
    assert outcome is Outcome.FAIL
    assert reason == "the quote does not name the matched trial NCT02117713 (IMAGINE-II)"


def test_a_matched_trial_with_no_registry_record_fails() -> None:
    matches = (Match("company", "resolved", COMPANY), Match("trial", "resolved", uuid4()))
    outcome, reason = _outcomes(_facts(VANTAGE_QUOTE, matches=matches, trial_names={}))[
        "trial_named_in_quote"
    ]
    assert (outcome, reason) == (Outcome.FAIL, "a matched trial has no registry record")


def test_a_resolved_trial_match_without_an_id_fails() -> None:
    matches = (Match("company", "resolved", COMPANY), Match("trial", "resolved", None))
    assert _outcomes(_facts(VANTAGE_QUOTE, matches=matches))["trial_named_in_quote"][0] is Outcome.FAIL


@pytest.mark.parametrize("status", [None, "unknown", "ambiguous"], ids=["no trial", "unknown", "ambiguous"])
def test_without_a_resolved_trial_the_check_does_not_apply(status: str | None) -> None:
    """names_matched already fails an unknown or ambiguous trial; this check is about resolved ones."""
    trials = () if status is None else (Match("trial", status, None),)
    facts = _facts(VANTAGE_QUOTE, matches=(Match("company", "resolved", COMPANY), *trials))
    outcomes = _outcomes(facts)
    assert outcomes["trial_named_in_quote"][0] is Outcome.NOT_APPLICABLE
    assert outcomes["names_matched"][0] is (Outcome.PASS if status is None else Outcome.FAIL)
