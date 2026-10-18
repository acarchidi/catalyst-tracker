"""The planted-error test, part one: clean claims, and realistic errors planted in copies (ADR 0018, part 4).

Plain code on recorded files only: the gold set's releases and a recorded copy of the registry.
These tests are judged against ADR 0018 part 4: each mutation makes one realistic error and
changes only the part it names, and the clean records are never changed.
"""

import json
from datetime import date
from functools import cache

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from catalyst.extract.agent import has_words
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
    date_phrases,
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
        "consistent_wrong_date",
        "past_window",
        "drug_in_release",
        "drug_elsewhere",
        "wrong_type",
        "wrong_company",
        "wrong_trial",
        "wrong_phase",
        "quote_swap",
        "quote_same_date",
        "pdufa_outside_review",
    }
    assert all(error.label and error.blind_spot for error in ERRORS)


@settings(max_examples=150, deadline=None)
@given(
    error=st.sampled_from(ERRORS),
    event=st.integers(min_value=0, max_value=18),
    seed=st.integers(min_value=0, max_value=10_000),
)
def test_a_mutation_changes_exactly_the_parts_it_names_and_never_the_clean_record(
    error: ErrorType, event: int, seed: int
) -> None:
    corpus = _corpus()
    original = corpus.records[event]
    before = parts(original, corpus)
    copies = plant(error, original, corpus, seed)
    assert parts(original, corpus) == before  # the clean record is never changed
    for copy in copies:
        after = parts(copy, corpus)
        assert {name for name in before if before[name] != after[name]} == error.changes
    assert len({c.id for c in copies} | {original.id}) == len(copies) + 1
    assert len({json.dumps(parts(c, corpus), default=str, sort_keys=True) for c in copies}) == len(copies)


@settings(max_examples=100, deadline=None)
@given(event=st.integers(min_value=0, max_value=18), seed=st.integers(min_value=0, max_value=10_000))
def test_each_planted_error_is_really_wrong(event: int, seed: int) -> None:
    corpus = _corpus()
    original = corpus.records[event]
    words = parse_window(original.value["date_text"], original.published)
    assert words is not None
    text = corpus.texts[original.document]

    for shifted in _all("wrong_window", original, seed):
        assert shifted.value["date_text"] == original.value["date_text"]
        assert date.fromisoformat(shifted.value["window_start"]) > words.start
    for past in _all("past_window", original, seed):
        assert date.fromisoformat(past.value["window_end"]) < original.published
    for dated in plant(BY_NAME["consistent_wrong_date"], original, _corpus(), seed):
        assert has_words(original.quote, dated.value["date_text"])  # the new date words are in the quote
        window = parse_window(dated.value["date_text"], original.published)
        assert window is not None
        assert dated.value["window_start"] == window.start.isoformat()
        assert dated.value["window_end"] == window.end.isoformat()
    for drug in [
        *plant(BY_NAME["drug_in_release"], original, corpus),
        *_all("drug_elsewhere", original, seed),
    ]:
        assert name_key(drug.value["drug"]) != name_key(original.value["drug"] or "")
    for kind in _all("wrong_type", original, seed):
        assert kind.value["catalyst_type"] != original.value["catalyst_type"]
    for company in _all("wrong_company", original, seed):
        assert company.company_id != original.company_id
    for swapped in [
        *_all("quote_swap", original, seed),
        *plant(BY_NAME["quote_same_date"], original, corpus),
    ]:
        assert find_quote(text, swapped.quote) is not None  # still the source's own words
        assert swapped.quote_end <= original.quote_start or swapped.quote_start >= original.quote_end
    for same in plant(BY_NAME["quote_same_date"], original, corpus):
        assert has_words(same.quote, original.value["date_text"])


def _all(name: str, original: Record, seed: int) -> tuple[Record, ...]:
    """Return the copies of an error every clean record can have; there must be at least one."""
    copies = plant(BY_NAME[name], original, _corpus(), seed)
    assert copies
    return copies


def test_wrong_drugs_come_from_the_same_release_or_from_other_companies() -> None:
    corpus = _corpus()
    original = _record("LIVMARLI", "Q4 2026")
    inside = {c.value["drug"] for c in plant(BY_NAME["drug_in_release"], original, corpus)}
    assert inside == {"Brelovitug", "volixibat", "zilurgisertib"}
    elsewhere = {c.value["drug"] for c in plant(BY_NAME["drug_elsewhere"], original, corpus)}
    assert not inside & elsewhere
    assert "SPY072" in elsewhere


def test_a_consistent_wrong_date_takes_another_date_phrase_of_the_same_quote() -> None:
    corpus = _corpus()
    briumvi = _record("subcutaneous BRIUMVI", "year-end 2026/first quarter 2027")
    dates = {c.value["date_text"] for c in plant(BY_NAME["consistent_wrong_date"], briumvi, corpus)}
    assert dates == {"year-end 2026", "first quarter 2027"}
    assert plant(BY_NAME["consistent_wrong_date"], _record("LIVMARLI", "Q4 2026"), corpus) == ()


def test_a_quote_from_another_event_holds_the_same_date_words() -> None:
    corpus = _corpus()
    copies = plant(BY_NAME["quote_same_date"], _record("Brelovitug", "Q4 2026"), corpus)
    assert any("LIVMARLI EXPAND" in c.quote for c in copies)


def test_every_alternative_trial_and_company_is_planted_not_one_random_pick() -> None:
    corpus = _corpus()
    original = _record("volixibat", "Q1 2027")
    trials = {t for c in plant(BY_NAME["wrong_trial"], original, corpus) for t in trial_links(c, corpus)}
    assert trials == set(corpus.company_trials[original.company_id]) - set(trial_links(original, corpus))
    companies = {c.company_id for c in plant(BY_NAME["wrong_company"], original, corpus)}
    assert companies == set(corpus.companies) - {original.company_id}
    assert plant(BY_NAME["wrong_trial"], original, corpus, 1) == plant(
        BY_NAME["wrong_trial"], original, corpus, 2
    )


@settings(max_examples=40, deadline=None)
@given(event=st.integers(min_value=0, max_value=18))
def test_a_wrong_phase_always_contradicts_the_quote(event: int) -> None:
    corpus = _corpus()
    original = corpus.records[event]
    stated = stated_phases(original.quote)
    copies = plant(BY_NAME["wrong_phase"], original, corpus)
    assert bool(copies) == bool(stated)
    for copy in copies:
        (wrong,) = trial_links(copy, corpus)
        registered = corpus.phases[wrong]
        assert registered not in (None, TrialPhase.NOT_APPLICABLE)
        assert not any(phases_overlap(said, registered) for said in stated)


def test_a_pdufa_date_is_moved_beyond_fifteen_months_and_other_types_are_left_alone() -> None:
    corpus = _corpus()
    error = BY_NAME["pdufa_outside_review"]
    (planted,) = plant(error, _record("zilurgisertib", "September 26, 2026"), corpus, SEED)
    assert date.fromisoformat(planted.value["window_start"]) > date(2027, 11, 6)
    assert plant(error, _record("LIVMARLI", "Q4 2026"), corpus, SEED) == ()


def test_date_phrases_are_found_as_written() -> None:
    text = "Data in Q4 2026, the first half of 2027, September 26, 2026, mid-2027 and Sept 2026."
    assert date_phrases(text) == [
        "Q4 2026",
        "first half of 2027",
        "September 26, 2026",
        "mid-2027",
        "Sept 2026",
    ]


@pytest.mark.parametrize("phase", list(TrialPhase))
def test_a_phase_always_overlaps_itself_unless_it_has_no_number(phase: TrialPhase) -> None:
    assert phases_overlap(phase, phase) is (phase is not TrialPhase.NOT_APPLICABLE)
