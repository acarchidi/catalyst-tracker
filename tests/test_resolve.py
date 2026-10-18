"""The entity resolver maps a name to one company, drug, or trial, or says why it cannot (ADR 0015).

A match is exact after spelling is set aside: case, punctuation, spacing, and legal-form words.
Nothing is guessed: a partial name is no match, and a name that fits two records is ambiguous
and goes to review with both candidates.
"""

from datetime import date
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from catalyst.resolve import CompanyRef, DrugRef, MatchedBy, ReferenceData, Status, TrialRef
from catalyst.schemas import DrugNameKind

VERTEX, SPYRE, OTHER = uuid4(), uuid4(), uuid4()


def _companies() -> list[CompanyRef]:
    return [
        CompanyRef(
            id=VERTEX,
            cik=875320,
            names=("VERTEX PHARMACEUTICALS INC / MA",),
            tickers=(("VRTX", date(1991, 7, 24), None),),
        ),
        CompanyRef(
            id=SPYRE,
            cik=1636282,
            names=("Spyre Therapeutics, Inc.", "Aeglea BioTherapeutics, Inc."),
            tickers=(("AGLE", date(2016, 4, 7), date(2023, 11, 27)), ("SYRE", date(2023, 11, 28), None)),
        ),
        CompanyRef(
            id=OTHER, cik=1000001, names=("Agle Holdings Corp",), tickers=(("AGLE", date(2024, 3, 1), None),)
        ),
    ]


def _references(drugs: list[DrugRef] | None = None, trials: list[TrialRef] | None = None) -> ReferenceData:
    return ReferenceData(companies=_companies(), drugs=drugs or [], trials=trials or [])


# Companies


def test_sec_number_matches_exactly() -> None:
    result = _references().resolve_company(cik=1636282)
    assert (result.status, result.id, result.matched_by) == (Status.RESOLVED, SPYRE, MatchedBy.CIK)


def test_unknown_sec_number_does_not_fall_back_to_the_name() -> None:
    result = _references().resolve_company(cik=999, name="Spyre Therapeutics")
    assert result.status is Status.UNKNOWN


def test_ticker_resolves_to_the_company_that_held_it_on_that_day() -> None:
    references = _references()
    assert references.resolve_company(ticker="AGLE", on=date(2022, 1, 3)).id == SPYRE
    assert references.resolve_company(ticker="agle", on=date(2025, 1, 3)).id == OTHER
    assert references.resolve_company(ticker="SYRE", on=date(2022, 1, 3)).status is Status.UNKNOWN


def test_ticker_without_a_day_means_the_current_ticker() -> None:
    result = _references().resolve_company(ticker="SYRE")
    assert (result.id, result.matched_by) == (SPYRE, MatchedBy.TICKER)


@pytest.mark.parametrize(
    "name",
    ["Spyre Therapeutics", "SPYRE THERAPEUTICS INC", "Spyre Therapeutics, Inc.", "Aeglea BioTherapeutics"],
)
def test_current_and_former_names_match_however_they_are_written(name: str) -> None:
    result = _references().resolve_company(name=name)
    assert (result.status, result.id, result.matched_by) == (Status.RESOLVED, SPYRE, MatchedBy.NAME)


@pytest.mark.parametrize("name", ["Spyre", "Vertex", "Spyre Therapeutics Holdings", "Inc.", ""])
def test_a_partial_or_empty_name_is_no_match(name: str) -> None:
    assert _references().resolve_company(name=name).status is Status.UNKNOWN


def test_a_name_two_companies_share_is_ambiguous_and_names_both() -> None:
    twin = CompanyRef(id=uuid4(), cik=2000002, names=("Vertex Pharmaceuticals Ltd",), tickers=())
    references = ReferenceData(companies=[*_companies(), twin], drugs=[], trials=[])
    result = references.resolve_company(name="Vertex Pharmaceuticals")
    assert result.status is Status.AMBIGUOUS
    assert set(result.candidates) == {VERTEX, twin.id}
    assert result.id is None


# Drugs

SPY001, LIVMARLI, ABC_SPYRE, ABC_OTHER = uuid4(), uuid4(), uuid4(), uuid4()


def _drugs() -> list[DrugRef]:
    return [
        DrugRef(id=SPY001, company_id=SPYRE, names=(("SPY001", DrugNameKind.CODE),)),
        DrugRef(
            id=LIVMARLI,
            company_id=None,
            names=(("maralixibat", DrugNameKind.INN), ("LIVMARLI", DrugNameKind.BRAND)),
        ),
        DrugRef(id=ABC_SPYRE, company_id=SPYRE, names=(("ABC-101", DrugNameKind.CODE),)),
        DrugRef(id=ABC_OTHER, company_id=OTHER, names=(("ABC-101", DrugNameKind.CODE),)),
    ]


@pytest.mark.parametrize("name", ["SPY001", "SPY-001", "spy 001", "SPY\u2013001"])
def test_code_names_match_across_case_hyphens_and_spaces(name: str) -> None:
    result = _references(_drugs()).resolve_drug(name, company_id=SPYRE)
    assert (result.status, result.id, result.matched_by) == (Status.RESOLVED, SPY001, MatchedBy.CODE_NAME)


def test_a_code_name_two_companies_use_needs_the_company() -> None:
    references = _references(_drugs())
    assert references.resolve_drug("ABC-101", company_id=OTHER).id == ABC_OTHER
    unscoped = references.resolve_drug("ABC-101")
    assert unscoped.status is Status.AMBIGUOUS
    assert set(unscoped.candidates) == {ABC_SPYRE, ABC_OTHER}


def test_another_company_s_code_name_is_no_match() -> None:
    assert _references(_drugs()).resolve_drug("SPY001", company_id=VERTEX).status is Status.UNKNOWN


def test_generic_and_brand_names_match_for_any_company() -> None:
    references = _references(_drugs())
    generic = references.resolve_drug("Maralixibat", company_id=VERTEX)
    brand = references.resolve_drug("LIVMARLI®", company_id=SPYRE)
    assert (generic.id, generic.matched_by) == (LIVMARLI, MatchedBy.INN)
    assert (brand.id, brand.matched_by) == (LIVMARLI, MatchedBy.BRAND)


@pytest.mark.parametrize("name", ["SPY", "SPY0011", "maralixibat chloride", "", "®"])
def test_a_different_or_empty_drug_name_is_no_match(name: str) -> None:
    assert _references(_drugs()).resolve_drug(name, company_id=SPYRE).status is Status.UNKNOWN


_CODE = st.from_regex(r"[A-Z]{2,4}[0-9]{2,4}", fullmatch=True)


@given(code=_CODE, separator=st.sampled_from(["", "-", " ", "\u2010", "\u2013"]), lower=st.booleans())
def test_any_spelling_of_a_code_name_resolves_to_the_same_drug(
    code: str, separator: str, lower: bool
) -> None:
    letters = code.rstrip("0123456789")
    written = letters + separator + code[len(letters) :]
    drug = DrugRef(id=uuid4(), company_id=SPYRE, names=((code, DrugNameKind.CODE),))
    references = ReferenceData(companies=_companies(), drugs=[drug], trials=[])
    assert references.resolve_drug(written.lower() if lower else written, company_id=SPYRE).id == drug.id


# Trials

AZURE1, AZURE4, SKYLINE, OTHER_AZURE1 = uuid4(), uuid4(), uuid4(), uuid4()


def _trials() -> list[TrialRef]:
    return [
        TrialRef(id=AZURE1, nct_id="NCT06111111", study_name="AZURE-1", company_ids=frozenset({SPYRE})),
        TrialRef(id=AZURE4, nct_id="NCT06444444", study_name="AZURE-4", company_ids=frozenset({SPYRE})),
        TrialRef(id=SKYLINE, nct_id="NCT06555555", study_name=None, company_ids=frozenset({SPYRE})),
        TrialRef(id=OTHER_AZURE1, nct_id="NCT07000001", study_name="Azure 1", company_ids=frozenset({OTHER})),
    ]


@pytest.mark.parametrize("text", ["NCT06555555", "nct06555555", "the SKYLINE trial (NCT06555555)"])
def test_a_registry_number_in_the_text_matches_exactly(text: str) -> None:
    result = _references(trials=_trials()).resolve_trial(text)
    assert (result.status, result.id, result.matched_by) == (Status.RESOLVED, SKYLINE, MatchedBy.NCT_ID)


def test_two_registry_numbers_in_one_text_are_ambiguous() -> None:
    result = _references(trials=_trials()).resolve_trial("NCT06111111 and NCT06444444")
    assert result.status is Status.AMBIGUOUS
    assert set(result.candidates) == {AZURE1, AZURE4}


def test_a_study_name_matches_within_the_company_however_it_is_written() -> None:
    references = _references(trials=_trials())
    assert references.resolve_trial("AZURE 1", company_id=SPYRE).id == AZURE1
    assert references.resolve_trial("azure-4", company_id=SPYRE).id == AZURE4
    unscoped = references.resolve_trial("AZURE-1")
    assert unscoped.status is Status.AMBIGUOUS
    assert set(unscoped.candidates) == {AZURE1, OTHER_AZURE1}


@pytest.mark.parametrize("text", ["AZURE", "AZURE-14", "NCT09999999", ""])
def test_a_partial_name_or_unknown_number_is_no_match(text: str) -> None:
    assert _references(trials=_trials()).resolve_trial(text, company_id=SPYRE).status is Status.UNKNOWN


def test_only_a_resolved_match_has_an_id() -> None:
    result = _references().resolve_company(name="nobody")
    assert (result.status, result.id, result.candidates, result.matched_by) == (
        Status.UNKNOWN,
        None,
        (),
        None,
    )
    assert isinstance(_references().resolve_company(cik=875320).id, UUID)


def test_two_registry_numbers_go_to_review_even_when_only_one_is_on_file() -> None:
    result = _references(trials=_trials()).resolve_trial("NCT06111111 and its extension study NCT09999999")
    assert result.status is Status.AMBIGUOUS
    assert (result.candidates, result.id) == ((AZURE1,), None)


def test_the_same_registry_number_twice_is_one_trial() -> None:
    assert _references(trials=_trials()).resolve_trial("NCT06111111 (nct06111111)").id == AZURE1


def test_stored_numbers_and_tickers_match_whatever_their_case() -> None:
    trial = TrialRef(id=uuid4(), nct_id="nct01234567", study_name=None, company_ids=frozenset())
    company = CompanyRef(
        id=uuid4(), cik=3000003, names=("Lower Co",), tickers=(("lowr", date(2020, 1, 1), None),)
    )
    references = ReferenceData(companies=[company], drugs=[], trials=[trial])
    assert references.resolve_trial("NCT01234567").id == trial.id
    assert references.resolve_company(ticker="LOWR").id == company.id


def test_two_records_with_one_sec_number_are_ambiguous() -> None:
    copy = CompanyRef(id=uuid4(), cik=875320, names=("Vertex copy",), tickers=())
    references = ReferenceData(companies=[*_companies(), copy], drugs=[], trials=[])
    result = references.resolve_company(cik=875320)
    assert result.status is Status.AMBIGUOUS
    assert set(result.candidates) == {VERTEX, copy.id}


def test_two_trial_records_with_one_registry_number_are_ambiguous() -> None:
    copy = TrialRef(id=uuid4(), nct_id="NCT06111111", study_name=None, company_ids=frozenset())
    references = ReferenceData(companies=[], drugs=[], trials=[*_trials(), copy])
    result = references.resolve_trial("NCT06111111")
    assert result.status is Status.AMBIGUOUS
    assert set(result.candidates) == {AZURE1, copy.id}


# Trials named inside a longer text (part 4)


def test_study_names_written_in_capitals_are_found_in_a_sentence() -> None:
    references = _references(trials=_trials())
    found = references.trials_mentioned(
        "Topline data from AZURE-1 and AZURE 4 are due in 2026.", company_id=SPYRE
    )
    assert {(name, result.id) for name, result in found} == {("AZURE-1", AZURE1), ("AZURE 4", AZURE4)}


@pytest.mark.parametrize(
    "text",
    ["Questions about azure-1 remain.", "AZURE-14 data are due.", "The AZURE program reads out.", ""],
    ids=["not in capitals", "a longer number", "no number", "empty"],
)
def test_lower_case_words_and_near_names_are_not_trials(text: str) -> None:
    assert _references(trials=_trials()).trials_mentioned(text, company_id=SPYRE) == []


@pytest.mark.parametrize(
    "text",
    ["IMAGINE-II topline data are due.", "PRE-IMAGINE data are due.", "IMAGINE\u2013II data are due."],
    ids=["a longer study name", "a longer name before it", "an en dash"],
)
def test_a_study_name_inside_a_longer_dashed_name_is_not_that_study(text: str) -> None:
    """Mirum's IMAGINE and IMAGINE-II are two trials; "IMAGINE-II" names only the second."""
    imagine, imagine_ii = uuid4(), uuid4()
    trials = [
        TrialRef(id=imagine, nct_id="NCT02047318", study_name="IMAGINE", company_ids=frozenset({SPYRE})),
        TrialRef(
            id=imagine_ii, nct_id="NCT02117713", study_name="IMAGINE-II", company_ids=frozenset({SPYRE})
        ),
    ]
    found = _references(trials=trials).trials_mentioned(text, company_id=SPYRE)
    assert imagine not in {result.id for _, result in found}
    if text.startswith("IMAGINE"):
        assert [result.id for _, result in found] == [imagine_ii]


@pytest.mark.parametrize(
    "text",
    ["Topline data from IMAGINE\u2014the Phase 3 study\u2014are due.",
     "The pivotal trial\u2014IMAGINE\u2014reads out.",
     "Topline data from IMAGINE\u2015are due."],
    ids=["em dash after", "em dashes around", "horizontal bar"],
)  # fmt: skip
def test_a_long_dash_used_as_punctuation_still_ends_the_study_name(text: str) -> None:
    """Writers set em dashes against words; only a hyphen or en dash joins a name to more.

    From the Opus review of PR #55.
    """
    imagine = uuid4()
    trials = [
        TrialRef(id=imagine, nct_id="NCT02047318", study_name="IMAGINE", company_ids=frozenset({SPYRE}))
    ]
    found = _references(trials=trials).trials_mentioned(text, company_id=SPYRE)
    assert [result.id for _, result in found] == [imagine]


def test_registry_numbers_in_a_sentence_are_each_reported() -> None:
    found = _references(trials=_trials()).trials_mentioned(
        "See NCT06111111 and NCT09999999.", company_id=SPYRE
    )
    assert [(name, result.status) for name, result in found] == [
        ("NCT06111111", Status.RESOLVED),
        ("NCT09999999", Status.UNKNOWN),
    ]
