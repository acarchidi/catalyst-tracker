"""The planted-error test, part one: clean claims, and realistic errors planted in copies (ADR 0018, part 4).

Plain code on recorded files only: the gold set's releases and a recorded copy of the registry.
These tests are judged against ADR 0018 part 4: each mutation makes one realistic error and
changes only the part it names, and the clean records are never changed.
"""

from datetime import date
from functools import cache

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from catalyst.extract.dates import parse_window
from catalyst.extract.text import find_quote
from catalyst.resolve import name_key
from catalyst.schemas import TrialPhase
from catalyst.verify.planted import (
    ERRORS,
    SEED,
    Corpus,
    ErrorType,
    Record,
    load_corpus,
    parts,
    plant,
    trial_links,
)
from catalyst.verify.rules import phases_overlap, stated_phases

BY_NAME = {error.name: error for error in ERRORS}


@cache
def _corpus() -> Corpus:
    return load_corpus()


def _record(drug: str, date_text: str) -> Record:
    return next(r for r in _corpus().records if r.value["drug"] == drug and r.value["date_text"] == date_text)


def test_every_gold_event_becomes_one_claim_as_the_extractor_stores_it() -> None:
    corpus = _corpus()
    assert len(corpus.records) + len(corpus.left_out) == 20
    # Mirum's "Q3" shares its year with "Q4 2026" in the sentence, so on its own it names no
    # window; the extractor flags that claim, and an error planted in it would prove nothing.
    assert [(r.value["drug"], r.value["date_text"], r.value["problems"]) for r in corpus.left_out] == [
        ("Brelovitug", "Q3", ["no_window"])
    ]
    for record in corpus.records:
        text = corpus.texts[record.document]
        assert text[record.quote_start : record.quote_end] == record.quote
        assert record.value["problems"] == []
        window = parse_window(record.value["date_text"], record.published)
        assert window is not None
        assert record.value["window_start"] == window.start.isoformat()


def test_the_error_types_cover_every_kind_adr_0018_names() -> None:
    assert set(BY_NAME) == {
        "wrong_window",
        "past_window",
        "wrong_drug",
        "wrong_type",
        "wrong_company",
        "wrong_trial",
        "wrong_phase",
        "quote_swap",
        "pdufa_outside_review",
    }
    assert all(error.label and error.blind_spot for error in ERRORS)


@settings(max_examples=150, deadline=None)
@given(
    error=st.sampled_from(ERRORS),
    event=st.integers(min_value=0, max_value=18),
    seed=st.integers(min_value=0, max_value=10_000),
)
def test_a_mutation_changes_exactly_the_part_it_names_and_never_the_clean_record(
    error: ErrorType, event: int, seed: int
) -> None:
    corpus = _corpus()
    original = corpus.records[event]
    before = parts(original, corpus)
    planted = plant(BY_NAME[error.name], original, corpus, seed)
    assert parts(original, corpus) == before  # the clean record is never changed
    if planted is None:
        return
    after = parts(planted, corpus)
    changed = {name for name in before if before[name] != after[name]}
    assert changed == BY_NAME[error.name].changes
    assert planted.id != original.id


@settings(max_examples=100, deadline=None)
@given(event=st.integers(min_value=0, max_value=18), seed=st.integers(min_value=0, max_value=10_000))
def test_each_planted_error_is_really_wrong(event: int, seed: int) -> None:
    corpus = _corpus()
    original = corpus.records[event]
    words = parse_window(original.value["date_text"], original.published)
    assert words is not None

    shifted = plant(BY_NAME["wrong_window"], original, corpus, seed)
    assert shifted is not None
    assert shifted.value["date_text"] == original.value["date_text"]
    assert date.fromisoformat(shifted.value["window_start"]) != words.start
    assert date.fromisoformat(shifted.value["window_end"]) >= original.published

    past = plant(BY_NAME["past_window"], original, corpus, seed)
    assert past is not None
    assert date.fromisoformat(past.value["window_end"]) < original.published

    drug = plant(BY_NAME["wrong_drug"], original, corpus, seed)
    assert drug is not None
    assert name_key(drug.value["drug"]) != name_key(original.value["drug"] or "")

    kind = plant(BY_NAME["wrong_type"], original, corpus, seed)
    assert kind is not None
    assert kind.value["catalyst_type"] != original.value["catalyst_type"]

    company = plant(BY_NAME["wrong_company"], original, corpus, seed)
    assert company is not None
    assert company.company_id != original.company_id

    swapped = plant(BY_NAME["quote_swap"], original, corpus, seed)
    assert swapped is not None
    text = corpus.texts[original.document]
    assert find_quote(text, swapped.quote) is not None  # still the source's own words
    assert swapped.quote_end <= original.quote_start or swapped.quote_start >= original.quote_end


@settings(max_examples=60, deadline=None)
@given(event=st.integers(min_value=0, max_value=18), seed=st.integers(min_value=0, max_value=10_000))
def test_a_wrong_trial_is_another_of_the_companys_trials_and_a_wrong_phase_contradicts_the_quote(
    event: int, seed: int
) -> None:
    corpus = _corpus()
    original = corpus.records[event]
    trial = plant(BY_NAME["wrong_trial"], original, corpus, seed)
    assert trial is not None
    (linked,) = trial_links(trial, corpus)
    assert linked not in trial_links(original, corpus)
    assert linked in corpus.company_trials[original.company_id]

    phase = plant(BY_NAME["wrong_phase"], original, corpus, seed)
    stated = stated_phases(original.quote)
    if not stated:
        assert phase is None
        return
    assert phase is not None
    (wrong,) = trial_links(phase, corpus)
    registered = corpus.phases[wrong]
    assert registered not in (None, TrialPhase.NOT_APPLICABLE)
    assert not any(phases_overlap(said, registered) for said in stated)


def test_a_pdufa_date_is_moved_beyond_fifteen_months_and_other_types_are_left_alone() -> None:
    corpus = _corpus()
    error = BY_NAME["pdufa_outside_review"]
    pdufa = _record("zilurgisertib", "September 26, 2026")
    planted = plant(error, pdufa, corpus, SEED)
    assert planted is not None
    assert date.fromisoformat(planted.value["window_start"]) > date(2027, 11, 6)
    assert plant(error, _record("LIVMARLI", "Q4 2026"), corpus, SEED) is None


@pytest.mark.parametrize("phase", list(TrialPhase))
def test_a_phase_always_overlaps_itself_unless_it_has_no_number(phase: TrialPhase) -> None:
    assert phases_overlap(phase, phase) is (phase is not TrialPhase.NOT_APPLICABLE)
