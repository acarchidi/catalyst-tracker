"""The planted-error test, part two: every check on clean and planted claims, and the owner's page.

Tests are judged against ADR 0018 part 4: caught means at least one check failed the planted
record; corrected means the second reading gave the right value with its own quote; missed means
every check passed it; clean records wrongly flagged are counted. The floor test is the CI gate:
no error type's catch rate may fall below `evals/planted_errors/baseline.json`, and no more clean
records may be wrongly flagged than it allows.
"""

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from functools import cache
from pathlib import Path
from uuid import UUID

from catalyst.cli import main
from catalyst.extract.dates import cut_short
from catalyst.schemas import SourceTier
from catalyst.verify.independent import independent_sources
from catalyst.verify.planted import ERRORS, FOLDER, Corpus, Record, load_corpus, plant
from catalyst.verify.planted_report import (
    CHECKS,
    Results,
    floor_problems,
    render_report,
    run,
    scores,
    statement_of,
)
from catalyst.verify.rules import CheckResult, Outcome


@cache
def _corpus() -> Corpus:
    return load_corpus()


@cache
def _results() -> Results:
    return run(_corpus())


def _record(drug: str, date_text: str) -> Record:
    return next(r for r in _corpus().records if r.value["drug"] == drug and r.value["date_text"] == date_text)


def test_the_same_seed_gives_the_same_results_every_run() -> None:
    assert scores(run(_corpus())) == scores(_results())
    assert render_report(run(_corpus()), _corpus()) == render_report(_results(), _corpus())


def test_a_wrong_window_is_caught_and_the_second_reading_corrects_it_with_its_own_quote() -> None:
    original = _record("LIVMARLI", "Q4 2026")
    found = next(p for p in _results().planted if p.error == "wrong_window" and p.original == original)
    assert "window_matches_words" in found.failed
    assert "second_reading_agrees" in found.failed
    assert found.correction is not None
    assert found.correction.quote == original.quote
    assert "window 2026-10-01 to 2026-12-31 (quarter)" in found.correction.value


def test_an_error_the_second_reading_cannot_see_is_never_counted_as_corrected() -> None:
    for found in _results().planted:
        if found.error in {"wrong_company", "wrong_trial", "wrong_phase"}:
            assert found.correction is None
            assert "second_reading_agrees" not in found.failed


def test_caught_corrected_and_missed_follow_their_definitions() -> None:
    for found in _results().planted:
        assert found.caught == bool(found.failed)
        if found.correction is not None:
            assert "second_reading_agrees" in found.failed


def test_a_check_added_to_the_list_runs_on_every_record() -> None:
    clean = {r.id for r in _corpus().records}

    def fails_planted(records: Sequence[Record], _corpus: Corpus) -> dict[UUID, list[CheckResult]]:
        return {
            r.id: [CheckResult("test_check", Outcome.FAIL, "planted")] for r in records if r.id not in clean
        }

    def fails_all(records: Sequence[Record], _corpus: Corpus) -> dict[UUID, list[CheckResult]]:
        return {r.id: [CheckResult("test_check", Outcome.FAIL, "every record")] for r in records}

    caught = run(_corpus(), checks=(*CHECKS, fails_planted))
    assert caught.planted
    assert all(found.caught for found in caught.planted)
    assert "test_check" in caught.checks
    flagged = run(_corpus(), checks=(*CHECKS, fails_all))
    assert len(flagged.flagged) == flagged.clean_checked
    assert flagged.planted == ()  # an error is planted only in a record that passes every check


def test_a_record_flagged_when_correct_gets_no_planted_error_and_is_reported() -> None:
    flagged = {(r.value["drug"], r.value["date_text"]) for r, _ in _results().flagged}
    # SKYLINE Part B tests six drugs, so its gold drug is empty (owner ruling 3), and the second
    # reading never agrees on a missing drug (ADR 0018, part 2).
    assert flagged == {(None, "2027")}
    assert len(_results().flagged) == 2
    assert not any(found.original.value["drug"] is None for found in _results().planted)
    assert "no drug is named (the trial tests several)" in render_report(_results(), _corpus())


def test_every_missed_case_is_listed_with_the_reason() -> None:
    report = render_report(_results(), _corpus())
    missed = [p for p in _results().planted if not p.caught]
    for error in ERRORS:
        if any(p.error == error.name for p in missed):
            assert error.blind_spot in report
    # One line per event an error type was missed on, with how many of its copies were missed.
    for error, original in {(p.error, p.original.id) for p in missed}:
        found = [p for p in _results().planted if p.error == error and p.original.id == original]
        lost = sum(not p.caught for p in found)
        assert f": {lost} of {len(found)} missed, for example " in report


def test_scores_count_every_planted_record_once() -> None:
    counted = scores(_results())
    for name, row in counted["errors"].items():
        assert row["caught"] + row["missed"] == row["planted"]
        assert row["corrected"] <= row["caught"]
        assert row["planted"] == sum(p.error == name for p in _results().planted)


def test_a_drop_below_the_floor_or_more_wrongly_flagged_clean_records_is_refused() -> None:
    counted = scores(_results())
    every = dict.fromkeys(counted["errors"], 1.0)
    floor = {
        "catch_rate": every,
        "rule_catch_rate": every,
        "clean_wrongly_flagged": 0,
        "clean_neighbours_wrongly_flagged": 0,
    }
    problems = floor_problems(counted, floor)
    assert "quote_same_date: catch_rate fell to 0.8, below the floor 1.0" in problems
    assert "2 clean records wrongly flagged, above the floor 0" in problems
    missing = floor_problems(counted, {})
    assert len(missing) == 3 * len(ERRORS) + 2
    too_few = floor_problems(counted, {**floor, "planted": {"pdufa_outside_review": 2}})
    assert "pdufa_outside_review: only 1 copies planted, below the floor 2" in too_few


# The CI gate (AGENTS.md: CI blocks a drop). The floor is owner-only once it joins OWNER_PATHS.
def test_todays_checks_hold_the_committed_floor() -> None:
    baseline = json.loads((FOLDER / "baseline.json").read_text(encoding="utf-8"))
    assert floor_problems(scores(_results()), baseline) == []


def test_the_committed_report_and_scores_are_current() -> None:
    committed = json.loads((FOLDER / "scores.json").read_text(encoding="utf-8"))
    assert committed == scores(_results())
    assert (FOLDER / "report.md").read_text(encoding="utf-8") == render_report(_results(), _corpus())


def test_the_command_writes_the_report_and_the_scores(tmp_path: Path) -> None:
    out, counts = tmp_path / "report.md", tmp_path / "scores.json"
    assert main(["planted-errors", "--out", str(out), "--scores", str(counts)]) == 0
    assert out.read_text(encoding="utf-8") == render_report(_results(), _corpus())
    assert json.loads(counts.read_text(encoding="utf-8")) == scores(_results())


def test_every_gold_release_is_an_sec_filing_so_the_independent_source_check_passes_it() -> None:
    # The publish rule lets a regulator or SEC filing stand alone (spec: Verification); every gold
    # release is an 8-K exhibit, so this check cannot catch any planted error here.
    assert all(found.error and "independent_source" not in found.failed for found in _results().planted)
    assert "independent_source" in _results().checks


def test_a_planted_copy_never_vouches_for_its_original_or_itself() -> None:
    corpus = _corpus()
    original = _record("LIVMARLI", "Q4 2026")
    copy = plant(next(e for e in ERRORS if e.name == "wrong_trial"), original, corpus)[0]
    statements = [
        replace(statement_of(r, corpus, clean=True), tier=SourceTier.COMPANY_SITE) for r in (original, copy)
    ]
    found = independent_sources(statements)
    assert found[original.id].outcome is Outcome.FAIL
    assert found[copy.id].outcome is Outcome.FAIL


def test_the_seed_used_is_carried_into_the_scores() -> None:
    assert scores(_results())["seed"] == 2026
    assert scores(run(_corpus(), seed=7))["seed"] == 7


def test_a_quote_from_another_drugs_event_with_the_same_date_words_is_caught() -> None:
    # The Opus review of PR #51 found these all missed: no check asked whether the quote names the
    # claim's drug. drug_named_near_quote (part 3e) now does. The one copy still missed is the
    # sister sub-study of the same drug: SPY072 is named just before its sentence, and its stored
    # values (SPY072, topline, Q4 2026) are true of that sentence too.
    same = [p for p in _results().planted if p.error == "quote_same_date"]
    assert len(same) == 5
    caught = [p for p in same if p.caught]
    assert len(caught) == 4
    assert all("drug_named_near_quote" in p.failed for p in caught)
    (missed,) = [p for p in same if not p.caught]
    assert (missed.original.value["drug"], missed.original.value["date_text"]) == (
        "SPY072",
        "fourth quarter of 2026",
    )
    assert missed.record.quote == "Topline proof-of-concept data are expected in the fourth quarter of 2026."
    assert "Quote from another event with the same date words** (1 of 5 missed)" in render_report(
        _results(), _corpus()
    )


def test_the_drug_check_is_run_and_flags_no_correct_claim() -> None:
    assert "drug_named_near_quote" in _results().checks
    assert all(
        "drug_named_near_quote" not in reason for _, reasons in _results().flagged for reason in reasons
    )
    assert _results().disturbed == ()


def test_the_type_check_is_run_flags_no_correct_claim_and_catches_most_wrong_types() -> None:
    # CP4 review, O1: before the type check (part 5d), only the advisory check caught a wrong
    # type without the second reading (17 of 221 copies).
    assert "type_named_near_quote" in _results().checks
    assert all(
        "type_named_near_quote" not in reason for _, reasons in _results().flagged for reason in reasons
    )
    assert _results().disturbed == ()
    wrong = [p for p in _results().planted if p.error == "wrong_type"]
    assert len(wrong) == 221
    by_type = [p for p in wrong if "type_named_near_quote" in p.failed]
    assert len(by_type) / len(wrong) >= 0.95


def test_a_consistent_wrong_date_passes_the_date_rules_unless_it_has_passed_or_is_cut_short() -> None:
    """Another date phrase of the quote passes the date rules, unless it is cut from a longer date.

    "year-end 2026" taken from "year-end 2026/first quarter 2027" is cut short (CP4, B2), so
    `date_words_in_quote` catches it; a whole phrase of its own still passes.
    """
    for found in _results().planted:
        if found.error == "consistent_wrong_date":
            value = found.record.value
            cut = cut_short(found.record.quote, value["date_text"], found.record.published)
            assert ("date_words_in_quote" in found.failed) is (cut is not None)
            ended = date.fromisoformat(found.record.value["window_end"]) < found.record.published
            assert ("window_matches_words" in found.failed) is ended


def test_each_company_carries_its_ticker_from_the_coverage_list() -> None:
    # Spyre's release names Spyre by its ticker alone, so the company check needs it (PR #55).
    tickers = {ref.names[0]: {t for t, _, _ in ref.tickers} for ref in _corpus().company_refs}
    assert tickers["Spyre Therapeutics, Inc."] == {"SYRE"}
    assert tickers["Mirum Pharmaceuticals, Inc."] == {"MIRM"}


def test_the_rule_checks_see_each_matched_trials_registry_names() -> None:
    corpus = _corpus()
    assert corpus.registry
    assert all(names.nct_id.startswith("NCT") for names in corpus.registry.values())


def test_the_company_and_trial_checks_are_run() -> None:
    assert {"company_named_in_source", "trial_named_in_quote"} <= set(_results().checks)


def test_a_wrong_company_or_a_wrong_trial_is_caught_every_time() -> None:
    for name in ("wrong_company", "wrong_trial"):
        planted = [p for p in _results().planted if p.error == name]
        assert planted
        assert all(p.caught for p in planted), name


def test_the_floor_holds_what_the_drug_check_now_catches() -> None:
    # Raised with part 3e (Opus review of PR #61), so losing the drug check fails CI.
    baseline = json.loads((FOLDER / "baseline.json").read_text(encoding="utf-8"))
    assert baseline["catch_rate"]["quote_same_date"] == 0.8
    assert baseline["rule_catch_rate"]["quote_same_date"] == 0.8
    assert baseline["rule_catch_rate"]["drug_in_release"] == 0.759
    assert baseline["rule_catch_rate"]["drug_elsewhere"] == 0.993
    without = [check for check in CHECKS if check.__name__ != "_drug_named"]
    assert len(without) == len(CHECKS) - 1
    problems = floor_problems(scores(run(_corpus(), checks=without)), baseline)
    assert "quote_same_date: catch_rate fell to 0.0, below the floor 0.8" in problems
