"""The indication's own sentence, found by plain code (ADR 0053, decision 2 as amended).

The extractor already reads an indication for each catalyst. It may be published only when one
visible sentence of the stored source states that the drug is aimed at that disease, and the
disease is one MeSH disease. A sentence that only mentions both is not enough (Opus review of
PR 124). The printed words are the sentence's own.
"""

import time
from functools import cache
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from catalyst.extract.indication import Found, IndicationMiss, clauses, indication_sentence, sentences
from catalyst.mesh import MeshIndex, Miss

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mesh" / "desc2026_excerpt.xml.gz"
TEXT = (
    "Acme Bio Reports Third Quarter 2026 Results\n"
    "Topline data from the Phase 3 RAINIER trial are expected in the second half of 2027.\n"
    "• Zentocel is in a Phase 3 trial in Relapsed/Refractory Acute Myeloid Leukemia; "
    "enrollment is complete. Otherumab is in a Phase 1 trial in melanoma.\n"
    "Zentocel was well tolerated."
)


@cache
def _index() -> MeshIndex:
    return MeshIndex.from_xml(FIXTURE.read_bytes())


def _find(
    drug: str, indication: str, text: str = TEXT, visible: str | None = None, others: tuple[str, ...] = ()
) -> Found | IndicationMiss | Miss:
    seen = text if visible is None else visible
    return indication_sentence(text, seen, drug, indication, _index(), other_drugs=others)


def test_sentences_split_at_full_stops_semicolons_line_breaks_and_bullets() -> None:
    spans = [TEXT[start:end] for start, end in sentences(TEXT)]
    assert "Zentocel is in a Phase 3 trial in Relapsed/Refractory Acute Myeloid Leukemia;" in spans
    assert "Otherumab is in a Phase 1 trial in melanoma." in spans
    assert all(span == span.strip() and span for span in spans)


def test_a_clause_ends_at_and_but_while_and_whereas() -> None:
    text = "Zentocel is in a trial in AML, and Otherumab is in a trial in melanoma."
    assert [text[a:b] for a, b in clauses(text)] == [
        "Zentocel is in a trial in AML",
        "Otherumab is in a trial in melanoma.",
    ]


def test_the_sentence_stating_the_indication_is_found_with_the_sources_own_words() -> None:
    found = _find("zentocel", "relapsed/refractory acute myeloid leukemia (AML)")
    assert isinstance(found, Found)
    assert (
        found.sentence.quote
        == "Zentocel is in a Phase 3 trial in Relapsed/Refractory Acute Myeloid Leukemia;"
    )
    assert TEXT[found.sentence.start : found.sentence.end] == found.sentence.quote
    assert found.words == "Acute Myeloid Leukemia"  # printed as the source writes it
    assert found.descriptor.name == "Leukemia, Myeloid, Acute"


@pytest.mark.parametrize(
    "sentence",
    [
        "Zentocel is being evaluated in patients with metastatic melanoma.",
        "Zentocel, an oral monotherapy, in melanoma is on track.",
        "Zentocel is in development for the treatment of melanoma.",
        "A Phase 2 trial of zentocel in melanoma began.",
    ],
)
def test_a_sentence_aiming_the_drug_at_the_disease_is_found(sentence: str) -> None:
    assert isinstance(_find("zentocel", "melanoma", text=sentence), Found)


@pytest.mark.parametrize(
    ("sentence", "indication", "reason"),
    [
        (
            "Zentocel is in a Phase 3 trial in AML, and Otherumab is in a Phase 1 trial in melanoma.",
            "melanoma",
            IndicationMiss.NO_SENTENCE,
        ),
        ("Zentocel is not being developed for melanoma.", "melanoma", IndicationMiss.NEGATED),
        (
            "Zentocel showed no activity in melanoma and is now in a Phase 3 trial in AML.",
            "melanoma",
            IndicationMiss.NEGATED,
        ),
        ("Zentocel failed in melanoma; it is now in Phase 3.", "melanoma", IndicationMiss.NEGATED),
        (
            "Patients with heart failure were excluded from the zentocel trial.",
            "heart failure",
            IndicationMiss.NEGATED,
        ),
        (
            "Zentocel did not reduce heart failure hospitalizations versus placebo.",
            "heart failure",
            IndicationMiss.NEGATED,
        ),
        ("Unlike melanoma, zentocel targets other cancers.", "melanoma", IndicationMiss.NEGATED),
        (
            "Zentocel is in a Phase 3 trial in melanoma and heart failure.",
            "melanoma",
            IndicationMiss.OTHER_DISEASE,
        ),
        (
            "Zentocel is in a Phase 3 trial for pain due to melanoma.",
            "melanoma",
            IndicationMiss.OTHER_DISEASE,
        ),
        ("Zentocel is in a Phase 3 trial for fatigue due to melanoma.", "melanoma", IndicationMiss.CAUSE),
        (
            "Zentocel is in a Phase 1 trial in melanoma-associated retinopathy.",
            "melanoma",
            IndicationMiss.CAUSE,
        ),
        ("Zentocel is in Phase 3 for KCNT1-related epilepsy.", "epilepsy", IndicationMiss.CAUSE),
        ("Zentocel is in a Phase 3 trial with ZB-1234 in melanoma.", "melanoma", IndicationMiss.OTHER_DRUG),
        ("Zentocel shares a target with drugs used in melanoma.", "melanoma", IndicationMiss.NOT_TARGETED),
    ],
)
def test_a_sentence_that_only_mentions_both_never_backs_the_disease(
    sentence: str, indication: str, reason: IndicationMiss
) -> None:
    assert _find("zentocel", indication, text=sentence) is reason


def test_a_drug_on_file_in_the_same_sentence_blocks_it() -> None:
    sentence = "Zentocel and otherumab are in a Phase 3 trial in melanoma."
    assert _find("zentocel", "melanoma", text=sentence, others=("otherumab",)) is IndicationMiss.OTHER_DRUG


def test_a_drug_name_too_short_or_too_common_is_refused() -> None:
    assert (
        _find("the", "melanoma", text="The Phase 1 trial in melanoma began.") is IndicationMiss.UNUSABLE_DRUG
    )
    assert _find("X1", "melanoma") is IndicationMiss.UNUSABLE_DRUG


def test_no_indication_or_one_mesh_cannot_name_gives_the_reason() -> None:
    assert _find("zentocel", "") is IndicationMiss.NO_INDICATION
    assert _find("zentocel", "advanced solid tumors") is Miss.NO_DISEASE
    assert _find("zentocel", "liver cirrhosis with portal hypertension") is Miss.SEVERAL_DISEASES


def test_printed_words_never_hold_brackets() -> None:
    sentence = "Zentocel is in a Phase 3 trial in Acute (not) Myeloid Leukemia."
    assert not isinstance(_find("zentocel", "acute myeloid leukemia", text=sentence), Found)


def test_a_sentence_readers_cannot_see_is_never_used() -> None:
    hidden = TEXT.replace("Zentocel is in a Phase 3 trial in Relapsed/Refractory Acute Myeloid Leukemia;", "")
    assert _find("zentocel", "acute myeloid leukemia", visible=hidden) is IndicationMiss.HIDDEN


def test_invisible_characters_in_the_sentence_are_refused() -> None:
    text = TEXT.replace("Acute Myeloid", "Acute\u200b Myeloid")
    assert _find("zentocel", "acute myeloid leukemia", text=text) is IndicationMiss.INVISIBLE_CHARACTERS


def test_a_very_long_sentence_is_checked_quickly() -> None:
    filler = " ".join(["word"] * 2000)
    sentence = f"Zentocel is in a Phase 3 trial in melanoma {filler}."
    began = time.monotonic()
    assert isinstance(_find("zentocel", "melanoma", text=sentence), Found)
    assert time.monotonic() - began < 2


@given(st.text(alphabet="abcdefghij ,.;", max_size=60), st.text(alphabet="abcdefghij ,", max_size=60))
def test_a_found_sentence_is_always_the_sources_own_words(before: str, after: str) -> None:
    text = f"{before} Zentocel is in a Phase 2 trial in melanoma {after}"
    found = indication_sentence(text, text, "zentocel", "melanoma", _index())
    if isinstance(found, Found):
        assert text[found.sentence.start : found.sentence.end] == found.sentence.quote
        assert found.words in found.sentence.quote
        assert found.words.casefold() == "melanoma"


@pytest.mark.parametrize("drug", ["Zentocel", "ZENTOCEL", "zentocel"])
def test_the_drug_is_found_whatever_its_case(drug: str) -> None:
    assert isinstance(_find(drug, "acute myeloid leukemia"), Found)
