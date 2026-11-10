"""The plain-code parts of a catalyst's indication (ADR 0053, decision 2 as amended 2026-11-10).

Two models read and check; plain code offers the sentences they may choose from and holds every
answer to hard checks no model can bend: the sentence is the stored source's own visible text,
the disease is written in it as given, holds no brackets, and names one MeSH disease.
"""

import time
from functools import cache
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from catalyst.extract.indication import Found, IndicationMiss, candidates, hard_check, sentences, usable_drug
from catalyst.extract.text import QuoteSpan
from catalyst.mesh import MeshIndex, Miss

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mesh" / "desc2026_excerpt.xml.gz"
TEXT = (
    "Acme Bio Reports Third Quarter 2026 Results\n"
    "Topline data from the Phase 3 RAINIER trial are expected in the second half of 2027.\n"
    "• Zentocel is in a Phase 3 trial in Relapsed/Refractory Acute Myeloid Leukemia; "
    "enrollment is complete. Otherumab is in a Phase 1 trial in melanoma.\n"
    "Zentocel was well tolerated."
)
AML = "Zentocel is in a Phase 3 trial in Relapsed/Refractory Acute Myeloid Leukemia;"


@cache
def _index() -> MeshIndex:
    return MeshIndex.from_xml(FIXTURE.read_bytes())


def _span(sentence: str) -> QuoteSpan:
    return QuoteSpan(start=0, end=len(sentence), quote=sentence)


def test_sentences_split_at_full_stops_semicolons_line_breaks_and_bullets() -> None:
    spans = [TEXT[start:end] for start, end in sentences(TEXT)]
    assert AML in spans
    assert "Otherumab is in a Phase 1 trial in melanoma." in spans
    assert all(span == span.strip() and span for span in spans)


def test_candidates_are_the_visible_sentences_naming_the_drug_in_the_sources_own_words() -> None:
    offered = candidates(TEXT, TEXT, "zentocel")
    assert [span.quote for span in offered] == [AML, "Zentocel was well tolerated."]
    assert all(TEXT[span.start : span.end] == span.quote for span in offered)


def test_a_sentence_readers_cannot_see_is_never_offered() -> None:
    hidden = TEXT.replace(AML, "")
    assert [span.quote for span in candidates(TEXT, hidden, "zentocel")] == ["Zentocel was well tolerated."]


def test_a_sentence_with_invisible_characters_is_never_offered() -> None:
    text = TEXT.replace("Acute Myeloid", "Acute​ Myeloid")
    assert AML.replace("Acute Myeloid", "Acute​ Myeloid") not in [
        s.quote for s in candidates(text, text, "zentocel")
    ]


def test_a_drug_named_only_inside_another_word_is_not_found() -> None:
    assert candidates("Prezentocel is in a Phase 3 trial in melanoma.", "", "zentocel") == []


def test_at_most_twenty_sentences_are_offered_and_quickly() -> None:
    text = " ".join(f"Zentocel trial {n} in melanoma." for n in range(5000))
    began = time.monotonic()
    assert len(candidates(text, text, "zentocel")) == 20
    assert time.monotonic() - began < 2


def test_a_drug_name_too_short_or_too_common_cannot_be_used() -> None:
    assert not usable_drug("the")
    assert not usable_drug("X1")
    assert usable_drug("ZB-1234")


def test_the_hard_check_keeps_the_sources_words_and_names_the_mesh_disease() -> None:
    found = hard_check(_span(AML), "Acute Myeloid Leukemia", _index())
    assert isinstance(found, Found)
    assert found.words == "Acute Myeloid Leukemia"
    assert found.descriptor.name == "Leukemia, Myeloid, Acute"


def test_a_disease_not_written_in_the_sentence_is_refused() -> None:
    assert hard_check(_span(AML), "melanoma", _index()) is IndicationMiss.DISEASE_NOT_IN_SENTENCE


def test_a_disease_with_brackets_inside_is_refused() -> None:
    sentence = "Zentocel is in a Phase 3 trial in Acute (not) Myeloid Leukemia."
    assert hard_check(_span(sentence), "Acute (not) Myeloid Leukemia", _index()) is IndicationMiss.BRACKETS


def test_a_disease_mesh_cannot_name_as_one_is_refused_with_its_reason() -> None:
    assert hard_check(_span("Zentocel in advanced solid tumors."), "advanced solid tumors", _index()) is (
        Miss.NO_DISEASE
    )
    sentence = "Zentocel in liver cirrhosis with portal hypertension."
    assert hard_check(_span(sentence), "liver cirrhosis with portal hypertension", _index()) is (
        Miss.SEVERAL_DISEASES
    )


@given(st.text(alphabet="abcdefghij ,.;", max_size=60), st.text(alphabet="abcdefghij ,", max_size=60))
def test_every_offered_sentence_is_the_sources_own_words(before: str, after: str) -> None:
    text = f"{before} Zentocel is in a Phase 2 trial in melanoma {after}"
    for span in candidates(text, text, "zentocel"):
        assert text[span.start : span.end] == span.quote
        assert "zentocel" in span.quote.casefold()


@pytest.mark.parametrize(
    ("sentence", "disease"),
    [
        (
            "Zentocel is in Phase 2b in cholestatic pruritus due to primary biliary cholangitis (PBC).",
            "primary biliary cholangitis",
        ),
        (
            "Zentocel is in a Phase 3 trial for anemia associated with chronic kidney disease.",
            "chronic kidney disease",
        ),
        ("Zentocel is in a Phase 3 trial for pain secondary to melanoma.", "melanoma"),
        ("Zentocel is in a Phase 1 trial in melanoma-associated retinopathy.", "melanoma"),
        ("Zentocel is in Phase 3 for melanoma related fatigue.", "melanoma"),
        ("Zentocel is in a Phase 1 trial in melanoma\u2013associated retinopathy.", "melanoma"),
    ],
)
def test_a_disease_the_sentence_gives_as_a_cause_is_refused(sentence: str, disease: str) -> None:
    assert hard_check(_span(sentence), disease, _index()) is IndicationMiss.CAUSE


def test_a_disease_named_as_the_target_is_not_taken_for_a_cause() -> None:
    sentence = "Zentocel is in a Phase 3 trial in patients with melanoma, with data due in 2027."
    assert isinstance(hard_check(_span(sentence), "melanoma", _index()), Found)


def test_a_trailing_abbreviation_in_brackets_is_dropped_from_the_disease() -> None:
    sentence = "Zentocel is in a Phase 3 trial in acute myeloid leukemia (AML)."
    found = hard_check(_span(sentence), "acute myeloid leukemia (AML)", _index())
    assert isinstance(found, Found)
    assert found.words == "acute myeloid leukemia"


def test_a_bracket_that_is_not_an_abbreviation_is_kept_and_so_refused() -> None:
    sentence = "Zentocel is in a Phase 3 trial in diabetes (type 2)."
    assert hard_check(_span(sentence), "diabetes (type 2)", _index()) is IndicationMiss.BRACKETS


def test_hidden_sentences_naming_the_drug_cannot_make_the_search_slow() -> None:
    text = " ".join(f"Zentocel hidden {n}." for n in range(3000))
    visible = "nothing " * 50000
    began = time.monotonic()
    assert candidates(text, visible, "zentocel") == []
    assert time.monotonic() - began < 3
