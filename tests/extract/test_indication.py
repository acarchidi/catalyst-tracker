"""The indication's own sentence, found by plain code (ADR 0053, decision 2 as amended).

The extractor already reads an indication for each catalyst. It may be published only when one
visible sentence of the stored source names both the drug and that disease, and the disease is
one MeSH disease. The printed words are the sentence's own.
"""

from functools import cache
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from catalyst.extract.indication import Found, IndicationMiss, indication_sentence, sentences
from catalyst.mesh import MeshIndex, Miss

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mesh" / "desc2026_excerpt.xml.gz"
TEXT = (
    "Acme Bio Reports Third Quarter 2026 Results\n"
    "Topline data from the Phase 3 RAINIER trial are expected in the second half of 2027.\n"
    "• Zentocel is in a Phase 3 trial in Relapsed/Refractory Acute Myeloid Leukemia (AML); "
    "enrollment is complete. Otherumab is in a Phase 1 trial in melanoma.\n"
    "Zentocel was well tolerated."
)


@cache
def _index() -> MeshIndex:
    return MeshIndex.from_xml(FIXTURE.read_bytes())


def _find(
    drug: str, indication: str, text: str = TEXT, visible: str | None = None
) -> Found | IndicationMiss | Miss:
    return indication_sentence(text, text if visible is None else visible, drug, indication, _index())


def test_sentences_split_at_full_stops_semicolons_line_breaks_and_bullets() -> None:
    spans = [TEXT[start:end] for start, end in sentences(TEXT)]
    assert "Zentocel is in a Phase 3 trial in Relapsed/Refractory Acute Myeloid Leukemia (AML);" in spans
    assert "Otherumab is in a Phase 1 trial in melanoma." in spans
    assert all(span == span.strip() and span for span in spans)


def test_the_sentence_naming_the_drug_and_the_disease_is_found_with_the_sources_own_words() -> None:
    found = _find("zentocel", "relapsed/refractory acute myeloid leukemia (AML)")
    assert isinstance(found, Found)
    assert (
        found.sentence.quote
        == "Zentocel is in a Phase 3 trial in Relapsed/Refractory Acute Myeloid Leukemia (AML);"
    )
    assert TEXT[found.sentence.start : found.sentence.end] == found.sentence.quote
    assert found.words == "Acute Myeloid Leukemia"  # printed as the source writes it
    assert found.descriptor.name == "Leukemia, Myeloid, Acute"


def test_a_sentence_naming_another_drug_never_backs_the_disease() -> None:
    assert _find("zentocel", "melanoma") is IndicationMiss.NO_SENTENCE


def test_no_indication_or_one_mesh_cannot_name_gives_the_reason() -> None:
    assert _find("zentocel", "") is IndicationMiss.NO_INDICATION
    assert _find("zentocel", "advanced solid tumors") is Miss.NO_DISEASE
    assert _find("zentocel", "liver cirrhosis with portal hypertension") is Miss.SEVERAL_DISEASES


def test_a_sentence_readers_cannot_see_is_never_used() -> None:
    hidden = TEXT.replace(
        "Zentocel is in a Phase 3 trial in Relapsed/Refractory Acute Myeloid Leukemia (AML);", ""
    )
    assert _find("zentocel", "acute myeloid leukemia", visible=hidden) is IndicationMiss.HIDDEN


def test_invisible_characters_in_the_sentence_are_refused() -> None:
    text = TEXT.replace("Acute Myeloid", "Acute\u200b Myeloid")
    assert _find("zentocel", "acute myeloid leukemia", text=text) is IndicationMiss.INVISIBLE_CHARACTERS


@given(st.text(max_size=120), st.text(max_size=40))
def test_any_text_gives_a_sentence_or_a_reason_and_never_fails(text: str, indication: str) -> None:
    found = indication_sentence(text, text, "zentocel", indication, _index())
    if isinstance(found, Found):
        assert text[found.sentence.start : found.sentence.end] == found.sentence.quote
        assert found.words in found.sentence.quote


@pytest.mark.parametrize("drug", ["Zentocel", "ZENTOCEL", "zentocel"])
def test_the_drug_is_found_whatever_its_case(drug: str) -> None:
    assert isinstance(_find(drug, "acute myeloid leukemia"), Found)
