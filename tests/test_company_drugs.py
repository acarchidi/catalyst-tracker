"""Reading the drug a claim states into names a company-named drug may carry (ADR 0060).

Judged against ADR 0060, part 3: a strength or form word is not part of a name, a bracket gives
a second name of the same drug, a list or a combination names each drug, and words that name no
drug (a phrase, a stand-in, a date, a congress, advice) give nothing. Every name here is made up.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from catalyst.company_drugs import MOST_CHARS, STOP_WORDS, stated_drugs
from catalyst.resolve import name_key
from catalyst.schemas import DrugNameKind


def _names(text: str) -> list[list[str]] | None:
    stated = stated_drugs(text)
    return None if stated is None else [[name.name for name in drug] for drug in stated.drugs]


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("zetamab", [["zetamab"]]),
        ("QRX-4521", [["QRX-4521"]]),
        ("4QR-150", [["4QR-150"]]),
        ("BRANDEX (zetamab)", [["BRANDEX", "zetamab"]]),
        ("zetamab (QRX-4521)", [["zetamab", "QRX-4521"]]),
        ("QRX-4521 (zetamab)", [["QRX-4521", "zetamab"]]),
        ("BRANDEX foam 0.3%", [["BRANDEX"]]),
        ("zetamab cream", [["zetamab"]]),
        ("zetamab 25 mg tablets", [["zetamab"]]),
        ("subcutaneous BRANDEX", [["BRANDEX"]]),
        ("Nebulized Novabrex", [["Novabrex"]]),
        ("BRANDEX (zetamab) sublingual film", [["BRANDEX", "zetamab"]]),
        ("BRANDEX® (zetamab)", [["BRANDEX", "zetamab"]]),
        ("QRX 4521", [["QRX 4521"]]),
    ],
)
def test_one_drug_is_read_without_its_strength_and_form_and_with_its_bracketed_name(
    text: str, names: list[list[str]]
) -> None:
    stated = stated_drugs(text)
    assert _names(text) == names
    assert stated is not None
    assert not stated.combined


@pytest.mark.parametrize(
    ("text", "names", "combined"),
    [
        ("zetamab plus lumetinib", [["zetamab"], ["lumetinib"]], True),
        ("zetamab + lumetinib", [["zetamab"], ["lumetinib"]], True),
        ("zetamab with lumetinib", [["zetamab"], ["lumetinib"]], True),
        ("zetamab in combination with QRX-4521", [["zetamab"], ["QRX-4521"]], True),
        ("QRX-4521 and QRX-4522", [["QRX-4521"], ["QRX-4522"]], False),
        ("QRX-4521, QRX-4522, QRX-4523", [["QRX-4521"], ["QRX-4522"], ["QRX-4523"]], False),
        ("QRX-4521, QRX-4522 and QRX-4523", [["QRX-4521"], ["QRX-4522"], ["QRX-4523"]], False),
    ],
)
def test_a_list_or_a_combination_names_each_drug(text: str, names: list[list[str]], combined: bool) -> None:
    stated = stated_drugs(text)
    assert _names(text) == names
    assert stated is not None
    assert stated.combined is combined


def test_a_name_with_a_digit_is_a_code_and_one_in_capitals_a_brand() -> None:
    stated = stated_drugs("BRANDEX (zetamab)")
    assert stated is not None
    assert [name.kind for name in stated.drugs[0]] == [DrugNameKind.BRAND, DrugNameKind.INN]
    code = stated_drugs("QRX-4521")
    assert code is not None
    assert code.drugs[0][0].kind is DrugNameKind.CODE


@pytest.mark.parametrize(
    "text",
    [
        "the drug",
        "our lead program",
        "the combination",
        "lead candidate",
        "novel oral small molecule",
        "QRX program",
        "next-generation degrader",
        "pipeline",
        "placebo",
        "standard of care",
        "chemotherapy",
        "zetamab cell therapy",
        "FGF Receptor",
        "Taxane",
        "ASCO 2027",
        "FDA 2027",
        "QRX-2027",
        "FY2026",
        "Q4 2026",
        "2H26",
        "1H2027",
        "January",
        "BUY 100",
        "SELL",
        "COVID-19",
        "IL-23",
        "KEYNOTE-189",
        "QRX",
        "",
        "   ",
        "zetamab/lumetinib",
        "zetamab or lumetinib",
        "zetamab (lumetinib, QRX-4521)",
        "zetamab (QRX-4521) with lumetinib",
        "zetamab plus the combination",
        "zetamab and placebo",
        "(zetamab)",
        "Z" * 301,
        "zetamab " * 12,
        "Zetamabolinumab" * 5,
    ],
)
def test_words_that_name_no_drug_give_nothing(text: str) -> None:
    assert stated_drugs(text) is None


# Letters X and Z only: no disease, target, congress, agency, or advice prefix is made of them.
_CODES = st.tuples(
    st.text(alphabet="XZ", min_size=2, max_size=5), st.integers(min_value=100, max_value=1899)
).map(lambda parts: f"{parts[0]}-{parts[1]}")
_FORMS = st.sampled_from(
    ["", " foam 0.3%", " cream", " 25 mg", " tablets", " injection", " oral solution", " 300 mg capsules"]
)


@given(code=_CODES, form=_FORMS)
def test_any_code_name_is_read_whole_whatever_strength_or_form_follows(code: str, form: str) -> None:
    assert _names(code + form) == [[code]]


@given(
    first=_CODES,
    second=_CODES,
    joiner=st.sampled_from([" plus ", " + ", " with ", " in combination with ", " and ", ", "]),
)
def test_any_two_code_names_joined_are_read_as_two_drugs(first: str, second: str, joiner: str) -> None:
    stated = stated_drugs(first + joiner + second)
    assert stated is not None
    assert [[name.name for name in drug] for drug in stated.drugs] == [[first], [second]]
    assert stated.combined is (joiner.strip() not in {"and", ","})


@given(st.text(max_size=120))
def test_every_name_read_is_written_in_the_words_and_within_the_rules(text: str) -> None:
    stated = stated_drugs(text)
    if stated is None:
        return
    for drug in stated.drugs:
        assert drug
        for name in drug:
            assert name_key(name.name)
            assert name_key(name.name) in name_key(text)
            assert len(name.name) <= MOST_CHARS
            assert not {word.casefold() for word in name.name.split()} & STOP_WORDS


@given(st.lists(st.sampled_from(sorted(STOP_WORDS)), min_size=1, max_size=5))
def test_a_phrase_of_stop_words_never_names_a_drug(words: list[str]) -> None:
    assert stated_drugs(" ".join(words)) is None
