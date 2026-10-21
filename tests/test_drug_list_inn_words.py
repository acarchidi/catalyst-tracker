"""Class and ordinary words that end like a WHO INN stem (ADR 0035, rule 3, "seventh review").

The seventh Opus review of PR #73 found that the stop list matched whole words only, so longer
forms ("Glycopeptide", "Neuropeptide Y", "Dinucleotide") and ordinary words with a stem's ending
("Surfactant", "Hodgkin", "Constant", "Concept") still counted as drug names. A trial's main name
like that joined the intervention's code and became the drug's shown name, so a company's claim
using the word would match the code drug with no review.
"""

import re
from uuid import uuid4

import pytest

from catalyst.drug_list import ListedName, SourceRecord, looks_like_a_drug, plan_drugs
from catalyst.drug_list_words import NOT_INN_ENDINGS
from catalyst.resolve import name_key
from catalyst.schemas import DrugNameKind, DrugNameSource
from conftest import FIXTURES

ACME = uuid4()
SPONSORS = {DrugNameSource.CTGOV: {"Acme Bio": ACME}, DrugNameSource.OPENFDA: {}}

STOP_WORDS = [
    # Class words as endings of longer words
    "Glycopeptide",
    "Neuropeptide Y",
    "Lipopeptide",
    "Dipeptide",
    "Dinucleotide",
    "Polynucleotide",
    "Oncogene",
    "Protooncogene",
    "Pseudogene",
    "Interleukin",
    # Named by the review
    "Surfactant",
    "Gonadotropin",
    "Hemostat",
    "Hodgkin",
    # Ordinary words ending in -tant, -pant, -cept, -sen
    "Constant",
    "Important",
    "Mutant",
    "Participant",
    "Occupant",
    "Concept",
    "Except",
    "Intercept",
    "Chosen",
    "Worsen",
    "Janssen",
]

REAL_INNS = [
    # -cept fusion proteins
    "etanercept",
    "abatacept",
    "aflibercept",
    "belatacept",
    "povetacicept",
    "sotatercept",
    "luspatercept",
    # -sen antisense drugs
    "olezarsen",
    "eplontersen",
    "tofersen",
    "nusinersen",
    "bepirovirsen",
    # -tant and -tide
    "aprepitant",
    "semaglutide",
    "tirzepatide",
    # -gene and -leukin: real stems, so these endings count as class words only after an ordinary prefix
    "axicabtagene",
    "aglatimagene",
    "vusolimogene",
    "onasemnogene",
    "etranacogene",
    "bempegaldesleukin",
    "aldesleukin",
    "denileukin",
]


@pytest.mark.parametrize("name", STOP_WORDS)
def test_a_class_or_ordinary_word_does_not_look_like_a_drug(name: str) -> None:
    assert not looks_like_a_drug(name)


@pytest.mark.parametrize("name", REAL_INNS)
def test_a_real_inn_with_the_same_ending_still_looks_like_a_drug(name: str) -> None:
    assert looks_like_a_drug(name)


def _fixture_words() -> set[str]:
    words: set[str] = set()
    for path in FIXTURES.rglob("*"):
        if path.is_file():
            words |= {w.casefold() for w in re.findall(r"[^\W\d_]+", path.read_text(errors="ignore"))}
    return words


def test_no_real_inn_in_the_recorded_fixtures_ends_in_a_class_ending() -> None:
    """Every recorded word that ends in "-peptide" or "-nucleotide" is a class word, not a drug."""
    ending = {w for w in _fixture_words() if w.endswith(NOT_INN_ENDINGS)}
    assert ending  # the scan reads the fixtures
    assert not [w for w in ending if looks_like_a_drug(w)]


def test_the_recorded_inns_ending_in_gene_or_leukin_still_count() -> None:
    """The recorded fixtures' real INNs that end in "-gene" or "-leukin" keep working."""
    recorded = {"axicabtagene", "aglatimagene", "vusolimogene", "bempegaldesleukin"}
    assert recorded <= _fixture_words()
    assert all(looks_like_a_drug(w) for w in recorded)


def _plan(main: str, code: str = "ACM-1234") -> list[tuple[str, list[str]]]:
    record = SourceRecord(
        DrugNameSource.CTGOV,
        "N1",
        "Acme Bio",
        (ListedName(main, DrugNameKind.INN), ListedName(code, DrugNameKind.CODE, main=False)),
    )
    return [(d.preferred_name, sorted(d.names)) for d in plan_drugs([record], SPONSORS).drugs]


@pytest.mark.parametrize("word", STOP_WORDS)
def test_a_stop_word_main_name_with_a_code_gives_the_code_alone(word: str) -> None:
    assert _plan(word) == [("ACM-1234", ["acm1234"])]


@pytest.mark.parametrize("generic", ["aflibercept", "olezarsen", "axicabtagene", "bempegaldesleukin"])
def test_a_real_inn_main_name_still_joins_its_code(generic: str) -> None:
    assert _plan(generic) == [(generic, sorted([name_key(generic), "acm1234"]))]
