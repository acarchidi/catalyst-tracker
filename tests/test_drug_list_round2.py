"""Cases the second Opus review of PR #73 found in the drug list (ADR 0035, "After the second review").

Plain code over recorded copies of real public records (`tests/fixtures/drug-list-round2/`,
recorded 2026-10-19):
- Drugs@FDA: YERVOY, OPDIVO (Bristol Myers Squibb), KEYTRUDA (Merck), GAZYVA (Genentech),
  CAMPTOSAR (Pfizer), TARCEVA (OSI), XOSPATA (Astellas), TURALIO (Daiichi Sankyo), ORSERDU
  (Stemline), and LYNOZYFIC, whose sponsor FDA spells "RENGENERON";
- ClinicalTrials.gov: Aktis's two radioligand trials (NCT07020117, NCT07581184), whose imaging
  agents both list "[64Cu]"; Regeneron's linvoseltamab, vonsetamig, and "Noninterventional"
  records (NCT05164250, NCT05092347, NCT05106387), which list "BCMAxCD3"; Bristol Myers Squibb's
  "MDX-010 (CTLA-4)" (NCT00050102); Kazia's paxalisib with "(PI3K)" (NCT05012670); Annexon's
  ANX009 with "(IgG1)" (NCT05780515); and trials in which a sponsor gives another company's
  approved drug (nivolumab, erlotinib, pexidartinib, gilteritinib, elacestrant, irinotecan,
  pembrolizumab, obinutuzumab) under its codes.

The owner decided on 2026-10-19 to keep a registry's other names as names, filtered further.
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
    merge_with_existing,
    plan_drugs,
    registry_names,
    registry_other_names,
    registry_records,
)
from catalyst.resolve import CompanyRef, DrugRef, ReferenceData, Status, name_key
from catalyst.resolve_claims import known_drug
from catalyst.schemas import DrugNameKind, DrugNameSource
from catalyst.verify.drug import Outcome, drug_named, drug_names, other_drugs
from conftest import FIXTURES

FOLDER = FIXTURES / "drug-list-round2"
REGISTERED = {
    "Aktis Oncology, Inc.": "AKTS",
    "Regeneron Pharmaceuticals": "REGN",
    "Bristol-Myers Squibb": "BMY",
    "Kazia Therapeutics Limited": "KZIA",
    "Annexon, Inc.": "ANNX",
    "Candel Therapeutics, Inc.": "CADL",
    "Nektar Therapeutics": "NKTR",
    "Genprex, Inc.": "GNPX",
    "Cogent Biosciences, Inc.": "COGT",
    "AbbVie": "ABBV",
    "Context Therapeutics Inc.": "CNTX",
    "Actuate Therapeutics Inc.": "ACTU",
    "PMV Pharmaceuticals, Inc": "PMVP",
    "Incyte Corporation": "INCY",
}
FDA_NAMES = {
    "BRISTOL MYERS SQUIBB CO": "BMY",
    "REGENERON PHARMACEUTICALS, INC.": "REGN",
    "Merck & Co., Inc.": "MRK",
    "PFIZER INC": "PFE",
}
COMPANY = {ticker: uuid4() for ticker in {*REGISTERED.values(), *FDA_NAMES.values()}}
TICKER = {company: ticker for ticker, company in COMPANY.items()}


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


# B1': an other name never joins two records whose main names differ


def test_an_isotope_two_radioligands_list_joins_neither(plan: DrugPlan) -> None:
    """Both Aktis imaging agents list "[64Cu]"; Cu-AKY-1189 and Cu-AKY-2519 stay two drugs."""
    first, second = _drug(plan, "AKTS", "Cu-AKY-1189"), _drug(plan, "AKTS", "Cu-AKY-2519")
    assert first is not second
    assert "64cu" not in _keys(plan, "AKTS")
    assert second.preferred_name == "Cu-AKY-2519"


def test_a_target_two_bispecifics_list_joins_neither(plan: DrugPlan) -> None:
    """Linvoseltamab (REGN5458) and vonsetamig (REGN5459) both list "BCMAxCD3".

    Since the fourth review, a registry word main name (vonsetamig) stands alone, without its codes.
    """
    linvoseltamab, vonsetamig = _drug(plan, "REGN", "REGN5458"), _drug(plan, "REGN", "vonsetamig")
    assert linvoseltamab is not vonsetamig
    assert "bcmaxcd3" not in _keys(plan, "REGN")
    assert vonsetamig.preferred_name == "Vonsetamig"
    assert "noninterventional" not in _keys(plan, "REGN")


def test_an_other_name_shared_by_two_records_with_different_main_names_joins_neither() -> None:
    """Constructed: two codes' records list one other code; it is dropped from both."""
    shared = ListedName("XYZ-1000", DrugNameKind.CODE, main=False)
    records = [
        SourceRecord(DrugNameSource.CTGOV, f"NCT0000000{i}", "Regeneron Pharmaceuticals", (main, shared))
        for i, main in enumerate(
            [ListedName("ABC-1234", DrugNameKind.CODE), ListedName("ABC-5678", DrugNameKind.CODE)]
        )
    ]
    plan = plan_drugs(records, _sponsors())
    assert {frozenset(d.names) for d in plan.drugs} == {frozenset({"abc1234"}), frozenset({"abc5678"})}


def test_an_other_name_that_is_another_record_s_main_name_still_joins_them() -> None:
    """Constructed: one record lists INS1007 under brensocatib, another is named INS1007: one drug.

    Brensocatib is the company's Drugs@FDA name here: since the fourth review, a registry word main
    name that is not one stands alone, without codes.
    """
    records = [
        SourceRecord(
            DrugNameSource.OPENFDA,
            "NDA000001",
            "REGENERON PHARMACEUTICALS, INC.",
            (ListedName("BRENSOCATIB", DrugNameKind.INN),),
            ingredient="brensocatib",
        ),
        SourceRecord(
            DrugNameSource.CTGOV,
            "NCT00000001",
            "Regeneron Pharmaceuticals",
            (
                ListedName("brensocatib", DrugNameKind.INN),
                ListedName("INS1007", DrugNameKind.CODE, main=False),
            ),
        ),
        SourceRecord(
            DrugNameSource.CTGOV,
            "NCT00000002",
            "Regeneron Pharmaceuticals",
            (ListedName("INS1007", DrugNameKind.CODE),),
        ),
    ]
    plan = plan_drugs(records, _sponsors())
    assert [frozenset(d.names) for d in plan.drugs] == [frozenset({"brensocatib", "ins1007"})]


@pytest.mark.parametrize(
    "text",
    [
        "Noninterventional",
        "Observational",
        "Genotyping",
        "Antibiotics",
        "Diuretics",
        "lumbar puncture",
        "Follicular Lymphoma",
        "Immuno-oncology",
        "Gemini",
    ],
)
def test_words_that_name_no_drug_are_no_main_name(text: str) -> None:
    assert not registry_names(text)


def test_lynozyfic_belongs_to_regeneron_though_fda_misspells_it(plan: DrugPlan) -> None:
    """FDA files LYNOZYFIC under "RENGENERON PHARMACEUTICALS, INC." (checked by hand 2026-10-19)."""
    assert {"lynozyfic", "linvoseltamab"} <= _drug(plan, "REGN", "LYNOZYFIC").names.keys()


# B2': target, class, isotope, and other non-drug "codes"


@pytest.mark.parametrize(
    "text",
    [
        "CTLA-4",
        "PI3K",
        "IgG1",
        "IgG4",
        "B7-H4",
        "LPA1",
        "ILT4",
        "5-HT3 RA",
        "131I",
        "68Ga",
        "89Zr",
        "64Cu",
        "Lu-177",
        "B.1.1.7",
        "U100",
        "U-100",
        "PS80",
        "QS-21",
        "AS01B",
        "MF59",
        "Type 2 Diabetes Mellitus",
        "Vitamin D3",
        "BCMAxCD3",
        "CD19",
        "HER2",
        "IL-17A",
        "PD-L1",
        "G12C",
        "V600E",
        "KEYNOTE-D79",
        "KEYNOTE-189",
        "CheckMate-067",
        "HPV16",
        "Day 28",
    ],
)
def test_a_target_class_isotope_or_trial_is_no_code_name(text: str) -> None:
    assert registry_other_names(text) == []
    assert not registry_names(text)


@pytest.mark.parametrize(
    "text",
    [
        "ABC-1234",
        "REGN5458",
        "GS-5423",
        "INS1007",
        "KO-539",
        "X4P-001",
        "GK1-399",
        "LY3471851",
        "BMS-986218",
        "SCH 58235",
        "Cu-AKY-1189",
        "ENTR-601-45",
        "R115777",
    ],
)
def test_a_drug_code_is_kept_from_other_names(text: str) -> None:
    assert [(n.name, n.kind) for n in registry_other_names(text)] == [(text, DrugNameKind.CODE)]


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


def test_a_yervoy_claim_on_a_quote_about_another_ctla_4_antibody_is_held(plan: DrugPlan) -> None:
    _must_fail(
        plan,
        "BMY",
        "YERVOY",
        "YERVOY",
        "Data from the anti-CTLA-4 antibody BMS-986218 in melanoma are expected in 2027.",
    )


def test_a_paxalisib_claim_on_a_quote_about_another_pi3k_program_is_held(plan: DrugPlan) -> None:
    _must_fail(
        plan,
        "KZIA",
        "paxalisib",
        "paxalisib",
        "A second PI3K/mTOR program is expected to enter the clinic in 2027.",
    )


def test_an_anx009_claim_on_a_quote_about_another_igg1_antibody_is_held(plan: DrugPlan) -> None:
    _must_fail(
        plan,
        "ANNX",
        "ANX009",
        "ANX009",
        "Tanruprubart (ANX005), a full-length IgG1 antibody, BLA submission is expected in 2027.",
    )


# B3: another company's approved drug is never filed as the sponsor's own


@pytest.mark.parametrize(
    ("ticker", "name"),
    [
        ("CADL", "ONO-4538"),
        ("NKTR", "BMS-936658"),
        ("GNPX", "OSI-774"),
        ("COGT", "PLX3397"),
        ("ABBV", "ASP-2215"),
        ("CNTX", "RAD1901"),
        ("ACTU", "CPT-11"),
        ("PMVP", "KEYNOTE-D79"),
        ("PMVP", "MK-3475"),
        ("INCY", "Gazyvaro"),
    ],
)
def test_a_trial_s_name_for_another_company_s_approved_drug_is_not_the_sponsor_s(
    plan: DrugPlan, ticker: str, name: str
) -> None:
    assert name_key(name) not in _keys(plan, ticker)
    references = ReferenceData(
        [CompanyRef(c, 0, (), ()) for c in COMPANY.values()],
        [
            DrugRef(uuid4(), d.company_id, tuple((n.name, n.kind) for n in d.names.values()))
            for d in plan.drugs
        ],
        [],
    )
    assert known_drug(references, name, COMPANY[ticker]).status is not Status.RESOLVED


# Optional items


@pytest.mark.parametrize(
    "ingredient",
    ["FERRIC CITRATE", "CALCIUM ACETATE", "POTASSIUM CHLORIDE", "MAGNESIUM SULFATE", "LITHIUM CARBONATE"],
)
def test_a_salt_of_a_metal_keeps_its_whole_name(ingredient: str) -> None:
    assert generic_name(ingredient) == ingredient


def test_the_main_name_most_records_give_is_shown() -> None:
    """Constructed: one trial is named "Iza-bren"; three name "izalontamab brengitecan", listing it."""
    short = SourceRecord(
        DrugNameSource.CTGOV,
        "NCT00000001",
        "Bristol-Myers Squibb",
        (ListedName("Iza-bren", DrugNameKind.INN),),
    )
    full = [
        SourceRecord(
            DrugNameSource.CTGOV,
            f"NCT0000001{i}",
            "Bristol-Myers Squibb",
            (
                ListedName("izalontamab brengitecan", DrugNameKind.INN),
                ListedName("Iza-bren", DrugNameKind.INN, main=False),
            ),
        )
        for i in range(3)
    ]
    plan = plan_drugs([short, *full], _sponsors())
    assert [d.preferred_name for d in plan.drugs] == ["izalontamab brengitecan"]


def test_a_code_name_another_company_s_drug_has_on_file_is_withheld() -> None:
    plan = plan_drugs(
        [
            SourceRecord(
                DrugNameSource.CTGOV,
                "NCT00000001",
                "Annexon, Inc.",
                (ListedName("ANX-1502", DrugNameKind.CODE),),
            )
        ],
        _sponsors(),
    )
    theirs = DrugRef(uuid4(), COMPANY["REGN"], (("ANX1502", DrugNameKind.CODE),))
    merged = merge_with_existing(plan, [theirs])
    assert merged.writes == []
    assert merged.withheld["anx1502"].company_ids == {COMPANY["ANNX"], COMPANY["REGN"]}


# The owner's decision: other names count again, filtered


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Brinsupri", [("Brinsupri", DrugNameKind.INN)]),
        ("cema-cel", [("cema-cel", DrugNameKind.INN)]),
        ("ARIKAYCE®", [("ARIKAYCE", DrugNameKind.BRAND)]),
        ("Commercial tablet", []),
        ("Recombinant humanized immunoglobulin G1 (IgG1) Fab", []),
        ("Phosphoinositide 3-kinase (PI3K) Inhibitor", []),
    ],
)
def test_other_names_count_again_once_filtered(text: str, expected: list[tuple[str, DrugNameKind]]) -> None:
    found = registry_other_names(text)
    assert [(n.name, n.kind) for n in found] == expected
    assert not any(n.main for n in found)


def test_a_shared_main_name_takes_its_other_names_with_it() -> None:
    """Constructed: two companies' trials both name cemdisiran; one lists a code too."""
    records = [
        SourceRecord(
            DrugNameSource.CTGOV,
            "NCT00000001",
            "Annexon, Inc.",
            (
                ListedName("cemdisiran", DrugNameKind.INN),
                ListedName("ALN-CC5", DrugNameKind.CODE, main=False),
            ),
        ),
        SourceRecord(
            DrugNameSource.CTGOV,
            "NCT00000002",
            "Regeneron Pharmaceuticals",
            (ListedName("cemdisiran", DrugNameKind.INN),),
        ),
    ]
    plan = plan_drugs(records, _sponsors())
    assert plan.drugs == []
    assert set(plan.withheld) == {"cemdisiran"}


@pytest.mark.parametrize(
    "text",
    [
        "Optimal",
        "Approach",
        "China",
        "Biological",
        "flexible",
        "freebase",
        "fumarate",
        "Optional",
        "radiotracer",
        "Radioligand",
        "long-acting insulins",
        "Ceralasertib taken orally",
        "Cervical Spinal Fusion",
        "Tumor-Infiltrating Lymphocytes",
        "RNA antisense oligonucleotide",
        "Bovine colostrum",
        "Wyeth",
        "mCRPC",
        "Non-interventional",
    ],
)
def test_free_text_that_names_no_drug_is_no_other_name(text: str) -> None:
    assert registry_other_names(text) == []


def test_a_molecule_another_sponsor_sells_takes_only_itself_out_of_a_record() -> None:
    """Constructed from Avalyn's NCT06329401: "AP01" lists "Pirfenidone", which FDA lists for another sponsor.

    AP01 is Avalyn's own inhaled pirfenidone. Only another sponsor's brand ("Gazyva" beside
    "Gazyvaro") or main name makes the whole intervention another company's.
    """
    fda = [r for a in _read("00_api.fda.gov_drug_drugsfda.json")["results"] for r in fda_records(a)]
    esbriet = SourceRecord(
        DrugNameSource.OPENFDA,
        "NDA022535",
        "GENENTECH",
        (ListedName("PIRFENIDONE", DrugNameKind.INN), ListedName("ESBRIET", DrugNameKind.BRAND)),
        ingredient="pirfenidone",
    )
    ap01 = SourceRecord(
        DrugNameSource.CTGOV,
        "NCT06329401",
        "Annexon, Inc.",
        (ListedName("AP01", DrugNameKind.CODE), ListedName("Pirfenidone", DrugNameKind.INN, main=False)),
    )
    plan = plan_drugs([*fda, esbriet, ap01], _sponsors())
    assert [sorted(d.names) for d in plan.drugs if d.company_id == COMPANY["ANNX"]] == [["ap01"]]


def test_an_fda_brand_stays_when_its_generic_name_is_shared(plan: DrugPlan) -> None:
    """Constructed like Outlook's LYTENAVA (bevacizumab-vikg) beside Genentech's AVASTIN; a covered stand-in.

    Bevacizumab is shared, so it is withheld; the brand LYTENAVA is the sponsor's alone and stays.
    """
    copy = SourceRecord(
        DrugNameSource.OPENFDA,
        "BLA761320",
        "REGENERON PHARMACEUTICALS, INC.",
        (ListedName("BEVACIZUMAB", DrugNameKind.INN), ListedName("LYTENAVA", DrugNameKind.BRAND)),
        ingredient="bevacizumab",
    )
    other = SourceRecord(
        DrugNameSource.OPENFDA,
        "BLA125085",
        "GENENTECH",
        (ListedName("BEVACIZUMAB", DrugNameKind.INN), ListedName("AVASTIN", DrugNameKind.BRAND)),
        ingredient="bevacizumab",
    )
    plan = plan_drugs([copy, other], _sponsors())
    assert [sorted(d.names) for d in plan.drugs if d.company_id == COMPANY["REGN"]] == [["lytenava"]]
    assert "bevacizumab" in plan.withheld


def test_every_shown_name_is_a_main_name(plan: DrugPlan) -> None:
    assert all(d.names[name_key(d.preferred_name)].main for d in plan.drugs)
