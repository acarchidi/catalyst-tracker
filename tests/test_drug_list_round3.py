"""Cases the third Opus review of PR #73 found in the drug list (ADR 0035, rule 3, marked "third review").

Plain code over recorded copies of real public records (`tests/fixtures/drug-list-round3/`,
recorded 2026-10-20):
- Drugs@FDA: LIBTAYO (Regeneron), OPDUALAG (Bristol Myers Squibb), EPKINLY (Genmab), FOLLISTIM
  (Organon), and GONAL-F (EMD Serono);
- ClinicalTrials.gov: Inovio's INO-3112 trial, which lists its parts VGX-3100 and INO-9012
  (NCT02172911), with trials of VGX-3100, INO-9012, and INO-5401 (NCT00685412, NCT02960594,
  NCT03491683); Bristol Myers Squibb's "panHer" (BMS-599626) and BMS-690514 trials that list
  "panHER" (NCT00095537, NCT00207012, NCT00329004, NCT00516451) and relatlimab with "Anti-LAG 3"
  (NCT01968109, NCT02488759); Regeneron's "cemiplimab (maintenance)" (NCT03690869) and a trial
  of Roche's cevostamab (RO7187797) beside linvoseltamab (NCT05137054); Lexicon's "LX9211
  (blinded)" (NCT06203002, NCT04455633); Genmab's "Epcoritamab (maintenance)" (NCT04542824);
  Alaunos's INXN-1001 with "Activator Ligand" (NCT01397708); and Organon's recFSH trials, one
  written "recFSH (follitropin alfa)" and one "recFSH (follitropin beta)" (NCT00702546, NCT00702624).
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
    plan_drugs,
    registry_names,
    registry_other_names,
    registry_records,
)
from catalyst.resolve import DrugRef, name_key
from catalyst.schemas import DrugNameKind, DrugNameSource
from catalyst.verify.drug import Outcome, drug_named, drug_names, other_drugs
from conftest import FIXTURES

FOLDER = FIXTURES / "drug-list-round3"
REGISTERED = {
    "Inovio Pharmaceuticals": "INO",
    "Bristol-Myers Squibb": "BMY",
    "Regeneron Pharmaceuticals": "REGN",
    "Lexicon Pharmaceuticals": "LXRX",
    "Genmab": "GMAB",
    "Alaunos Therapeutics": "TCRT",
    "Organon and Co": "OGN",
    "Hoffmann-La Roche": "RHHBY",
}
FDA_NAMES = {
    "REGENERON PHARMACEUTICALS": "REGN",
    "BRISTOL MYERS SQUIBB": "BMY",
    "GENMAB US, INC.": "GMAB",
    "ORGANON USA INC": "OGN",
}
COMPANY = {ticker: uuid4() for ticker in {*REGISTERED.values(), *FDA_NAMES.values()}}


def _read(name: str) -> dict[str, Any]:
    return json.loads((FOLDER / name).read_text())


def _records() -> list[SourceRecord]:
    records = [r for a in _read("00_api.fda.gov_drug_drugsfda.json")["results"] for r in fda_records(a)]
    studies = _read("01_clinicaltrials.gov_api_v2_studies")["studies"]
    return records + [r for s in studies for r in registry_records(s)]


def _sponsors() -> dict[DrugNameSource, dict[str, UUID]]:
    return {
        DrugNameSource.OPENFDA: {fda_sponsor_key(n): COMPANY[t] for n, t in FDA_NAMES.items()},
        DrugNameSource.CTGOV: {n: COMPANY[t] for n, t in REGISTERED.items()},
    }


@pytest.fixture(scope="module")
def plan() -> DrugPlan:
    return plan_drugs(_records(), _sponsors())


def _drug(plan: DrugPlan, ticker: str, name: str) -> Any:
    found = [d for d in plan.drugs if d.company_id == COMPANY[ticker] and name_key(name) in d.names]
    assert len(found) == 1, (ticker, name, found)
    return found[0]


def _keys(plan: DrugPlan, ticker: str) -> set[str]:
    return {key for d in plan.drugs if d.company_id == COMPANY[ticker] for key in d.names}


def _must_fail(plan: DrugPlan, ticker: str, key: str, claim_drug: str, quote: str) -> None:
    refs = [
        DrugRef(uuid4(), d.company_id, tuple((n.name, n.kind) for n in d.names.values())) for d in plan.drugs
    ]
    drug = _drug(plan, ticker, key)
    record = next(
        r
        for r in refs
        if r.company_id == drug.company_id and {name_key(n) for n, _ in r.names} == set(drug.names)
    )
    names = drug_names(claim_drug, [record])
    result = drug_named(quote, quote, 0, len(quote), names, other_drugs(refs, {drug.company_id}, names))
    assert result.outcome is Outcome.FAIL, (claim_drug, names)


def _registry(ref: str, sponsor: str, *names: ListedName) -> SourceRecord:
    return SourceRecord(DrugNameSource.CTGOV, ref, sponsor, names)


def _code(name: str, *, main: bool = True) -> ListedName:
    return ListedName(name, DrugNameKind.CODE, main)


# Blocking 1: an alias never joins two different drugs


def test_a_combination_trial_listing_its_parts_joins_none_of_them(plan: DrugPlan) -> None:
    """NCT02172911's INO-3112 lists VGX-3100 and INO-9012: three drugs stay three."""
    combination = _drug(plan, "INO", "INO-3112")
    vgx, ino = _drug(plan, "INO", "VGX-3100"), _drug(plan, "INO", "INO-9012")
    assert len({id(combination), id(vgx), id(ino)}) == 3
    assert "ino9012" not in vgx.names
    assert "vgx3100" not in ino.names


def test_a_vgx_3100_claim_on_a_quote_about_ino_9012_is_held(plan: DrugPlan) -> None:
    _must_fail(
        plan,
        "INO",
        "VGX-3100",
        "VGX-3100",
        "Data for INO-5401 with INO-9012 in glioblastoma are expected in 2027.",
    )


def test_two_codes_no_record_gives_together_are_not_joined_by_a_shared_word(plan: DrugPlan) -> None:
    """BMS-599626 is "panHer"; BMS-690514 trials list "panHER" too. They stay two drugs."""
    first, second = _drug(plan, "BMY", "BMS-599626"), _drug(plan, "BMY", "BMS-690514")
    assert first is not second
    assert "panher" not in second.names
    assert second.preferred_name == "BMS-690514"


def test_a_bms_690514_claim_on_a_quote_about_bms_599626_is_held(plan: DrugPlan) -> None:
    _must_fail(plan, "BMY", "BMS-690514", "BMS-690514", "BMS-599626 data are expected in 2027.")


def test_one_record_whose_other_names_are_two_drugs_joins_neither() -> None:
    """Constructed: "XYZ-3000" lists "XYZ-1000" and "XYZ-2000", each a trial's main name."""
    sponsor = "Inovio Pharmaceuticals"
    records = [
        _registry(
            "NCT00000001",
            sponsor,
            _code("XYZ-3000"),
            _code("XYZ-1000", main=False),
            _code("XYZ-2000", main=False),
        ),
        _registry("NCT00000002", sponsor, _code("XYZ-1000")),
        _registry("NCT00000003", sponsor, _code("XYZ-2000")),
    ]
    plan = plan_drugs(records, _sponsors())
    assert sorted(sorted(d.names) for d in plan.drugs) == [["xyz1000"], ["xyz2000"], ["xyz3000"]]


def _approved(generic: str) -> SourceRecord:
    """Return a constructed Drugs@FDA product of Regeneron's.

    Since the fourth review, a registry word main name joins codes only when it is such a name.
    """
    names = (ListedName(generic.upper(), DrugNameKind.INN),)
    return SourceRecord(
        DrugNameSource.OPENFDA, "NDA000001", "REGENERON PHARMACEUTICALS", names, ingredient=name_key(generic)
    )


def test_an_alias_never_joins_two_codes_no_record_gives_together() -> None:
    """Constructed: "Zebra" is XYZ-1000 in one trial and is listed under XYZ-2000 in another."""
    sponsor = "Regeneron Pharmaceuticals"
    records = [
        _approved("Zebrafin"),
        _registry(
            "NCT00000001", sponsor, ListedName("Zebrafin", DrugNameKind.INN), _code("XYZ-1000", main=False)
        ),
        _registry(
            "NCT00000002", sponsor, _code("XYZ-2000"), ListedName("Zebrafin", DrugNameKind.INN, main=False)
        ),
    ]
    plan = plan_drugs(records, _sponsors())
    assert sorted(sorted(d.names) for d in plan.drugs) == [["xyz1000", "zebrafin"], ["xyz2000"]]


def test_an_alias_with_a_code_both_records_give_still_joins() -> None:
    """Constructed: brensocatib lists INS1007, and a trial of INS1007 lists brensocatib: one drug."""
    sponsor = "Regeneron Pharmaceuticals"
    records = [
        _approved("brensocatib"),
        _registry(
            "NCT00000001", sponsor, ListedName("brensocatib", DrugNameKind.INN), _code("INS1007", main=False)
        ),
        _registry(
            "NCT00000002", sponsor, _code("INS1007"), ListedName("brensocatib", DrugNameKind.INN, main=False)
        ),
    ]
    plan = plan_drugs(records, _sponsors())
    assert [sorted(d.names) for d in plan.drugs] == [["brensocatib", "ins1007"]]


# Blocking 2: an other name with no digit needs a mark or a source that gives it as a main name


@pytest.mark.parametrize(
    ("ticker", "key", "word"),
    [
        ("REGN", "cemiplimab", "maintenance"),
        ("LXRX", "LX9211", "blinded"),
        ("BMY", "relatlimab", "antilag"),
        ("GMAB", "epcoritamab", "maintenance"),
        ("TCRT", "INXN-1001", "activatorligand"),
    ],
)
def test_a_word_in_brackets_or_an_unmarked_other_name_is_no_drug_name(
    plan: DrugPlan, ticker: str, key: str, word: str
) -> None:
    assert word not in _drug(plan, ticker, key).names
    assert word not in _keys(plan, ticker)


def test_a_marked_other_name_or_one_fda_gives_stays(plan: DrugPlan) -> None:
    """EPKINLY™ is marked and FDA's; Libtayo is FDA's brand of cemiplimab."""
    assert "epkinly" in _drug(plan, "GMAB", "epcoritamab").names
    assert "libtayo" in _drug(plan, "REGN", "cemiplimab").names


def test_a_cemiplimab_claim_on_a_quote_about_a_maintenance_study_of_another_drug_is_held(
    plan: DrugPlan,
) -> None:
    _must_fail(
        plan,
        "REGN",
        "cemiplimab",
        "cemiplimab",
        "The maintenance study of fianlimab is expected to read out in 2027.",
    )


def test_an_lx9211_claim_on_a_quote_about_a_blinded_study_of_another_drug_is_held(plan: DrugPlan) -> None:
    _must_fail(
        plan,
        "LXRX",
        "LX9211",
        "LX9211",
        "A double-blinded study of sotagliflozin is expected to report in 2027.",
    )


def test_a_relatlimab_claim_on_a_quote_about_another_anti_lag_3_program_is_held(plan: DrugPlan) -> None:
    _must_fail(
        plan, "BMY", "relatlimab", "relatlimab", "An anti-LAG-3 program is expected to report in 2027."
    )


def test_an_unmarked_other_name_counts_when_drugs_at_fda_gives_it() -> None:
    """Since the fourth review, another trial's word main name no longer joins it to a code."""
    sponsor = "Regeneron Pharmaceuticals"
    records = [
        _registry(
            "NCT00000001", sponsor, _code("XYZ-1000"), ListedName("zebrafin", DrugNameKind.INN, main=False)
        ),
        _approved("zebrafin"),
        _registry(
            "NCT00000003", sponsor, _code("XYZ-2000"), ListedName("Quokkamab", DrugNameKind.INN, main=False)
        ),
        _registry(
            "NCT00000004",
            sponsor,
            _code("XYZ-3000"),
            ListedName("WOMBAT", DrugNameKind.BRAND, main=False, marked=True),
        ),
    ]
    plan = plan_drugs(records, _sponsors())
    # "Quokkamab" is unmarked and no record's main name; "WOMBAT" is marked.
    assert sorted(sorted(d.names) for d in plan.drugs) == [
        ["wombat", "xyz3000"],
        ["xyz1000", "zebrafin"],
        ["xyz2000"],
    ]


def test_a_registry_mark_is_recorded_on_the_name() -> None:
    assert [(n.name, n.marked) for n in registry_other_names("EPKINLY™")] == [("EPKINLY", True)]
    assert [(n.name, n.marked) for n in registry_other_names("Libtayo")] == [("Libtayo", False)]
    assert [(n.name, n.main) for n in registry_names("cemiplimab (maintenance)") or []] == [
        ("cemiplimab", True),
        ("maintenance", False),
    ]


# Optional items


def test_a_partner_s_unapproved_drug_in_the_sponsor_s_trial_is_not_the_sponsor_s(plan: DrugPlan) -> None:
    """NCT05137054 gives Roche's cevostamab (RO7187797) and PF-03084014 beside Regeneron's own drugs."""
    keys = _keys(plan, "REGN")
    assert not {"cevostamab", "ro7187797", "bfcr4350a", "nirogacestat", "pf03084014"} & keys
    # Their codes REGN5458 and REGN3767 are other names of word main names here (fourth review).
    assert {"linvoseltamab", "fianlimab"} <= keys


def test_a_code_with_a_big_sponsor_s_prefix_stays_that_sponsor_s() -> None:
    records = [
        _registry(
            "NCT00000001",
            "Hoffmann-La Roche",
            _code("RO7187797"),
        )
    ]
    plan = plan_drugs(records, _sponsors())
    assert [(d.company_id, sorted(d.names)) for d in plan.drugs] == [(COMPANY["RHHBY"], ["ro7187797"])]


def test_organon_s_merck_codes_stay_organon_s() -> None:
    """Organon came out of Merck: corifollitropin alfa's code MK-8962 is Organon's own.

    Constructed with the code as the main name: since the fourth review, the registry's word main
    name "Corifollitropin alfa" stands alone, without its codes.
    """
    plan = plan_drugs([_registry("NCT00000001", "Organon and Co", _code("MK-8962"))], _sponsors())
    assert [(d.company_id, sorted(d.names)) for d in plan.drugs] == [(COMPANY["OGN"], ["mk8962"])]


def test_follitropin_alfa_and_beta_are_never_one_drug(plan: DrugPlan) -> None:
    for drug in plan.drugs:
        assert not {"follitropinalfa", "follitropinbeta"} <= drug.names.keys()


@pytest.mark.parametrize(
    "text",
    [
        "FGF21",
        "MUC16",
        "MUC16xCD3",
        "OX40",
        "aOX40",
        "GPR75",
        "HSD17B13",
        "MK-3475-001",
        "AC-225",
        "Ac-225",
        "non-PS80",
    ],
)
def test_a_gene_target_isotope_or_protocol_number_is_no_code_name(text: str) -> None:
    assert registry_other_names(text) == []
    assert not registry_names(text)


@pytest.mark.parametrize(
    "text",
    [
        "TIGIT",
        "STING",
        "MENIN",
        "siRNA",
        "GalNAc",
        "Factor IX",
        "prostacyclin",
        "Direct oral anticoagulant",
        "Regular TCA use",
        "Thyroid hormone",
        "acetominophen",
    ],
)
def test_a_target_class_or_misspelled_word_is_no_drug_name(text: str) -> None:
    assert registry_other_names(text) == []
    assert not registry_names(text)
