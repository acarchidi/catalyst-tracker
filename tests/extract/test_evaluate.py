"""Scoring the extractor against hand-checked catalysts (spec: Gold set; AGENTS.md: prompt evaluations)."""

from datetime import date

import pytest

from catalyst.extract.agent import ExtractedCatalyst, Problem
from catalyst.extract.dates import parse_window
from catalyst.extract.evaluate import GoldCatalyst, Scores, score_document, total
from catalyst.extract.text import find_quote
from catalyst.schemas.enums import CatalystType, DateConfidence

TEXT = (
    "Topline data from the Phase 3 RAINIER trial of zentocel are expected in the second half of 2027.\n"
    "The company plans to submit an NDA for zentocel by year-end 2027.\n"
    "Enrollment in BRIGHT is expected to complete in the first quarter of 2027."
)
PUBLISHED = date(2026, 10, 14)
READOUT = GoldCatalyst(
    CatalystType.TOPLINE_READOUT,
    "zentocel",
    "second half of 2027",
    "Topline data from the Phase 3 RAINIER trial of zentocel are expected in the second half of 2027.",
)
FILING = GoldCatalyst(
    CatalystType.REGULATORY_SUBMISSION,
    "zentocel",
    "by year-end 2027",
    "The company plans to submit an NDA for zentocel by year-end 2027.",
)
ENROLLMENT = GoldCatalyst(
    CatalystType.ENROLLMENT_COMPLETION,
    None,
    "first quarter of 2027",
    "Enrollment in BRIGHT is expected to complete in the first quarter of 2027.",
)


def _proposal(
    gold: GoldCatalyst,
    *,
    kind: CatalystType | None = None,
    drug: str | None = "",
    date_text: str | None = None,
) -> ExtractedCatalyst:
    words = date_text or gold.date_text
    return ExtractedCatalyst(
        catalyst_type=kind or gold.catalyst_type,
        title="t",
        drug=gold.drug if drug == "" else drug,
        indication=None,
        date_text=words,
        quote=find_quote(TEXT, gold.quote),
        window=parse_window(words, PUBLISHED),
        date_confidence=DateConfidence.COMPANY_CONFIRMED,
        problems=(),
    )


def test_a_perfect_answer_scores_one_everywhere() -> None:
    gold = [READOUT, FILING, ENROLLMENT]
    scores = total([score_document(TEXT, PUBLISHED, gold, [_proposal(g) for g in gold])])
    assert scores == Scores(
        recall=1.0, precision=1.0, catalyst_type=1.0, window=1.0, drug=1.0, quote_found=1.0
    )


def test_a_missed_event_lowers_recall_only() -> None:
    scores = total([score_document(TEXT, PUBLISHED, [READOUT, FILING], [_proposal(READOUT)])])
    assert (scores.recall, scores.precision, scores.window) == (0.5, 1.0, 1.0)


def test_an_invented_event_lowers_precision_only() -> None:
    invented = ExtractedCatalyst(
        CatalystType.PDUFA_DATE, "t", None, None, "2027", None, None, DateConfidence.REGULATOR_SCHEDULED,
        (Problem.QUOTE_NOT_FOUND,),
    )  # fmt: skip
    scores = total([score_document(TEXT, PUBLISHED, [READOUT], [_proposal(READOUT), invented])])
    assert (scores.recall, scores.precision, scores.quote_found) == (1.0, 0.5, 0.5)


def test_wrong_fields_on_a_matched_event_are_scored_per_field() -> None:
    proposals = [
        _proposal(READOUT, kind=CatalystType.CONFERENCE_PRESENTATION),
        _proposal(FILING, drug="ZENTOCEL"),  # same drug, different capitals: correct
        _proposal(ENROLLMENT, date_text="2027"),
    ]
    scores = total([score_document(TEXT, PUBLISHED, [READOUT, FILING, ENROLLMENT], proposals)])
    assert scores.recall == 1.0
    assert scores.catalyst_type == pytest.approx(2 / 3, abs=0.001)
    assert scores.drug == 1.0
    assert scores.window == pytest.approx(2 / 3, abs=0.001)


def test_scores_add_up_across_documents_by_event_not_by_document() -> None:
    one = score_document(TEXT, PUBLISHED, [READOUT, FILING, ENROLLMENT], [_proposal(READOUT)])
    two = score_document(TEXT, PUBLISHED, [READOUT], [_proposal(READOUT)])
    assert total([one, two]).recall == 0.5  # 2 of 4 gold events


def test_a_release_with_nothing_to_find_and_nothing_found_is_not_an_error() -> None:
    scores = total([score_document(TEXT, PUBLISHED, [], [])])
    assert scores == Scores(
        recall=1.0, precision=1.0, catalyst_type=1.0, window=1.0, drug=1.0, quote_found=1.0
    )


def test_a_gold_quote_that_is_not_in_the_text_is_refused() -> None:
    wrong = GoldCatalyst(CatalystType.PDUFA_DATE, None, "2027", "Words that are not there in 2027.")
    with pytest.raises(ValueError, match="gold quote"):
        score_document(TEXT, PUBLISHED, [wrong], [])
