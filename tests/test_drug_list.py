"""The drug list: names from Drugs@FDA and ClinicalTrials.gov, and which company each belongs to (ADR 0035).

Plain code over recorded copies of real public records (`tests/fixtures/drug-list/`, recorded
2026-10-19): Drugs@FDA applications of Insmed, Pfizer, Kura, Incyte, Vertex, and GSK, and every
trial Insmed and Kura lead on ClinicalTrials.gov. A test that needs a case the records lack
changes a copy of one, and says how.

ADR 0015 kept registry interventions out because Insmed's records list a comparator
(azithromycin), placebos, and short forms ("ETH"). These tests hold each of those out, keep a
name two companies' records give for neither, and keep each name's records.
"""

import copy
import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from catalyst.collectors.sources.ctgov import lead_sponsor_search
from catalyst.drug_list import (
    DrugPlan,
    ListedName,
    SourceRecord,
    fda_records,
    fda_sponsor_key,
    generic_name,
    merge_with_existing,
    plan_drugs,
    registry_names,
    registry_records,
)
from catalyst.resolve import DrugRef, name_key
from catalyst.schemas import DrugNameKind, DrugNameSource
from conftest import FIXTURES

FOLDER = FIXTURES / "drug-list"
INSMED, KURA, PFIZER, INCYTE = uuid4(), uuid4(), uuid4(), uuid4()


def _read(name: str) -> dict[str, Any]:
    return json.loads((FOLDER / name).read_text())


def _applications() -> list[dict[str, Any]]:
    return _read("00_api.fda.gov_drug_drugsfda.json")["results"]


def _studies(name: str) -> list[dict[str, Any]]:
    return _read(name)["studies"]


INSMED_TRIALS = "01_clinicaltrials.gov_api_v2_studies"
KURA_TRIALS = "02_clinicaltrials.gov_api_v2_studies"


def _sponsors(**extra: UUID) -> dict[DrugNameSource, dict[str, UUID]]:
    fda = {fda_sponsor_key(n): c for n, c in [("INSMED Inc", INSMED), ("PFIZER INC", PFIZER)]}
    fda |= {fda_sponsor_key("Kura Oncology, Inc."): KURA, fda_sponsor_key("INCYTE CORP"): INCYTE}
    registry = {"Insmed Incorporated": INSMED, "Kura Oncology, Inc.": KURA} | extra
    return {DrugNameSource.OPENFDA: fda, DrugNameSource.CTGOV: registry}


def _records() -> list[SourceRecord]:
    records = [r for a in _applications() for r in fda_records(a)]
    return records + [
        r for name in (INSMED_TRIALS, KURA_TRIALS) for s in _studies(name) for r in registry_records(s)
    ]


def _drug_of(plan: DrugPlan, company: UUID, name: str) -> dict[str, tuple[str, DrugNameKind, DrugNameSource]]:
    """Every name of the company's planned drug that goes by `name`, keyed as the resolver compares."""
    for drug in plan.drugs:
        if drug.company_id == company and name_key(name) in drug.names:
            return {k: (n.name, n.kind, n.source) for k, n in drug.names.items()}
    return {}


def _names_of(plan: DrugPlan, company: UUID) -> set[str]:
    return {key for drug in plan.drugs if drug.company_id == company for key in drug.names}


# Drugs@FDA


@pytest.mark.parametrize(
    ("ingredient", "generic"),
    [
        ("RUXOLITINIB PHOSPHATE", "RUXOLITINIB"),
        ("OLEZARSEN SODIUM", "OLEZARSEN"),
        ("DOSTARLIMAB-GXLY", "DOSTARLIMAB"),
        ("AMIKACIN SULFATE", "AMIKACIN"),
        ("SODIUM OXYBATE", "SODIUM OXYBATE"),  # a metal written first names the product
        ("FLUTICASONE PROPIONATE", "FLUTICASONE PROPIONATE"),  # an ester is another product
        ("SODIUM", "SODIUM"),
    ],
)
def test_an_fda_ingredient_s_generic_name_drops_salt_and_biologic_suffix(
    ingredient: str, generic: str
) -> None:
    assert generic_name(ingredient) == generic


def test_an_approved_application_gives_its_brand_and_single_ingredient_generic_name() -> None:
    by_number = {r.ref: r for a in _applications() for r in fda_records(a)}
    brinsupri = by_number["NDA217673"]
    assert (brinsupri.source, brinsupri.sponsor) == (DrugNameSource.OPENFDA, "INSMED INC")
    assert [(n.name, n.kind) for n in brinsupri.names] == [
        ("BRENSOCATIB", DrugNameKind.INN),
        ("BRINSUPRI", DrugNameKind.BRAND),
    ]
    # "ARIKAYCE KIT" is the brand ARIKAYCE; "DOSTARLIMAB-GXLY" is dostarlimab.
    assert [n.name for n in by_number["NDA207356"].names] == ["AMIKACIN", "ARIKAYCE"]
    assert [n.name for n in by_number["BLA761174"].names] == ["DOSTARLIMAB", "JEMPERLI"]


def test_a_combination_product_gives_only_its_brand() -> None:
    alyftrek = next(r for a in _applications() for r in fda_records(a) if r.ref == "NDA218730")
    assert [(n.name, n.kind) for n in alyftrek.names] == [("ALYFTREK", DrugNameKind.BRAND)]
    assert alyftrek.ingredient is None
    assert {"TEZACAFTOR", "VANZACAFTOR"} <= set(alyftrek.other_names)  # kept for the comparator rule


@pytest.mark.parametrize("number", ["ANDA077201", "BLA761174"])
def test_a_generic_copy_or_an_application_without_an_approval_gives_nothing(number: str) -> None:
    """A copy of GSK's JEMPERLI record, renumbered as an ANDA, or with its approvals marked tentative."""
    application = copy.deepcopy(next(a for a in _applications() if a["application_number"] == "BLA761174"))
    if number.startswith("ANDA"):
        application["application_number"] = number
    else:
        for submission in application["submissions"]:
            submission["submission_status"] = "TA"
    assert fda_records(application) == []


def test_fda_abbreviations_are_spelled_out_as_company_names_are_compared() -> None:
    assert fda_sponsor_key("VERTEX PHARMS INC") == fda_sponsor_key("VERTEX PHARMACEUTICALS INC / MA")
    assert fda_sponsor_key("SAREPTA THERAPS INC") == fda_sponsor_key("Sarepta Therapeutics, Inc.")
    assert fda_sponsor_key("BIOMARIN PHARM") == fda_sponsor_key("BIOMARIN PHARMACEUTICAL INC")
    assert fda_sponsor_key("UNITED GUARDIAN") != fda_sponsor_key("UNITED THERAPEUTICS Corp")


def test_an_fda_sponsor_cut_short_names_the_company_only_when_its_labels_do() -> None:
    """FDA writes Kura Oncology as "KURA"; its label names "Kura Oncology, Inc." in full."""
    plan = plan_drugs([r for a in _applications() for r in fda_records(a)], _sponsors())
    assert {"ziftomenib", "komzifti"} <= set(_drug_of(plan, KURA, "ziftomenib"))
    record = next(r for a in _applications() for r in fda_records(a) if r.ref == "NDA220305")
    elsewhere = SourceRecord(
        record.source, record.ref, record.sponsor, record.names, labelers=("Acme Kura LLC",)
    )
    other = plan_drugs([elsewhere], _sponsors())
    assert other.drugs == []


# ClinicalTrials.gov names


@pytest.mark.parametrize(
    "text",
    [
        "Placebo",
        "ELC (matching placebo for ALIS)",
        "Vehicle cream",
        "Standard of care",
        "Investigator's choice chemotherapy",
        "Saline",
        "Brensocatib + azithromycin",
        "Ziftomenib/venetoclax",
        "TSC-102-A01 and TSC-102-A03",
        "SPY072 plus IL-17A/F",
    ],
)
def test_stand_ins_and_combinations_are_not_drug_names(text: str) -> None:
    assert registry_names(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ETH", []),  # a short form (ADR 0015)
        ("ATEV", []),
        ("CAR-T cells", []),
        ("Anti C1q Antibody", []),
        ("14C", []),
        ("Active BMN 111: Subcutaneous injection of recommended dose", []),
        ("Brensocatib 25 mg", [("Brensocatib", DrugNameKind.INN)]),
        ("Oral brensocatib tablets", [("brensocatib", DrugNameKind.INN)]),
        ("Selinexor (KPT-330)", [("Selinexor", DrugNameKind.INN), ("KPT-330", DrugNameKind.CODE)]),
        ("ARIKACE™", [("ARIKACE", DrugNameKind.BRAND)]),
        ("4D-150", [("4D-150", DrugNameKind.CODE)]),
        ("AKCEA-APO(a)-LRx", [("AKCEA-APO(a)-LRx", DrugNameKind.INN)]),
        ("Previously REOLYSIN", [("REOLYSIN", DrugNameKind.BRAND)]),
        ("IL-23 blocker", []),
    ],
)
def test_registry_names_are_cleaned_and_short_forms_dropped(
    text: str, expected: list[tuple[str, DrugNameKind]]
) -> None:
    assert [(n.name, n.kind) for n in registry_names(text) or []] == expected


@given(
    st.text(max_size=20),
    st.sampled_from(["placebo", "Placebo", "PLACEBO", "vehicle", "sham"]),
    st.text(max_size=20),
)
def test_no_name_with_a_stand_in_word_is_ever_kept(before: str, word: str, after: str) -> None:
    assert registry_names(f"{before} {word} {after}") is None


def _with_arms(study: dict[str, Any]) -> bool:
    module = study["protocolSection"].get("armsInterventionsModule", {})
    types = {arm.get("type") for arm in module.get("armGroups", [])}
    return {"EXPERIMENTAL", "PLACEBO_COMPARATOR"} <= types and "INS1007" in json.dumps(module)


def test_an_intervention_given_only_in_comparator_arms_names_no_drug() -> None:
    """An Insmed brensocatib trial with a placebo arm; then a copy whose every arm is a comparator."""
    study = next(s for s in _studies(INSMED_TRIALS) if _with_arms(s))
    names = {name_key(n.name) for r in registry_records(study) for n in r.names}
    assert "brensocatib" in names
    assert not any("placebo" in key for key in names)
    changed = copy.deepcopy(study)
    for arm in changed["protocolSection"]["armsInterventionsModule"]["armGroups"]:
        arm["type"] = "ACTIVE_COMPARATOR"
    assert registry_records(changed) == []


def test_a_lead_sponsor_search_is_a_short_plain_phrase() -> None:
    """The registry refuses a long phrase and reads brackets and "&" as query syntax."""
    sponsor = "ArQule, Inc., a subsidiary of Merck Sharp & Dohme LLC, a subsidiary of Merck & Co., Inc."
    sponsor += " (Rahway, NJ USA)"
    assert lead_sponsor_search(sponsor) == '"ArQule Inc a subsidiary of Merck"'
    assert lead_sponsor_search("Kura Oncology, Inc.") == '"Kura Oncology Inc"'


# The plan


def test_a_registry_name_fda_lists_for_another_sponsor_is_a_comparator() -> None:
    """Insmed's trials compare with azithromycin, which FDA lists for Pfizer (ZITHROMAX)."""
    plan = plan_drugs(_records(), _sponsors())
    assert "azithromycin" not in _names_of(plan, INSMED)
    assert "zithromax" not in _names_of(plan, INSMED)
    assert {"azithromycin", "zithromax"} <= _names_of(plan, PFIZER)
    assert plan.comparators > 0


def test_a_company_s_registry_and_fda_names_join_into_one_drug_with_their_sources() -> None:
    plan = plan_drugs(_records(), _sponsors())
    drug = _drug_of(plan, INSMED, "brensocatib")
    assert drug["brensocatib"] == ("BRENSOCATIB", DrugNameKind.INN, DrugNameSource.OPENFDA)
    assert drug["brinsupri"] == ("BRINSUPRI", DrugNameKind.BRAND, DrugNameSource.OPENFDA)
    assert drug["ins1007"] == ("INS1007", DrugNameKind.CODE, DrugNameSource.CTGOV)
    assert drug["azd7986"][1:] == (DrugNameKind.CODE, DrugNameSource.CTGOV)
    planned = next(d for d in plan.drugs if "brensocatib" in d.names)
    assert planned.preferred_name == "brensocatib"  # the generic name, in small letters
    refs = planned.names["ins1007"].refs
    assert refs
    assert all(ref.startswith("NCT") for ref in refs)
    assert planned.names["brinsupri"].refs == ["NDA217673"]


def test_kura_s_ziftomenib_joins_its_code_name_and_brand() -> None:
    plan = plan_drugs(_records(), _sponsors())
    assert {"ziftomenib", "ko539", "komzifti"} <= set(_drug_of(plan, KURA, "KO-539"))


def test_trials_of_an_uncovered_sponsor_name_no_drug() -> None:
    sponsors = _sponsors()
    del sponsors[DrugNameSource.CTGOV]["Kura Oncology, Inc."]
    records = [r for s in _studies(KURA_TRIALS) for r in registry_records(s)]
    plan = plan_drugs(records, sponsors)
    assert plan.drugs == []
    assert plan.unlinked_records == len(records)


def test_a_name_two_companies_records_give_is_kept_for_neither() -> None:
    """A copy of one Insmed treprostinil palmitil (INS1009) trial, registered as led by Kura."""
    copied = copy.deepcopy(next(s for s in _studies(INSMED_TRIALS) if "INS1009" in json.dumps(s)))
    copied["protocolSection"]["sponsorCollaboratorsModule"]["leadSponsor"]["name"] = "Kura Oncology, Inc."
    plan = plan_drugs([*_records(), *registry_records(copied)], _sponsors())
    assert "ins1009" not in _names_of(plan, INSMED) | _names_of(plan, KURA)
    assert plan.withheld["ins1009"].company_ids == {INSMED, KURA}
    assert plan.withheld["ins1009"].reason == "shared"


def test_a_trial_of_another_company_s_approved_drug_names_nothing_of_its_sponsor() -> None:
    """A copy of one Insmed brensocatib trial, led by Kura: FDA approved brensocatib for Insmed."""
    copied = copy.deepcopy(next(s for s in _studies(INSMED_TRIALS) if "INS1007" in json.dumps(s)))
    copied["protocolSection"]["sponsorCollaboratorsModule"]["leadSponsor"]["name"] = "Kura Oncology, Inc."
    plan = plan_drugs([*_records(), *registry_records(copied)], _sponsors())
    assert {"brensocatib", "ins1007"} <= _names_of(plan, INSMED)
    assert not {"brensocatib", "ins1007", "azd7986"} & _names_of(plan, KURA)


def test_a_name_an_uncovered_fda_sponsor_also_holds_is_kept_for_review() -> None:
    """A copy of Pfizer's ZITHROMAX record under another sponsor: azithromycin is then shared."""
    application = copy.deepcopy(next(a for a in _applications() if a["application_number"] == "NDA050710"))
    application["sponsor_name"] = "ACME GENERICS"
    application["openfda"] = {}
    records = [*_records(), *fda_records(application)]
    plan = plan_drugs(records, _sponsors())
    assert "azithromycin" not in _names_of(plan, PFIZER)
    assert plan.withheld["azithromycin"].company_ids == {PFIZER}


def test_a_registry_record_that_joins_two_approved_products_is_left_out() -> None:
    """Two constructed Insmed registry records that share one code: one names brensocatib, one amikacin.

    (Since the Opus review of PR #73 a free-text other name such as "AMIKACIN" is no name, so
    only a shared code can join two products now.)
    """
    shared = ListedName("INS-0001", DrugNameKind.CODE)
    joining = [
        SourceRecord(
            DrugNameSource.CTGOV, ref, "Insmed Incorporated", (ListedName(main, DrugNameKind.INN), shared)
        )
        for ref, main in (("NCT00000001", "Brensocatib"), ("NCT00000002", "Amikacin"))
    ]
    plan = plan_drugs([*_records(), *joining], _sponsors())
    brensocatib, amikacin = _drug_of(plan, INSMED, "brensocatib"), _drug_of(plan, INSMED, "amikacin")
    assert brensocatib
    assert amikacin
    assert brensocatib != amikacin
    assert plan.split_drugs >= 1


def test_the_plan_is_the_same_whatever_the_order_of_the_records() -> None:
    def shape(plan: DrugPlan) -> set[tuple[UUID, frozenset[str]]]:
        return {(d.company_id, frozenset(d.names)) for d in plan.drugs}

    records = _records()
    assert shape(plan_drugs(records, _sponsors())) == shape(plan_drugs(list(reversed(records)), _sponsors()))


# Against the drugs on file


def test_names_join_the_company_s_drug_on_file_and_keep_its_records() -> None:
    plan = plan_drugs(_records(), _sponsors())
    on_file = DrugRef(uuid4(), INSMED, (("INS-1007", DrugNameKind.CODE),))
    merged = merge_with_existing(plan, [on_file])
    write = next(w for w in merged.writes if w.drug_id == on_file.id)
    assert "brensocatib" in {name_key(n.name) for n in write.names}
    assert "ins1007" not in {name_key(n.name) for n in write.names}  # already on file as INS-1007
    assert write.known_refs["INS-1007"]


def test_a_generic_name_already_another_company_s_is_withheld() -> None:
    plan = plan_drugs(_records(), _sponsors())
    theirs = DrugRef(uuid4(), PFIZER, (("brensocatib", DrugNameKind.INN),))
    merged = merge_with_existing(plan, [theirs])
    added = {name_key(n.name) for w in merged.writes if w.company_id == INSMED for n in w.names}
    assert "brensocatib" not in added
    assert merged.withheld["brensocatib"].company_ids == {INSMED, PFIZER}
    assert "brinsupri" in added


def test_a_code_name_another_company_s_drug_has_on_file_is_withheld() -> None:
    """One code under two companies is usually one partnered drug (second review of PR #73)."""
    plan = plan_drugs(_records(), _sponsors())
    theirs = DrugRef(uuid4(), PFIZER, (("INS1007", DrugNameKind.CODE),))
    merged = merge_with_existing(plan, [theirs])
    added = {name_key(n.name) for w in merged.writes if w.company_id == INSMED for n in w.names}
    assert "ins1007" not in added
    assert merged.withheld["ins1007"].company_ids == {INSMED, PFIZER}


def test_a_planned_drug_that_two_drugs_on_file_share_waits_for_a_person() -> None:
    plan = plan_drugs(_records(), _sponsors())
    one = DrugRef(uuid4(), INSMED, (("INS1007", DrugNameKind.CODE),))
    two = DrugRef(uuid4(), INSMED, (("BRINSUPRI", DrugNameKind.BRAND),))
    merged = merge_with_existing(plan, [one, two])
    assert merged.conflicts == 1
    assert not [w for w in merged.writes if w.drug_id in {one.id, two.id}]
