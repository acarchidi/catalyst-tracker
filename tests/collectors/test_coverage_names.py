"""Company names match a trial sponsor only when they are the same name, written differently.

SEC and ClinicalTrials.gov spell names differently ("VERTEX PHARMACEUTICALS INC / MA" and
"Vertex Pharmaceuticals Incorporated"). Matching ignores case, punctuation, legal-form words,
and EDGAR's state suffix, and nothing else: a partial or prefix match is not a match.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from catalyst.collectors.coverage import normalize_name, same_company


@pytest.mark.parametrize(
    ("sec_name", "sponsor"),
    [
        ("VERTEX PHARMACEUTICALS INC / MA", "Vertex Pharmaceuticals Incorporated"),
        ("ELI LILLY & Co", "Eli Lilly and Company"),
        ("PFIZER INC", "Pfizer"),
        ("Alnylam Pharmaceuticals, Inc.", "ALNYLAM PHARMACEUTICALS, INC."),
        ("INSMED Inc", "Insmed Incorporated"),
        ("BioMarin Pharmaceutical Inc.", "BioMarin Pharmaceutical"),
        ("Ionis Pharmaceuticals Inc", "Ionis Pharmaceuticals, Inc."),
        ("argenx SE", "argenx"),
        ("BRISTOL MYERS SQUIBB CO", "Bristol-Myers Squibb"),
        ("Neurocrine Biosciences Inc /CA/", "Neurocrine Biosciences"),
    ],
)
def test_same_company_written_differently_matches(sec_name: str, sponsor: str) -> None:
    assert same_company(sec_name, sponsor)


@pytest.mark.parametrize(
    ("sec_name", "sponsor"),
    [
        ("Regeneron Pharmaceuticals Inc", "Regeneron"),
        ("Moderna, Inc.", "ModernaTX, Inc."),
        ("Vertex Pharmaceuticals Inc", "Vertex Energy Inc"),
        ("Gilead Sciences Inc", "Gilead Sciences Europe Ltd"),
        ("Alpha Therapeutics Inc", "Alpha Therapeutics Holdings Inc"),
        ("Corp", "Inc"),
    ],
)
def test_different_names_do_not_match(sec_name: str, sponsor: str) -> None:
    assert not same_company(sec_name, sponsor)


def test_a_name_made_only_of_legal_words_never_matches() -> None:
    assert normalize_name("Inc. Ltd.") == ""
    assert not same_company("Inc", "Inc")


names = st.text(alphabet=st.characters(categories=("L", "N", "P", "Zs")), max_size=40)


@given(names)
def test_normalizing_is_stable(name: str) -> None:
    once = normalize_name(name)
    assert normalize_name(once) == once


@given(names, st.sampled_from(["Inc", "Inc.", ", Inc.", "Corporation", "Ltd", "plc", "/DE/", " / MA"]))
def test_legal_form_suffixes_never_change_the_match(name: str, suffix: str) -> None:
    if normalize_name(name):
        assert same_company(name, f"{name} {suffix}")
