"""Titles composed in code from checked fields only (CP4 review, B1; ADR 0018, part 5d)."""

import pytest

from catalyst.publish.title import compose_title
from catalyst.schemas import CatalystType
from catalyst.verify.event_type import LABELS


def test_the_label_alone_when_no_drug_or_trial_is_matched() -> None:
    assert compose_title(CatalystType.OFFERING, [], []) == "Offering"


def test_drugs_then_trials_each_in_a_fixed_order() -> None:
    title = compose_title(
        CatalystType.TOPLINE_READOUT, ["semaglutide", "pegozafermin"], ["SYMMETRY", "RAINIER"]
    )
    assert title == "Topline data: pegozafermin and semaglutide, RAINIER and SYMMETRY trials"
    assert compose_title(CatalystType.PDUFA_DATE, ["A", "c", "B"], []) == "FDA decision date: A, B and c"
    assert compose_title(CatalystType.ENROLLMENT_COMPLETION, [], ["NCT06000002"]) == (
        "Enrollment completion: NCT06000002 trial"
    )


@pytest.mark.parametrize("kind", list(CatalystType))
def test_every_word_comes_from_the_label_or_the_names_given(kind: CatalystType) -> None:
    title = compose_title(kind, ["drugname"], ["STUDY"])
    assert title == f"{LABELS[kind]}: drugname, STUDY trial"
