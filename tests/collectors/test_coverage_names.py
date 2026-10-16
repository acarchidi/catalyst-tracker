"""Company names match a trial sponsor only when they are the same name, written differently.

SEC and ClinicalTrials.gov spell names differently ("VERTEX PHARMACEUTICALS INC / MA" and
"Vertex Pharmaceuticals Incorporated"). Matching ignores case, punctuation, legal-form words,
and EDGAR's state suffix, and nothing else: a partial or prefix match is not a match.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from catalyst.collectors.coverage import normalize_name, same_company, sponsor_of


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
        ("Viridian Therapeutics, Inc.\\DE", "Viridian Therapeutics, Inc."),
        ("LIFECORE BIOMEDICAL, INC. \\DE\\", "Lifecore Biomedical"),
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


@given(
    names, st.sampled_from(["Inc", "Inc.", ", Inc.", "Corporation", "Ltd", "plc", "/DE/", " / MA", "\\DE\\"])
)
def test_legal_form_suffixes_never_change_the_match(name: str, suffix: str) -> None:
    if normalize_name(name):
        assert same_company(name, f"{name} {suffix}")


@pytest.mark.parametrize(
    ("sec_name", "sponsor"),
    [
        ("ADC Therapeutics SA", "ADC Therapeutics S.A."),
        ("GENMAB A/S", "Genmab"),
        ("uniQure N.V.", "UniQure Biopharma B.V."),
    ],
)
def test_dotted_and_slashed_legal_forms_count_as_legal_forms(sec_name: str, sponsor: str) -> None:
    expected = sponsor != "UniQure Biopharma B.V."  # "Biopharma" is a different name
    assert same_company(sec_name, sponsor) is expected


@pytest.mark.parametrize(
    ("sec_name", "sponsor"),
    [
        ("GSK plc", "Stiefel, a GSK Company"),
        ("GSK plc", "Sierra Oncology LLC - a GSK company"),
        (
            "Zentalis Pharmaceuticals, Inc.",
            "K-Group, Beta, Inc., a wholly owned subsidiary of Zentalis Pharmaceuticals, Inc",
        ),
        (
            "Merck & Co., Inc.",
            "ArQule, Inc., a subsidiary of Merck Sharp & Dohme LLC, a subsidiary of Merck & Co., Inc. "
            "(Rahway, NJ USA)",
        ),
    ],
)
def test_a_sponsor_that_names_its_parent_counts_for_the_parent(sec_name: str, sponsor: str) -> None:
    assert sponsor_of(sec_name, sponsor)


@pytest.mark.parametrize(
    ("sec_name", "sponsor"),
    [
        ("BridgeBio Pharma, Inc.", "Navire Pharma Inc., a BridgeBio company"),
        ("Merck & Co., Inc.", "Merck KGaA, Darmstadt, Germany"),
        ("ICON PLC", "ICON Bioscience Inc"),
        ("Eli Lilly & Co", "Dermira, Inc."),
        ("Organon & Co.", "Organon and Co"),
    ],
)
def test_a_parent_named_differently_or_a_lookalike_does_not_count(sec_name: str, sponsor: str) -> None:
    expected = sec_name == "Organon & Co."  # same company, written differently
    assert sponsor_of(sec_name, sponsor) is expected
