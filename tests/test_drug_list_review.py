"""Cases the Opus review of PR #73 found in the drug list (ADR 0035, "After the Opus review").

Plain code over recorded copies of real public records (`tests/fixtures/drug-list-review/`,
recorded 2026-10-19): Drugs@FDA applications of Gilead (SUNLENCA, YEZTUGO), Organon (ZETIA,
VYTORIN), AstraZeneca (BRILINTA), and Biogen (TECFIDERA, VUMERITY), and five registry records:
- NCT07683000 (Gilead), which gives "Lenacapavir Tablet" teropavimab's code GS-5423;
- NCT00552097 (Organon), which lists ZETIA and VYTORIN as other names of one intervention;
- NCT00442897 (Organon), "simvastatin (+) ezetimibe";
- NCT03126695 (AstraZeneca), "Ticagrelor immediate release (IR) tablets (Commercial tablet)";
- NCT07177209 (Pfizer), a "Non-Interventional Study" whose other names list competitors.
"""

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from catalyst.drug_list import (
    DrugPlan,
    ListedName,
    SourceRecord,
    fda_records,
    fda_sponsor_key,
    generic_name,
    plan_drugs,
    registry_names,
    registry_records,
)
from catalyst.resolve import DrugRef, name_key
from catalyst.schemas import DrugNameKind, DrugNameSource
from catalyst.verify.drug import Outcome, drug_named, drug_names
from conftest import FIXTURES

FOLDER = FIXTURES / "drug-list-review"
GILEAD, ORGANON, ASTRAZENECA, BIOGEN, PFIZER = (uuid4() for _ in range(5))


def _read(name: str) -> dict[str, Any]:
    return json.loads((FOLDER / name).read_text())


def _applications() -> list[dict[str, Any]]:
    return _read("00_api.fda.gov_drug_drugsfda.json")["results"]


def _study(nct_id: str) -> dict[str, Any]:
    studies = _read("01_clinicaltrials.gov_api_v2_studies")["studies"]
    return next(s for s in studies if s["protocolSection"]["identificationModule"]["nctId"] == nct_id)


def _sponsors() -> dict[DrugNameSource, dict[str, UUID]]:
    names = {
        "GILEAD SCIENCES INC": GILEAD,
        "Organon & Co.": ORGANON,
        "AstraZeneca PLC": ASTRAZENECA,
        "BIOGEN INC.": BIOGEN,
        "PFIZER INC": PFIZER,
    }
    registry = {
        "Gilead Sciences": GILEAD,
        "Organon and Co": ORGANON,
        "AstraZeneca": ASTRAZENECA,
        "Pfizer": PFIZER,
    }
    fda = {fda_sponsor_key(name): company for name, company in names.items()}
    return {DrugNameSource.OPENFDA: fda, DrugNameSource.CTGOV: registry}


def _plan(*trials: str) -> DrugPlan:
    records = [r for a in _applications() for r in fda_records(a)]
    records += [r for nct_id in trials for r in registry_records(_study(nct_id))]
    return plan_drugs(records, _sponsors())


def _drug(plan: DrugPlan, company: UUID, name: str) -> Any:
    return next(d for d in plan.drugs if d.company_id == company and name_key(name) in d.names)


# B1: wrong joins and wrong shown names


def test_an_other_name_two_interventions_of_one_trial_share_joins_nothing() -> None:
    """NCT07683000 gives "Lenacapavir Tablet" the other names of Teropavimab (TAB, GS-5423)."""
    plan = _plan("NCT07683000")
    lenacapavir = _drug(plan, GILEAD, "lenacapavir")
    assert {"sunlenca", "yeztugo"} <= set(lenacapavir.names)
    assert "gs5423" not in lenacapavir.names
    assert "teropavimab" not in lenacapavir.names
    assert lenacapavir.preferred_name == "lenacapavir"
    assert _drug(plan, GILEAD, "teropavimab").preferred_name == "Teropavimab"


def test_a_combination_product_never_joins_a_single_ingredient_drug() -> None:
    """NCT00552097 lists ZETIA and VYTORIN under one intervention; VYTORIN is ezetimibe and simvastatin."""
    plan = _plan("NCT00552097", "NCT00442897")
    zetia, vytorin = _drug(plan, ORGANON, "ZETIA"), _drug(plan, ORGANON, "VYTORIN")
    assert zetia is not vytorin
    assert zetia.preferred_name == "ezetimibe"
    assert vytorin.preferred_name == "VYTORIN"
    assert "ezetimibe" not in vytorin.names


def test_a_registry_record_that_joins_a_combination_and_a_single_drug_is_left_out() -> None:
    """Two constructed registry records sharing one code: one names VYTORIN, the other ZETIA."""
    shared = ListedName("SCH 58235", DrugNameKind.CODE)
    joining = [
        SourceRecord(
            DrugNameSource.CTGOV,
            "NCT00000001",
            "Organon and Co",
            (ListedName("VYTORIN", DrugNameKind.BRAND), shared),
        ),
        SourceRecord(
            DrugNameSource.CTGOV,
            "NCT00000002",
            "Organon and Co",
            (ListedName("ZETIA", DrugNameKind.BRAND), shared),
        ),
    ]
    records = [r for a in _applications() for r in fda_records(a)] + joining
    plan = plan_drugs(records, _sponsors())
    assert _drug(plan, ORGANON, "ZETIA") is not _drug(plan, ORGANON, "VYTORIN")
    assert plan.split_drugs == len(joining)


def test_the_shown_name_is_fda_s_generic_name_never_a_registry_other_name() -> None:
    plan = _plan("NCT03126695")
    assert _drug(plan, ASTRAZENECA, "BRILINTA").preferred_name == "ticagrelor"


def test_a_planned_drug_s_shown_name_ranks_fda_names_first() -> None:
    """A constructed registry record that names TECFIDERA by a misspelled generic name."""
    misspelled = SourceRecord(
        DrugNameSource.CTGOV,
        "NCT00000003",
        "Biogen",
        (ListedName("Dimethylfumarat", DrugNameKind.INN), ListedName("TECFIDERA", DrugNameKind.BRAND)),
    )
    sponsors = _sponsors()
    sponsors[DrugNameSource.CTGOV]["Biogen"] = BIOGEN
    plan = plan_drugs([*(r for a in _applications() for r in fda_records(a)), misspelled], sponsors)
    assert _drug(plan, BIOGEN, "TECFIDERA").preferred_name == "dimethyl fumarate"


# B2: free-text other names never become names


def test_a_brilinta_claim_with_a_quote_about_something_else_is_held() -> None:
    """The review's case: "(Commercial tablet)" must not make BRILINTA named in a launch sentence."""
    plan = _plan("NCT03126695")
    ticagrelor = _drug(plan, ASTRAZENECA, "BRILINTA")
    assert "commercial" not in ticagrelor.names
    record = DrugRef(uuid4(), ASTRAZENECA, tuple((n.name, n.kind) for n in ticagrelor.names.values()))
    quote = "Commercial launch is expected in the first half of 2027."
    result = drug_named(quote, quote, 0, len(quote), drug_names("BRILINTA", [record]), ())
    assert result.outcome is Outcome.FAIL


@pytest.mark.parametrize(
    "text",
    [
        "Commercial tablet",
        "concentration",
        "brand name",
        "if necessary",
        "until May",
        "in FOLFOX",
        "as radiosensitizer",
        "Administration",
        "Non-Interventional Study",
    ],
)
def test_ordinary_words_are_no_drug_name(text: str) -> None:
    assert not registry_names(text)


def test_only_code_names_are_kept_from_a_registry_s_other_names() -> None:
    """NCT00552097's other names: brands are left to Drugs@FDA; the codes stay."""
    names = {n.name for r in registry_records(_study("NCT00552097")) for n in r.names}
    assert "VYTORIN" not in names
    assert "ZETIA" not in names


def test_a_record_with_no_drug_main_name_gives_nothing() -> None:
    """NCT07177209's intervention is a "Non-Interventional Study" listing competitors, misspelled too."""
    assert registry_records(_study("NCT07177209")) == []
    assert "upadicitinib" not in {key for d in _plan("NCT07177209").drugs for key in d.names}


# S1, S2


@pytest.mark.parametrize(
    ("ingredient", "generic"),
    [
        ("DIMETHYL FUMARATE", "DIMETHYL FUMARATE"),  # an ester, not a salt
        ("DIROXIMEL FUMARATE", "DIROXIMEL FUMARATE"),
        ("MONOMETHYL FUMARATE", "MONOMETHYL FUMARATE"),
        ("QUETIAPINE FUMARATE", "QUETIAPINE"),  # a salt
        ("LENACAPAVIR SODIUM", "LENACAPAVIR"),
    ],
)
def test_fumarate_esters_keep_their_whole_name(ingredient: str, generic: str) -> None:
    assert generic_name(ingredient) == generic


def test_tecfidera_and_vumerity_are_named_by_their_whole_generic_names() -> None:
    plan = _plan()
    assert _drug(plan, BIOGEN, "TECFIDERA").preferred_name == "dimethyl fumarate"
    assert _drug(plan, BIOGEN, "VUMERITY").preferred_name == "diroximel fumarate"
    assert not any(key in {"dimethyl", "diroximel"} for d in plan.drugs for key in d.names)


@pytest.mark.parametrize(
    "text", ["simvastatin (+) ezetimibe", "ezetimibe (plus simvastatin)", "X-100 (with Y-200)"]
)
def test_a_join_inside_brackets_is_a_combination(text: str) -> None:
    assert registry_names(text) is None


def test_a_registry_name_in_capitals_is_a_brand_and_shown_as_written() -> None:
    """Sarepta's registry records name ELEVIDYS in capitals; a generic name is shown before it.

    Since the fourth review, registry word main names join each other only, not codes, so the two
    records here join through "ELEVIDYS" listed under the generic name.
    """
    assert [(n.name, n.kind) for n in registry_names("ELEVIDYS") or []] == [("ELEVIDYS", DrugNameKind.BRAND)]
    records = [
        SourceRecord(
            DrugNameSource.CTGOV, "NCT00000004", "Biogen", (ListedName("ELEVIDYS", DrugNameKind.BRAND),)
        ),
        SourceRecord(
            DrugNameSource.CTGOV,
            "NCT00000005",
            "Biogen",
            (
                ListedName("delandistrogene moxeparvovec", DrugNameKind.INN),
                ListedName("ELEVIDYS", DrugNameKind.BRAND, main=False),
            ),
        ),
    ]
    code_only = SourceRecord(
        DrugNameSource.CTGOV, "NCT00000006", "Biogen", (ListedName("PBGENE-DMD", DrugNameKind.BRAND),)
    )
    sponsors = _sponsors()
    sponsors[DrugNameSource.CTGOV]["Biogen"] = BIOGEN
    plan = plan_drugs([*records, code_only], sponsors)
    assert _drug(plan, BIOGEN, "ELEVIDYS").preferred_name == "delandistrogene moxeparvovec"
    assert _drug(plan, BIOGEN, "PBGENE-DMD").preferred_name == "PBGENE-DMD"
