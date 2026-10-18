"""The publish decision, judged against the spec's publish rule (spec: Verification; ADR 0018, part 5).

A claim publishes only when every required check has a stored row that passed or did not apply,
and its quote is found again, character for character, at its place in the stored copy. Anything
else waits for review with every reason. A missing row never passes.
"""

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from catalyst.extract.agent import EXTRACTOR_PROMPT
from catalyst.extract.text import TEXT_VERSION, document_text
from catalyst.publish.gate import (
    ADVISORY_CHECK,
    MAY_NOT_APPLY,
    QUOTE_CHECK,
    REQUIRED_CHECKS,
    RULE_CHECKS,
    Decision,
    UnreadableCopy,
    decide,
    match_quote,
    read_copy,
    rule_check_names,
)
from catalyst.schemas import CheckOutcome, Claim, ClaimCheck, ClaimSubject, GateDecision
from catalyst.snapshots import LocalSnapshotStore, snapshot_key
from catalyst.verify import advisory, company, drug, independent, rules, second
from catalyst.verify.rules import ClaimFacts, Outcome, rule_checks

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
SENTENCE = "Topline data from the Phase 3 RAINIER trial are expected in the second half of 2027."
RELEASE = f"<html><body><p>Acme Bio news</p><p>{SENTENCE}</p></body></html>".encode()
TEXT = document_text(RELEASE, "text/html")
START = TEXT.index(SENTENCE)


def _claim(*, quote: str = SENTENCE, start: int = START, **changes: Any) -> Claim:
    value: dict[str, Any] = {"catalyst_type": "topline_readout", "problems": [], "text_version": TEXT_VERSION}
    fields: dict[str, Any] = {
        "source_document_id": uuid4(),
        "subject_type": ClaimSubject.CATALYST,
        "field": "catalyst:abc",
        "value": value | changes.pop("value", {}),
        "quote": quote,
        "quote_start": start,
        "quote_end": start + len(quote),
        "agent": "extractor",
        "prompt_id": EXTRACTOR_PROMPT[0],
        "prompt_version": EXTRACTOR_PROMPT[1],
        "provider": "anthropic",
        "model": "claude-sonnet-5-5",
        "extracted_at": NOW,
    }
    return Claim.model_validate(fields | changes)


def _row(
    claim: Claim, name: str, outcome: CheckOutcome = CheckOutcome.PASS, reason: str | None = None
) -> ClaimCheck:
    if outcome is CheckOutcome.FAIL and reason is None:
        reason = f"{name} found a problem"
    return ClaimCheck(claim_id=claim.id, check_name=name, outcome=outcome, reason=reason, checked_at=NOW)


def _all_pass(claim: Claim) -> list[ClaimCheck]:
    return [_row(claim, name) for name in REQUIRED_CHECKS]


def _quote_ok(claim: Claim) -> Any:
    return match_quote(claim, TEXT)


# --- The required list ---


def test_the_rule_checks_are_read_from_the_rules_themselves() -> None:
    facts = ClaimFacts(
        id=uuid4(),
        value={"date_text": "second half of 2027", "problems": []},
        quote=SENTENCE,
        published=date(2026, 10, 14),
        matches=(),
        trial_phases={},
    )
    assert tuple(result.check for result in rule_checks(facts, date(2026, 10, 18))) == RULE_CHECKS
    assert len(set(RULE_CHECKS)) == len(RULE_CHECKS) == 9
    assert "trial_named_in_quote" in RULE_CHECKS  # part 3d


def test_every_check_the_publish_rule_names_is_required() -> None:
    # Spec: quote match (here, by the gate itself), second extraction, rule checks, and a second
    # source or a regulator or SEC filing (the independent-source check). ADR 0018 part 3 adds
    # the Federal Register notice for an advisory meeting, part 3d the company named in the
    # source document, and part 3e the drug named in or just before the quote.
    assert set(RULE_CHECKS) < set(REQUIRED_CHECKS)
    assert second.CHECK in REQUIRED_CHECKS
    assert independent.CHECK in REQUIRED_CHECKS
    assert ADVISORY_CHECK in REQUIRED_CHECKS
    assert company.CHECK in REQUIRED_CHECKS
    assert drug.CHECK in REQUIRED_CHECKS
    assert len(set(REQUIRED_CHECKS)) == len(REQUIRED_CHECKS) == len(RULE_CHECKS) + 5
    assert QUOTE_CHECK not in REQUIRED_CHECKS  # the gate matches the quote itself, never from a row


def test_the_advisory_check_is_the_one_its_module_stores() -> None:
    assert ADVISORY_CHECK == advisory.CHECK == "advisory_meeting_noticed"


def test_only_checks_about_a_possibly_absent_fact_may_not_apply() -> None:
    assert {
        "window_not_past",
        "phase_matches_registry",
        "pdufa_date_plausible",
        "advisory_meeting_noticed",
        "trial_named_in_quote",  # no matched trial (part 3d)
        "drug_named_near_quote",  # no drug named, as in a trial of several drugs (part 3e)
    } == MAY_NOT_APPLY
    for name in (
        "extractor_checks",
        "has_window",
        "names_matched",
        second.CHECK,
        independent.CHECK,
        company.CHECK,
    ):
        assert name not in MAY_NOT_APPLY


def test_two_rules_naming_one_check_are_refused() -> None:
    with pytest.raises(RuntimeError, match="each rule must name its own check"):
        rule_check_names((*rules.RULES, rules.RULES[0]))
    assert rule_check_names(rules.RULES) == RULE_CHECKS


# --- decide() ---


def test_every_check_passing_and_the_quote_found_publishes() -> None:
    claim = _claim()
    assert decide(claim, _all_pass(claim), _quote_ok(claim)) == Decision(GateDecision.PUBLISH, ())


def test_not_applicable_passes_only_for_the_checks_that_may_not_apply() -> None:
    claim = _claim()
    rows = [
        _row(claim, name, CheckOutcome.NOT_APPLICABLE, "does not apply")
        if name in MAY_NOT_APPLY
        else _row(claim, name)
        for name in REQUIRED_CHECKS
    ]
    assert decide(claim, rows, _quote_ok(claim)).decision is GateDecision.PUBLISH


def test_not_applicable_everywhere_never_publishes() -> None:
    claim = _claim()
    rows = [_row(claim, name, CheckOutcome.NOT_APPLICABLE, "does not apply") for name in REQUIRED_CHECKS]
    decision = decide(claim, rows, _quote_ok(claim))
    assert decision.decision is GateDecision.REVIEW
    assert decision.reasons == tuple(
        f"{name} must pass, but it did not apply" for name in REQUIRED_CHECKS if name not in MAY_NOT_APPLY
    )


def test_a_not_applicable_independent_source_goes_to_review() -> None:
    claim = _claim()
    rows = [r for r in _all_pass(claim) if r.check_name != independent.CHECK]
    rows.append(_row(claim, independent.CHECK, CheckOutcome.NOT_APPLICABLE, "no other document"))
    decision = decide(claim, rows, _quote_ok(claim))
    assert decision.decision is GateDecision.REVIEW
    assert decision.reasons == (f"{independent.CHECK} must pass, but it did not apply",)
    assert [hold.label for hold in decision.holds] == [f"{independent.CHECK} failed"]


@pytest.mark.parametrize(
    "prompt",
    [(EXTRACTOR_PROMPT[0], "0.9.0"), ("verify_catalysts", EXTRACTOR_PROMPT[1])],
    ids=["an older version", "another prompt"],
)
def test_a_claim_from_any_prompt_but_the_current_extractor_goes_to_review(prompt: tuple[str, str]) -> None:
    claim = _claim(prompt_id=prompt[0], prompt_version=prompt[1])
    decision = decide(claim, _all_pass(claim), _quote_ok(claim))
    assert decision.decision is GateDecision.REVIEW
    current = "/".join(EXTRACTOR_PROMPT)
    assert decision.reasons == (
        f"the claim came from prompt {prompt[0]}/{prompt[1]}, not the current {current}",
    )


def test_one_failing_check_sends_the_claim_to_review_with_its_stored_reason() -> None:
    claim = _claim()
    rows = [r for r in _all_pass(claim) if r.check_name != "window_not_past"]
    rows.append(_row(claim, "window_not_past", CheckOutcome.FAIL, "the window ended on 2026-09-30"))
    decision = decide(claim, rows, _quote_ok(claim))
    assert decision.decision is GateDecision.REVIEW
    assert decision.reasons == ("window_not_past: the window ended on 2026-09-30",)


def test_a_missing_row_never_passes() -> None:
    claim = _claim()
    rows = [r for r in _all_pass(claim) if r.check_name != second.CHECK]
    decision = decide(claim, rows, _quote_ok(claim))
    assert decision.decision is GateDecision.REVIEW
    assert decision.reasons == (f"check {second.CHECK} has not run",)


def test_no_rows_at_all_lists_every_required_check() -> None:
    claim = _claim()
    decision = decide(claim, [], _quote_ok(claim))
    assert decision.decision is GateDecision.REVIEW
    assert decision.reasons == tuple(f"check {name} has not run" for name in REQUIRED_CHECKS)


def test_every_reason_is_kept_quote_first_then_checks_in_order() -> None:
    claim = _claim()
    rows = [r for r in _all_pass(claim) if r.check_name not in {"has_window", independent.CHECK}]
    rows.append(_row(claim, "has_window", CheckOutcome.FAIL, "the date words name no window"))
    quote = match_quote(claim, TEXT.replace("second half", "first half"))
    decision = decide(claim, rows, quote)
    assert decision.decision is GateDecision.REVIEW
    assert decision.reasons == (
        quote.reason,
        "has_window: the date words name no window",
        f"check {independent.CHECK} has not run",
    )
    assert [hold.label for hold in decision.holds] == [
        f"{QUOTE_CHECK} failed",
        "has_window failed",
        f"{independent.CHECK} has not run",
    ]


def test_a_quote_that_does_not_match_the_stored_copy_goes_to_review() -> None:
    claim = _claim()
    decision = decide(claim, _all_pass(claim), match_quote(claim, TEXT.replace("2027", "2028")))
    assert decision.decision is GateDecision.REVIEW
    assert decision.reasons == ("the quote is not at its stored place in the stored copy",)


def test_a_changed_text_version_goes_to_review_even_when_the_words_line_up() -> None:
    claim = _claim(value={"text_version": "0"})
    decision = decide(claim, _all_pass(claim), match_quote(claim, TEXT))
    assert decision.decision is GateDecision.REVIEW
    assert decision.reasons == (
        f"the quote's place was found with text version 0, not the current {TEXT_VERSION}; "
        "it must be found again",
    )


@pytest.mark.parametrize("version", [None, 1, ""])
def test_a_claim_that_does_not_name_its_text_version_goes_to_review(version: object) -> None:
    claim = _claim(value={"text_version": version})
    assert match_quote(claim, TEXT).outcome is Outcome.FAIL
    assert decide(claim, _all_pass(claim), match_quote(claim, TEXT)).decision is GateDecision.REVIEW


def test_an_unreadable_copy_goes_to_review_with_why() -> None:
    claim = _claim()
    quote = match_quote(claim, UnreadableCopy("the stored copy is missing"))
    decision = decide(claim, _all_pass(claim), quote)
    assert decision.decision is GateDecision.REVIEW
    assert decision.reasons == ("the stored copy is missing",)


def test_a_quote_found_elsewhere_in_the_copy_is_not_enough() -> None:
    # The stored offsets are part of the claim; the words must be at that exact place.
    text = f"{SENTENCE}\n{TEXT}"
    claim = _claim()
    assert match_quote(claim, text).outcome is Outcome.FAIL


def test_a_quote_past_the_end_of_the_copy_fails() -> None:
    claim = _claim()
    assert match_quote(claim, TEXT[: START + 10]).outcome is Outcome.FAIL


def test_the_quote_check_must_pass_not_merely_not_apply() -> None:
    claim = _claim()
    quote = match_quote(claim, TEXT)
    stand_in = type(quote)(QUOTE_CHECK, Outcome.NOT_APPLICABLE, "skipped")
    assert decide(claim, _all_pass(claim), stand_in).decision is GateDecision.REVIEW


def test_a_quote_result_from_another_check_is_refused() -> None:
    claim = _claim()
    quote = match_quote(claim, TEXT)
    with pytest.raises(ValueError, match="quote"):
        decide(claim, _all_pass(claim), type(quote)("has_window", Outcome.PASS))


def test_rows_of_retired_or_unknown_checks_are_ignored() -> None:
    # ADR 0018: the gate reads only the current list of checks.
    claim = _claim()
    rows = [*_all_pass(claim), _row(claim, "retired_check", CheckOutcome.FAIL, "old")]
    assert decide(claim, rows, _quote_ok(claim)).decision is GateDecision.PUBLISH


def test_rows_for_another_claim_are_refused() -> None:
    claim, other = _claim(), _claim()
    with pytest.raises(ValueError, match="another claim"):
        decide(claim, [*_all_pass(claim)[1:], _row(other, REQUIRED_CHECKS[0])], _quote_ok(claim))


def test_two_rows_for_one_check_are_refused() -> None:
    claim = _claim()
    with pytest.raises(ValueError, match="twice"):
        decide(claim, [*_all_pass(claim), _row(claim, "has_window")], _quote_ok(claim))


@pytest.mark.parametrize(
    "changes",
    [{"agent": "verifier"}, {"field": "window"}],
    ids=["a verifier claim", "not a catalyst claim"],
)
def test_only_the_extractors_catalyst_claims_can_publish(changes: dict[str, Any]) -> None:
    claim = _claim(**changes)
    decision = decide(claim, _all_pass(claim), _quote_ok(claim))
    assert decision.decision is GateDecision.REVIEW
    assert decision.reasons == ("only the extractor's catalyst claims can publish",)


# --- Property: any single failing, missing, or wrongly excused required check means review ---


def _clean_outcomes() -> st.SearchStrategy[list[CheckOutcome]]:
    """One outcome per required check that lets a claim publish: pass, or n/a where allowed."""
    either = st.sampled_from([CheckOutcome.PASS, CheckOutcome.NOT_APPLICABLE])
    parts = [either if name in MAY_NOT_APPLY else st.just(CheckOutcome.PASS) for name in REQUIRED_CHECKS]
    return st.tuples(*parts).map(list)


@given(
    others=_clean_outcomes(),
    excused=st.sampled_from(sorted(set(REQUIRED_CHECKS) - MAY_NOT_APPLY)),
)
def test_not_applicable_from_any_check_that_must_pass_means_review(
    others: list[CheckOutcome], excused: str
) -> None:
    claim = _claim()
    rows = [
        _row(claim, name, CheckOutcome.NOT_APPLICABLE if name == excused else outcome, "n/a")
        for name, outcome in zip(REQUIRED_CHECKS, others, strict=True)
    ]
    decision = decide(claim, rows, _quote_ok(claim))
    assert decision.decision is GateDecision.REVIEW
    assert decision.reasons == (f"{excused} must pass, but it did not apply",)


@given(
    others=_clean_outcomes(),
    broken=st.sampled_from(REQUIRED_CHECKS),
    missing=st.booleans(),
    extra=st.lists(
        # Names shaped like real check names: stored text is trimmed, so blank names cannot be stored.
        st.from_regex(r"[a-z][a-z_]{0,19}", fullmatch=True).filter(lambda s: s not in REQUIRED_CHECKS),
        max_size=3,
        unique=True,
    ),
)
def test_any_single_failing_or_missing_required_check_means_review(
    others: list[CheckOutcome], broken: str, missing: bool, extra: list[str]
) -> None:
    claim = _claim()
    rows = [_row(claim, name, outcome) for name, outcome in zip(REQUIRED_CHECKS, others, strict=True)]
    rows = [r for r in rows if r.check_name != broken]
    if not missing:
        rows.append(_row(claim, broken, CheckOutcome.FAIL, "planted"))
    rows += [_row(claim, name) for name in extra]
    decision = decide(claim, rows, _quote_ok(claim))
    assert decision.decision is GateDecision.REVIEW
    assert decision.reasons == ((f"check {broken} has not run",) if missing else (f"{broken}: planted",))


@given(
    outcomes=_clean_outcomes(),
)
def test_publish_when_every_required_row_passed_or_may_not_apply(outcomes: list[CheckOutcome]) -> None:
    claim = _claim()
    rows = [
        _row(claim, name, outcome, "n/a") for name, outcome in zip(REQUIRED_CHECKS, outcomes, strict=True)
    ]
    assert decide(claim, rows, _quote_ok(claim)) == Decision(GateDecision.PUBLISH, ())


# --- Reading the stored copy ---


def test_read_copy_returns_the_text_the_extractor_read(tmp_path: Path) -> None:
    store = LocalSnapshotStore(tmp_path)
    snapshot = store.put(RELEASE)
    assert read_copy(store, snapshot.key, snapshot.sha256, "text/html") == TEXT


def test_read_copy_names_a_missing_copy(tmp_path: Path) -> None:
    sha = "b" * 64
    copy = read_copy(LocalSnapshotStore(tmp_path), snapshot_key(sha), sha, "text/html")
    assert copy == UnreadableCopy("the stored copy is missing")


def test_read_copy_names_a_changed_copy(tmp_path: Path) -> None:
    store = LocalSnapshotStore(tmp_path)
    snapshot = store.put(RELEASE)
    (tmp_path / snapshot.key).write_bytes(RELEASE.replace(b"2027", b"2028"))
    copy = read_copy(store, snapshot.key, snapshot.sha256, "text/html")
    assert copy == UnreadableCopy("the stored copy no longer matches its hash")


def test_read_copy_refuses_a_key_that_is_not_the_recorded_hash(tmp_path: Path) -> None:
    store = LocalSnapshotStore(tmp_path)
    snapshot = store.put(RELEASE)
    other = store.put(b"another document")
    copy = read_copy(store, other.key, snapshot.sha256, "text/html")
    assert copy == UnreadableCopy("the stored copy's key does not match the document's recorded hash")


def test_read_copy_names_a_malformed_key(tmp_path: Path) -> None:
    copy = read_copy(LocalSnapshotStore(tmp_path), snapshot_key("not-a-hash"), "not-a-hash", "text/html")
    assert copy == UnreadableCopy("the stored copy could not be read")


def test_an_unreadable_copy_fails_the_quote_check() -> None:
    result = match_quote(_claim(), UnreadableCopy("the stored copy could not be read"))
    assert (result.check, result.outcome, result.reason) == (
        QUOTE_CHECK,
        Outcome.FAIL,
        "the stored copy could not be read",
    )


def test_the_quote_check_names_its_check() -> None:
    claim = _claim()
    result = match_quote(claim, TEXT)
    assert (result.check, result.outcome, result.reason) == (QUOTE_CHECK, Outcome.PASS, None)


def test_a_claim_with_no_matched_trial_can_still_publish() -> None:
    """Most claims name no trial; trial_named_in_quote then does not apply (part 3d)."""
    claim = _claim()
    rows = [r for r in _all_pass(claim) if r.check_name != "trial_named_in_quote"]
    rows.append(_row(claim, "trial_named_in_quote", CheckOutcome.NOT_APPLICABLE, "no matched trial"))
    assert decide(claim, rows, _quote_ok(claim)) == Decision(GateDecision.PUBLISH, ())


def test_a_claim_whose_source_does_not_name_its_company_goes_to_review() -> None:
    claim = _claim()
    rows = [r for r in _all_pass(claim) if r.check_name != company.CHECK]
    rows.append(
        _row(claim, company.CHECK, CheckOutcome.FAIL, "the source does not name Spyre Therapeutics, Inc.")
    )
    decision = decide(claim, rows, _quote_ok(claim))
    assert decision.decision is GateDecision.REVIEW
    assert decision.reasons == (f"{company.CHECK}: the source does not name Spyre Therapeutics, Inc.",)


def test_a_claim_that_names_no_drug_can_still_publish() -> None:
    """A trial of several drugs names none; drug_named_near_quote then does not apply (part 3e)."""
    claim = _claim()
    rows = [r for r in _all_pass(claim) if r.check_name != drug.CHECK]
    rows.append(_row(claim, drug.CHECK, CheckOutcome.NOT_APPLICABLE, "the claim names no drug"))
    assert decide(claim, rows, _quote_ok(claim)) == Decision(GateDecision.PUBLISH, ())


def test_a_claim_whose_quote_is_about_another_drug_goes_to_review() -> None:
    claim = _claim()
    rows = [r for r in _all_pass(claim) if r.check_name != drug.CHECK]
    rows.append(_row(claim, drug.CHECK, CheckOutcome.FAIL, "the quote names SPY003, not SPY072"))
    decision = decide(claim, rows, _quote_ok(claim))
    assert decision.decision is GateDecision.REVIEW
    assert decision.reasons == (f"{drug.CHECK}: the quote names SPY003, not SPY072",)
