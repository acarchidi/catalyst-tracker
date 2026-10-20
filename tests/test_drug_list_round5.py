"""Cases the fifth Opus review of PR #73 found in the drug list (ADR 0035, rule 3, "fifth review").

The owner decided on 2026-10-20 to keep filtering. Plain code over recorded copies of real public
records (`tests/fixtures/drug-list-round5/`, recorded 2026-10-20): Janssen's and Moderna's trials
that give GSK's Shingrix (NCT03953196, NCT05701800); Novavax's that give Sanofi's Fluzone and
Flublok (NCT03658629, NCT04120194); Moderna's that gives Seqirus's "Afluria® Quadrivalent"
(NCT05330975); Incyte's that gives Kite's Yescarta (NCT04071366); Bristol Myers Squibb's "Oral
Taxane" (NCT00103831); and Pfizer's "Clostridium difficile" vaccine trials (NCT02117570,
NCT03918629). Plus the round-4 records of Lilly's "FGF Receptor" (NCT01212107).
"""

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.drug_list import (
    DrugPlan,
    ListedName,
    SourceRecord,
    looks_like_a_drug,
    plan_drugs,
    registry_records,
)
from catalyst.drug_list_cber import CBER_PRODUCTS
from catalyst.references import drug_refs
from catalyst.resolve import CompanyRef, DrugRef, ReferenceData, Status, name_key
from catalyst.resolve_claims import known_drug
from catalyst.schemas import Drug, DrugName, DrugNameKind, DrugNameSource
from catalyst.verify.drug import Outcome, drug_named, drug_names, other_drugs
from conftest import FIXTURES

REGISTERED = {
    "Janssen Research & Development, LLC": "JNJ",
    "ModernaTX, Inc.": "MRNA",
    "Novavax": "NVAX",
    "Incyte Corporation": "INCY",
    "Bristol-Myers Squibb": "BMY",
    "Pfizer": "PFE",
    "Eli Lilly and Company": "LLY",
    "Acme Bio": "ACME",
}
COMPANY = {ticker: uuid4() for ticker in set(REGISTERED.values())}
SPONSORS: dict[DrugNameSource, dict[str, UUID]] = {
    DrugNameSource.CTGOV: {n: COMPANY[t] for n, t in REGISTERED.items()},
    DrugNameSource.OPENFDA: {},
}


def _studies(folder: str) -> list[dict[str, Any]]:
    path = FIXTURES / folder / "01_clinicaltrials.gov_api_v2_studies"
    studies: list[dict[str, Any]] = json.loads(path.read_text())["studies"]
    return studies


@pytest.fixture(scope="module")
def plan() -> DrugPlan:
    studies = _studies("drug-list-round5") + _studies("drug-list-round4")
    return plan_drugs([r for s in studies for r in registry_records(s)], SPONSORS)


def _references(plan: DrugPlan) -> tuple[ReferenceData, list[DrugRef]]:
    refs = [
        DrugRef(uuid4(), d.company_id, tuple((n.name, n.kind) for n in d.names.values()), d.unconfirmed)
        for d in plan.drugs
    ]
    return ReferenceData([CompanyRef(c, 0, (), ()) for c in COMPANY.values()], refs, []), refs


def _held(plan: DrugPlan, ticker: str, claim_drug: str, quote: str) -> None:
    """Assert the claim waits for review: unknown, ambiguous, marked for review, or not in the quote."""
    references, refs = _references(plan)
    found = known_drug(references, claim_drug, COMPANY[ticker])
    if found.status is not Status.RESOLVED or found.needs_review:
        return
    record = next(r for r in refs if r.id == found.id)
    names = drug_names(claim_drug, [record])
    result = drug_named(quote, quote, 0, len(quote), names, other_drugs(refs, {COMPANY[ticker]}, names))
    assert result.outcome is Outcome.FAIL, (claim_drug, names)


# Blocking: another company's product, or a class word, filed as the sponsor's own drug


@pytest.mark.parametrize(
    ("ticker", "claim_drug", "quote"),
    [
        (
            "JNJ",
            "Shingrix",
            "Data from the co-administration study with GSK's Shingrix are expected in 2027.",
        ),
        (
            "MRNA",
            "Shingrix",
            "Data from the co-administration study with GSK's Shingrix are expected in 2027.",
        ),
        ("NVAX", "Fluzone", "Sanofi's Fluzone High-Dose label update is expected in 2027."),
        ("NVAX", "Flublok Quadrivalent", "Sanofi's Flublok Quadrivalent data are expected in 2027."),
        ("MRNA", "Afluria Quadrivalent", "Afluria Quadrivalent data are expected in 2027."),
        ("INCY", "Yescarta", "Kite expects to file Yescarta for second-line follicular lymphoma in 2027."),
        ("BMY", "Taxane", "The taxane comparator arm will read out in 2027."),
        ("LLY", "FGF Receptor", "FGF receptor inhibitor data are expected in 2027."),
        (
            "PFE",
            "Clostridium difficile",
            "Clostridium difficile vaccine data from another company are expected in 2027.",
        ),
    ],
)
def test_another_company_s_product_or_a_class_word_is_never_the_sponsor_s_drug(
    plan: DrugPlan, ticker: str, claim_drug: str, quote: str
) -> None:
    assert not [d for d in plan.drugs if d.company_id == COMPANY[ticker] and name_key(claim_drug) in d.names]
    _held(plan, ticker, claim_drug, quote)


def test_the_cber_list_knows_the_rival_products() -> None:
    brands = {name for _, _, names in CBER_PRODUCTS for name in names}
    assert {"SHINGRIX", "FLUZONE", "FLUBLOK", "AFLURIA", "YESCARTA"} <= brands


def test_a_marked_rival_brand_is_dropped_by_the_cber_list() -> None:
    """Constructed: Acme's trial gives "SHINGRIX®", which passes the drug-name shape as a marked brand."""
    record = SourceRecord(
        DrugNameSource.CTGOV, "N1", "Acme Bio", (ListedName("SHINGRIX", DrugNameKind.BRAND, marked=True),)
    )
    assert plan_drugs([record], SPONSORS).drugs == []


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("navitoclax", True),
        ("Tanruprubart", True),
        ("inaxaplin", True),
        ("cemacabtagene ansegedleucel", True),
        ("izalontamab brengitecan", True),
        ("ELEVIDYS", True),
        ("Taxane", False),
        ("FGF Receptor", False),
        ("Clostridium difficile", False),
        ("Shingrix", False),
        ("Iza-bren", False),
        ("Avidin", False),
        ("Yellow Fever", False),
        ("Skin", False),
    ],
)
def test_a_plain_word_main_name_must_look_like_a_drug_name(name: str, expected: bool) -> None:
    assert looks_like_a_drug(name) is expected


# Belt and braces: a drug named only by registry words waits for review


def test_a_drug_known_only_by_registry_words_waits_for_review() -> None:
    company = uuid4()
    words = DrugRef(uuid4(), company, (("ELEVIDYS", DrugNameKind.BRAND),), unconfirmed=True)
    coded = DrugRef(uuid4(), company, (("navitoclax", DrugNameKind.INN), ("ABT-263", DrugNameKind.CODE)))
    references = ReferenceData([CompanyRef(company, 0, (), ())], [words, coded], [])
    held = known_drug(references, "ELEVIDYS", company)
    assert (held.status, held.candidates) == (Status.AMBIGUOUS, (words.id,))
    assert known_drug(references, "navitoclax", company).id == coded.id


def test_a_planned_drug_without_codes_or_fda_names_is_unconfirmed() -> None:
    records = [
        SourceRecord(DrugNameSource.CTGOV, "N1", "Acme Bio", (ListedName("ELEVIDYS", DrugNameKind.BRAND),)),
        SourceRecord(
            DrugNameSource.CTGOV,
            "N2",
            "Acme Bio",
            (
                ListedName("navitoclax", DrugNameKind.INN),
                ListedName("ABT-263", DrugNameKind.CODE, main=False),
            ),
        ),
    ]
    found = {d.preferred_name: d.unconfirmed for d in plan_drugs(records, SPONSORS).drugs}
    assert found == {"ELEVIDYS": True, "navitoclax": False}


# Cost item: an INN-shaped generic joins its own code in the same intervention


@pytest.mark.parametrize(
    ("generic", "code"),
    [
        ("navitoclax", "ABT-263"),
        ("inaxaplin", "VX-147"),
        ("tanruprubart", "ANX005"),
        ("obicetrapib", "TA-8995"),
    ],
)
def test_an_inn_shaped_generic_joins_the_code_its_intervention_gives(generic: str, code: str) -> None:
    records = [
        SourceRecord(
            DrugNameSource.CTGOV,
            "N1",
            "Acme Bio",
            (ListedName(generic, DrugNameKind.INN), ListedName(code, DrugNameKind.CODE, main=False)),
        ),
        SourceRecord(DrugNameSource.CTGOV, "N2", "Acme Bio", (ListedName(code, DrugNameKind.CODE),)),
    ]
    assert [sorted(d.names) for d in plan_drugs(records, SPONSORS).drugs] == [
        sorted([name_key(generic), name_key(code)])
    ]


def test_itacitinib_keeps_its_code(plan: DrugPlan) -> None:
    (drug,) = [d for d in plan.drugs if d.company_id == COMPANY["INCY"] and "itacitinib" in d.names]
    assert "incb039110" in drug.names


def test_a_class_word_with_a_code_leaves_the_code_as_the_drug(plan: DrugPlan) -> None:
    """Lilly's "FGF Receptor" with LY2874455 is LY2874455 alone."""
    found = [d for d in plan.drugs if "ly2874455" in d.names]
    assert [sorted(d.names) for d in found] == [["ly2874455"]]


def test_reference_data_marks_a_stored_drug_named_only_by_registry_words(
    session_factory: sessionmaker[Session],
) -> None:
    """The matching job reads the flag from `drug_name.source`: every name from the registry, none a code."""
    stored = {
        "words": [("ELEVIDYS", DrugNameKind.BRAND, DrugNameSource.CTGOV)],
        "coded": [
            ("navitoclax", DrugNameKind.INN, DrugNameSource.CTGOV),
            ("ABT-263", DrugNameKind.CODE, DrugNameSource.CTGOV),
        ],
        "fda": [("BRINSUPRI", DrugNameKind.BRAND, DrugNameSource.OPENFDA)],
        "claim": [("Vonsetamig", DrugNameKind.INN, DrugNameSource.CLAIM)],
    }
    ids: dict[str, UUID] = {}
    with session_factory.begin() as session:
        for label, names in stored.items():
            drug = Drug(preferred_name=names[0][0])
            ids[label] = drug.id
            session.execute(insert(TABLES[Drug]).values(**drug.model_dump()))
            for name, kind, source in names:
                row = DrugName(drug_id=drug.id, name=name, kind=kind, source=source)
                session.execute(insert(TABLES[DrugName]).values(**row.model_dump()))
    with session_factory() as session:
        flags = {d.id: d.unconfirmed for d in drug_refs(session)}
    assert {label: flags[i] for label, i in ids.items()} == {
        "words": True,
        "coded": False,
        "fda": False,
        "claim": False,
    }
