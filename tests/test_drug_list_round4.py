"""Cases the fourth Opus review of PR #73 found in the drug list (ADR 0035, rule 3, "fourth review").

Plain code over recorded copies of real public records (`tests/fixtures/drug-list-round4/`,
recorded 2026-10-20) and constructed records:
- ClinicalTrials.gov: Bristol Myers Squibb's BMS-275183 trials, one written "BMS-275183 (oral
  taxane)", and an "Oral Taxane" trial (NCT00099879, NCT00207025, NCT00359450, NCT00103831);
  Lilly's "FGF Receptor" with LY2874455 (NCT01212107); Sanofi's "Avidin" with SSR29261
  (NCT00311090) and "Yellow Fever" with YF VAX® (NCT01488890); Pfizer's "Anti-Beta Interferon
  (PF-06823859)" (NCT05192200, NCT03181893); Janssen's TAR-210 with JNJ-42756493, erdafitinib's
  code (NCT06919965, NCT02218073); and Shire's SPD476 with mesalazine (NCT00151944, NCT00151892).
"""

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from catalyst.drug_list import (
    DrugPlan,
    ListedName,
    SourceRecord,
    fda_sponsor_key,
    plan_drugs,
    registry_names,
    registry_records,
)
from catalyst.resolve import DrugRef, name_key
from catalyst.schemas import DrugNameKind, DrugNameSource
from catalyst.verify.drug import Outcome, drug_named, drug_names, other_drugs
from conftest import FIXTURES

FOLDER = FIXTURES / "drug-list-round4"
REGISTERED = {
    "Bristol-Myers Squibb": "BMY",
    "Eli Lilly and Company": "LLY",
    "Sanofi": "SNY",
    "Sanofi Pasteur, a Sanofi Company": "SNY",
    "Pfizer": "PFE",
    "Janssen Research & Development, LLC": "JNJ",
    "Shire": "TAK",
    "Acme Bio": "ACME",
}
COMPANY = {ticker: uuid4() for ticker in set(REGISTERED.values())}
ACME = "Acme Bio"
SPONSORS: dict[DrugNameSource, dict[str, UUID]] = {
    DrugNameSource.CTGOV: {n: COMPANY[t] for n, t in REGISTERED.items()},
    DrugNameSource.OPENFDA: {fda_sponsor_key("ACME BIO"): COMPANY["ACME"]},
}


def _records() -> list[SourceRecord]:
    studies = json.loads((FOLDER / "01_clinicaltrials.gov_api_v2_studies").read_text())["studies"]
    return [r for s in studies for r in registry_records(s)]


@pytest.fixture(scope="module")
def plan() -> DrugPlan:
    return plan_drugs(_records(), SPONSORS)


def _drugs_with(plan: DrugPlan, ticker: str, name: str) -> list[Any]:
    return [d for d in plan.drugs if d.company_id == COMPANY[ticker] and name_key(name) in d.names]


def _held(plan: DrugPlan, ticker: str, claim_drug: str, quote: str) -> None:
    """Assert the claim waits for review: its drug is unknown, or the quote does not name it."""
    found = _drugs_with(plan, ticker, claim_drug)
    assert len(found) <= 1
    if not found:
        return  # "drug unknown": held
    refs = [
        DrugRef(uuid4(), d.company_id, tuple((n.name, n.kind) for n in d.names.values())) for d in plan.drugs
    ]
    record = next(
        r
        for r in refs
        if r.company_id == found[0].company_id and {name_key(n) for n, _ in r.names} == set(found[0].names)
    )
    names = drug_names(claim_drug, [record])
    result = drug_named(quote, quote, 0, len(quote), names, other_drugs(refs, {found[0].company_id}, names))
    assert result.outcome is Outcome.FAIL, (claim_drug, names)


def _code(name: str, *, main: bool = True) -> ListedName:
    return ListedName(name, DrugNameKind.CODE, main)


def _trial(ref: str, main: ListedName, *others: ListedName) -> SourceRecord:
    return SourceRecord(DrugNameSource.CTGOV, ref, ACME, (main, *others))


def _approved(ref: str, generic: str) -> SourceRecord:
    return SourceRecord(
        DrugNameSource.OPENFDA,
        ref,
        "ACME BIO",
        (ListedName(generic, DrugNameKind.INN),),
        ingredient=name_key(generic),
    )


def _names(plan: DrugPlan) -> list[list[str]]:
    return sorted(sorted(d.names) for d in plan.drugs)


# Blocking 1: a combination's parts, listed one per trial, and chains of aliases


def test_a_combination_s_parts_listed_in_two_trials_join_none_of_them() -> None:
    """The reviewer's case: ACM-3112's two trials list one part each; the parts have trials of their own."""
    plan = plan_drugs(
        [
            _trial("N1", _code("ACM-3112"), _code("ACM-3100", main=False)),
            _trial("N2", _code("ACM-3112"), _code("ACM-9012", main=False)),
            _trial("N3", _code("ACM-3100")),
            _trial("N4", _code("ACM-9012")),
        ],
        SPONSORS,
    )
    assert _names(plan) == [["acm3100"], ["acm3112"], ["acm9012"]]


def test_a_code_listing_one_other_code_drug_does_not_join_it() -> None:
    """Two drugs that each have a code main name never join through an alias."""
    plan = plan_drugs(
        [_trial("N1", _code("ACM-3112"), _code("ACM-3100", main=False)), _trial("N2", _code("ACM-3100"))],
        SPONSORS,
    )
    assert _names(plan) == [["acm3100"], ["acm3112"]]


@pytest.mark.parametrize(
    "records",
    [
        # A's trial lists B, and B's trial lists C.
        [
            _trial("N1", _code("ACM-101"), _code("XYZ-9001", main=False)),
            _trial("N2", _code("XYZ-9001"), _code("ACM-202", main=False)),
            _trial("N3", _code("ACM-202")),
        ],
        [
            _trial("N1", _code("ACM-101"), _code("ACM-202", main=False)),
            _trial("N2", _code("ACM-202"), _code("ACM-303", main=False)),
            _trial("N3", _code("ACM-303")),
        ],
    ],
)
def test_a_chain_of_aliases_joins_nothing(records: list[SourceRecord]) -> None:
    plan = plan_drugs(records, SPONSORS)
    assert all(len([n for n in d.names.values() if n.kind is DrugNameKind.CODE]) == 1 for d in plan.drugs)
    assert len(plan.drugs) == 3


def test_one_alias_hop_still_joins_a_generic_name_and_its_code() -> None:
    """Acme's approved acmetinib is ACM-101 in a trial; ACM-101's trial lists ACM-202: only one hop joins."""
    plan = plan_drugs(
        [
            _approved("NDA000001", "ACMETINIB"),
            _trial("N1", ListedName("acmetinib", DrugNameKind.INN), _code("ACM-101", main=False)),
            _trial("N2", _code("ACM-101"), _code("ACM-202", main=False)),
            _trial("N3", _code("ACM-202")),
        ],
        SPONSORS,
    )
    assert _names(plan) == [["acm101", "acmetinib"], ["acm202"]]


# Blocking 2: a registry main name without a digit never joins codes, FDA names, or brands


@pytest.mark.parametrize(
    ("ticker", "code", "word"),
    [
        ("BMY", "BMS-275183", "taxane"),
        ("SNY", "SSR29261", "avidin"),
        ("SNY", "YF VAX", "yellowfever"),
        ("PFE", "PF-06823859", "antibetainterferon"),
    ],
)
def test_a_class_target_or_disease_word_never_joins_a_drug_s_code(
    plan: DrugPlan, ticker: str, code: str, word: str
) -> None:
    for drug in plan.drugs:
        if drug.company_id == COMPANY[ticker]:
            assert not {name_key(code), word} <= drug.names.keys()


def test_fgf_receptor_is_not_ly2874455(plan: DrugPlan) -> None:
    assert not [d for d in plan.drugs if {"fgfreceptor", "ly2874455"} <= d.names.keys()]


def test_a_bms_275183_claim_on_a_quote_about_docetaxel_a_taxane_is_held(plan: DrugPlan) -> None:
    _held(plan, "BMY", "BMS-275183", "Data for docetaxel, a taxane, are expected in 2027.")


def test_an_ly2874455_claim_on_a_quote_about_erdafitinib_an_fgf_receptor_inhibitor_is_held(
    plan: DrugPlan,
) -> None:
    _held(plan, "LLY", "LY2874455", "Data for erdafitinib, an FGF receptor inhibitor, are expected in 2027.")


def test_tar_210_and_erdafitinib_s_code_are_two_drugs(plan: DrugPlan) -> None:
    """TAR-210 releases erdafitinib (JNJ-42756493) in the bladder; it is a product of its own."""
    assert not [d for d in plan.drugs if {"tar210", "jnj42756493"} <= d.names.keys()]
    assert len(_drugs_with(plan, "JNJ", "TAR-210")) == 1
    assert len(_drugs_with(plan, "JNJ", "JNJ-42756493")) == 1


def test_spd476_keeps_only_its_code_without_an_fda_name_for_mesalazine(plan: DrugPlan) -> None:
    """SPD476 is mesalazine, but the trials give "mesalazine" unmarked and Drugs@FDA writes "mesalamine"."""
    (spd476,) = _drugs_with(plan, "TAK", "SPD476")
    assert "mesalazine" not in spd476.names


def test_plain_word_names_still_join_each_other() -> None:
    """Constructed: "izalontamab brengitecan" lists "IZA-BREN", another trial's main name: one drug.

    Written in capitals since the fifth review: a mixed-case "Iza-bren" no longer looks like a
    drug's name, so its trial names nothing.
    """
    plan = plan_drugs(
        [
            _trial("N1", ListedName("IZA-BREN", DrugNameKind.BRAND)),
            _trial(
                "N2",
                ListedName("izalontamab brengitecan", DrugNameKind.INN),
                ListedName("IZA-BREN", DrugNameKind.BRAND, main=False),
            ),
        ],
        SPONSORS,
    )
    assert _names(plan) == [["izabren", "izalontamabbrengitecan"]]


def test_a_word_main_name_that_is_the_company_s_fda_name_joins_its_code() -> None:
    plan = plan_drugs(
        [
            _approved("NDA000001", "ACMETINIB"),
            _trial("N1", ListedName("Acmetinib", DrugNameKind.INN), _code("ACM-101", main=False)),
        ],
        SPONSORS,
    )
    assert _names(plan) == [["acm101", "acmetinib"]]


# Optional items


@pytest.mark.parametrize(
    "text",
    [
        "Vaccination",
        "Various",
        "Switch",
        "Opioid",
        "Premedication",
        "Schedule D",
        "Relative Bioavailability",
        "Absolute Bioavailability",
        "Relative/Absolute Bioavailability",
    ],
)
def test_words_of_a_procedure_or_class_are_no_drug(text: str) -> None:
    assert not registry_names(text)


def test_a_partner_s_unapproved_drug_is_not_let_in_by_the_sponsor_s_other_trials() -> None:
    """Constructed: Acme gives Roche's "zebramab (RO7654321)"; another Acme trial is of "zebramab" alone."""
    plan = plan_drugs(
        [
            _trial("N1", ListedName("zebramab", DrugNameKind.INN), _code("RO7654321", main=False)),
            _trial("N2", ListedName("zebramab", DrugNameKind.INN)),
        ],
        SPONSORS,
    )
    assert not any("ro7654321" in d.names for d in plan.drugs)


def test_a_licensed_in_drug_the_company_has_approved_keeps_its_partner_code() -> None:
    plan = plan_drugs(
        [
            _approved("NDA000001", "ACMETINIB"),
            _trial("N1", ListedName("acmetinib", DrugNameKind.INN), _code("AZD7986", main=False)),
        ],
        SPONSORS,
    )
    assert _names(plan) == [["acmetinib", "azd7986"]]
