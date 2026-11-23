"""A drug named by the company: an unknown name its own document writes becomes its drug (ADR 0060).

Judged against ADR 0060. The matching job may add a drug for the claim's company only when the
name is a drug's name by the rules, no record anywhere has it, and the stored copy of the
company's own document names it in the claim's quote or just before it (the evidence of
`drug_named_near_quote`). Every other case must stay held, so a wrong or unverified drug never
reaches the publish gate through this path. Every name and sentence here is made up.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.repository import link_company_document, save_source_document, upsert_company
from catalyst.db.tables import TABLES
from catalyst.drug_list import DrugPlan, PlannedDrug, PlannedName
from catalyst.drug_list_store import store_plan
from catalyst.extract.text import document_text
from catalyst.publish.gate import REQUIRED_CHECKS
from catalyst.publish.sanity import drug_role
from catalyst.references import record_trial
from catalyst.resolve import name_key
from catalyst.resolve_claims import resolve_claims
from catalyst.schemas import (
    Claim,
    ClaimCheck,
    ClaimSubject,
    DocumentMatch,
    Drug,
    DrugName,
    DrugNameKind,
    DrugNameSource,
    DrugRole,
    EntityMatch,
    SourceDocument,
    SourceTier,
    Trial,
    TrialPhase,
    WithheldDrugName,
)
from catalyst.snapshots import LocalSnapshotStore
from catalyst.verify.drug import CHECK, LOOK_BACK, check_drug_named
from catalyst.verify.run import run_checks

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)
QUOTE = "Topline data are expected in the fourth quarter of 2027."


class World:
    """One company, its releases as stored copies, and their claims."""

    def __init__(self, factory: sessionmaker[Session], store: LocalSnapshotStore) -> None:
        self.factory, self.store, self._n = factory, store, 0
        with factory.begin() as session:
            self.company = upsert_company(session, 1001, "Acme Bio, Inc.", "2834").id
            self.other = upsert_company(session, 1002, "Borealis Pharma, Inc.", "2834").id

    def release(self, *paragraphs: str, companies: tuple[UUID, ...] | None = None) -> tuple[UUID, str]:
        """Store a release made of these paragraphs; return its document and its visible text."""
        html = "<html><body>" + "".join(f"<p>{p}</p>" for p in paragraphs) + "</body></html>"
        snapshot = self.store.put(html.encode())
        self._n += 1
        with self.factory.begin() as session:
            document, _ = save_source_document(
                session,
                SourceDocument(
                    source_key=f"sec:acme-{self._n}",
                    url=f"https://www.sec.gov/acme-{self._n}.htm",
                    publisher="SEC EDGAR",
                    source_tier=SourceTier.REGULATOR_OR_SEC,
                    document_type="8-K EX-99.1",
                    published_at=NOW,
                    retrieved_at=NOW,
                    sha256=snapshot.sha256,
                    snapshot_key=snapshot.key,
                    content_type="text/html",
                ),
            )
            for company in (self.company,) if companies is None else companies:
                link_company_document(session, company, document.id, DocumentMatch.SEC_CIK)
        return document.id, document_text(html.encode(), "text/html")

    def claim(
        self, release: tuple[UUID, str], drug: str, quote: str = QUOTE, indication: str | None = None
    ) -> UUID:
        document, text = release
        self._n += 1
        value: dict[str, Any] = {"catalyst_type": "topline_readout", "drug": drug, "title": "t"}
        value |= {"problems": list[str](), "indication": indication}
        claim = Claim(
            source_document_id=document,
            subject_type=ClaimSubject.CATALYST,
            field=f"catalyst:{self._n:016x}",
            value=value,
            quote=quote,
            quote_start=text.index(quote),
            quote_end=text.index(quote) + len(quote),
            agent="extractor",
            prompt_id="extract_catalysts",
            prompt_version="1.0.0",
            provider="anthropic",
            model="m",
            extracted_at=NOW,
        )
        with self.factory.begin() as session:
            session.execute(insert(TABLES[Claim]).values(**claim.model_dump()))
        return claim.id

    def stated(self, drug: str, *paragraphs: str, quote: str = QUOTE) -> UUID:
        """Add a release of these paragraphs and one claim on it that states this drug."""
        return self.claim(self.release(*paragraphs), drug, quote)

    def drug(self, company: UUID | None, *names: tuple[str, DrugNameKind]) -> UUID:
        drug = Drug(company_id=company, preferred_name=names[0][0])
        with self.factory.begin() as session:
            session.execute(insert(TABLES[Drug]).values(**drug.model_dump()))
            for name, kind in names:
                row = DrugName(drug_id=drug.id, name=name, kind=kind, source=DrugNameSource.OPENFDA)
                session.execute(insert(TABLES[DrugName]).values(**row.model_dump()))
        return drug.id

    def resolve(self) -> dict[str, Any]:
        return resolve_claims(self.factory, lambda: NOW, self.store).summary()

    def rows(self, model: type[Any]) -> list[Any]:
        with self.factory() as session:
            return list(session.execute(select(TABLES[model])).mappings())

    def matches(self, claim: UUID) -> dict[str, tuple[str, UUID | None, str | None]]:
        """Return the claim's drug matches by name: status, record, and rule."""
        return {
            r["name"]: (r["status"], r["matched_id"], r["matched_by"])
            for r in self.rows(EntityMatch)
            if r["claim_id"] == claim and r["entity_type"] == "drug"
        }

    def names(self, drug: UUID | None) -> dict[str, dict[str, Any]]:
        return {r["name"]: dict(r) for r in self.rows(DrugName) if r["drug_id"] == drug}

    def check(self, claim: UUID, name: str) -> str:
        (row,) = [r for r in self.rows(ClaimCheck) if r["claim_id"] == claim and r["check_name"] == name]
        return row["outcome"]

    def count(self, model: type[Any]) -> int:
        with self.factory() as session:
            return session.execute(select(func.count()).select_from(TABLES[model])).scalar_one()


@pytest.fixture
def world(session_factory: sessionmaker[Session], tmp_path: Path) -> World:
    return World(session_factory, LocalSnapshotStore(tmp_path))


def _held(world: World, claim: UUID, drug: str, drugs_before: int = 0) -> None:
    """Check the drug stays unmatched, nothing was created, and `names_matched` holds the claim."""
    assert world.matches(claim) == {drug[:300]: ("unknown", None, None)}
    assert world.count(Drug) == drugs_before
    run_checks(world.factory, lambda: NOW)
    assert world.check(claim, "names_matched") == "fail"


# --- The rule ---


def test_an_unknown_name_the_release_writes_in_the_quote_becomes_the_company_s_drug(world: World) -> None:
    quote = "Topline data for zetamab are expected in the fourth quarter of 2027."
    claim = world.stated("zetamab", "Acme Bio reports results.", quote, quote=quote)
    assert world.resolve()["drugs_created"] == 1
    ((status, drug_id, rule),) = world.matches(claim).values()
    assert (status, rule) == ("resolved", "company_named")
    (drug,) = world.rows(Drug)
    assert (drug["id"], drug["company_id"], drug["preferred_name"]) == (drug_id, world.company, "zetamab")
    (name,) = world.names(drug_id).values()
    # Marked as found in the company's own document, with the claim as its provenance.
    assert (name["kind"], name["source"], name["source_refs"]) == ("inn", "claim", [f"claim:{claim}"])
    run_checks(world.factory, lambda: NOW)
    check_drug_named(world.factory, world.store, lambda: NOW)
    assert (world.check(claim, "names_matched"), world.check(claim, CHECK)) == ("pass", "pass")


@pytest.mark.parametrize(
    ("drug", "kind"),
    [
        ("zetamab", "inn"),
        ("BRANDEX", "brand"),
        ("QRX-4521", "code"),
        ("4QR-150", "code"),
        ("qrxT-797", "code"),
    ],
)
def test_a_name_written_just_before_the_quote_counts_whatever_its_kind(
    world: World, drug: str, kind: str
) -> None:
    claim = world.stated(drug, f"{drug} in adults with a rare disease", QUOTE)
    world.resolve()
    ((status, drug_id, rule),) = world.matches(claim).values()
    assert (status, rule) == ("resolved", "company_named")
    assert [n["kind"] for n in world.names(drug_id).values()] == [kind]


def test_a_second_claim_and_a_second_run_find_the_same_drug_and_add_nothing(world: World) -> None:
    release = world.release("QRX-4521 is our lead medicine.", QUOTE, "Enrollment in the study is complete.")
    first = world.claim(release, "QRX-4521")
    second = world.claim(release, "qrx 4521", "Enrollment in the study is complete.")
    assert world.resolve()["drugs_created"] == 1
    assert world.resolve()["drugs_created"] == 0
    (one,) = world.matches(first).values()
    (two,) = world.matches(second).values()
    assert one == ("resolved", one[1], "company_named")  # the marker stays while that drug is the match
    assert two == ("resolved", one[1], "code_name")
    assert (world.count(Drug), world.count(DrugName)) == (1, 1)


def test_a_held_claim_is_released_by_the_next_run_without_a_new_reading(world: World) -> None:
    """Every run matches every claim again, so a claim held before the rule existed is picked up."""
    claim = world.stated("zetamab", "zetamab in adults with a rare disease", QUOTE)
    resolve_claims(world.factory, lambda: NOW)  # as before this rule: no stored copy is read
    _held(world, claim, "zetamab")
    world.resolve()
    run_checks(world.factory, lambda: NOW)
    assert world.check(claim, "names_matched") == "pass"
    assert world.count(Claim) == 1


# --- What must stay held ---


@pytest.mark.parametrize(
    "drug",
    [
        "the drug",
        "our lead program",
        "the combination",
        "lead candidate",
        "novel oral small molecule",
        "placebo",
        "pipeline",
        "ASCO 2027",
        "QRX program",
        "BUY 100",
        "January",
        "QRX",
        "zetamab cell therapy",
        "FGF Receptor",
        "Taxane",
        "Zetamabolinumab" * 5,
    ],
)
def test_a_phrase_that_names_no_drug_stays_held_even_when_the_quote_writes_it(
    world: World, drug: str
) -> None:
    quote = f"Topline data for {drug} are expected in the fourth quarter of 2027."
    claim = world.stated(drug, quote, quote=quote)
    assert world.resolve()["drugs_created"] == 0
    _held(world, claim, drug)


@pytest.mark.parametrize(
    ("paragraphs", "drug_check"),
    [
        (("Acme Bio reports results for lumetinib.", QUOTE), "fail"),
        (("Results for zetamabs were shown.", QUOTE), "fail"),
        # The drug check reads "zetamab-treated" as naming zetamab, so it reads this longer name
        # that way too; a new drug needs the name written alone, as the resolver reads names.
        (("Results for pre-zetamab-2 were shown.", QUOTE), "pass"),
        (("zetamab was discussed." + " More words follow here." * (LOOK_BACK // 20), QUOTE), "fail"),
        (("zetamab was discussed. Our other medicine QRX-4521 completed enrollment.", QUOTE), "fail"),
    ],
    ids=["not written", "inside a longer word", "joined to more", "too far before", "another drug after it"],
)
def test_a_name_the_document_does_not_write_at_the_quote_stays_held(
    world: World, paragraphs: tuple[str, ...], drug_check: str
) -> None:
    release = world.release(*paragraphs)
    claim = world.claim(release, "zetamab")
    if "QRX-4521" in paragraphs[0]:
        world.claim(release, "QRX-4521", "Our other medicine QRX-4521 completed enrollment.")
    world.resolve()
    assert world.matches(claim) == {"zetamab": ("unknown", None, None)}
    run_checks(world.factory, lambda: NOW)
    check_drug_named(world.factory, world.store, lambda: NOW)
    # The gate requires both checks: a drug its document does not name at the quote cannot publish.
    assert (world.check(claim, "names_matched"), world.check(claim, CHECK)) == ("fail", drug_check)
    assert {"names_matched", CHECK} <= set(REQUIRED_CHECKS)


@pytest.mark.parametrize("kind", [DrugNameKind.INN, DrugNameKind.BRAND, DrugNameKind.CODE])
def test_a_name_another_company_s_drug_has_stays_ambiguous(world: World, kind: DrugNameKind) -> None:
    theirs = world.drug(world.other, ("ZETAMAB-7", kind))
    claim = world.stated("ZETAMAB-7", "ZETAMAB-7 in adults with a rare disease", QUOTE)
    assert world.resolve()["drugs_created"] == 0
    assert world.matches(claim) == {"ZETAMAB-7": ("ambiguous", None, world.matches(claim)["ZETAMAB-7"][2])}
    assert [r["id"] for r in world.rows(Drug)] == [theirs]


def test_a_name_two_records_share_stays_ambiguous(world: World) -> None:
    world.drug(world.company, ("zetamab", DrugNameKind.INN))
    world.drug(None, ("zetamab", DrugNameKind.INN))
    claim = world.stated("zetamab", "zetamab in adults with a rare disease", QUOTE)
    assert world.resolve()["drugs_created"] == 0
    assert world.matches(claim)["zetamab"][0] == "ambiguous"
    assert world.count(Drug) == 2


def test_a_name_withheld_as_shared_by_two_companies_records_stays_held(world: World) -> None:
    withheld = WithheldDrugName(
        name="Zetamab",
        reason="shared",
        company_ids=[str(world.company), str(world.other)],
        source_refs=["NCT00000001"],
        recorded_at=NOW,
    )
    with world.factory.begin() as session:
        session.execute(insert(TABLES[WithheldDrugName]).values(**withheld.model_dump()))
    claim = world.stated("zetamab", "zetamab in adults with a rare disease", QUOTE)
    world.resolve()
    _held(world, claim, "zetamab")


def test_a_study_name_or_a_company_name_is_not_a_drug(world: World) -> None:
    with world.factory.begin() as session:
        record_trial(session, Trial(nct_id="NCT06111111", study_name="SUMMIT", phase=TrialPhase.PHASE_3))
    study = world.stated("SUMMIT", "SUMMIT in adults with a rare disease", QUOTE)
    company = world.stated("Borealis Pharma", "Borealis Pharma in adults with a rare disease", QUOTE)
    assert world.resolve()["drugs_created"] == 0
    assert world.matches(study) == {"SUMMIT": ("unknown", None, None)}
    assert world.matches(company) == {"Borealis Pharma": ("unknown", None, None)}


def test_the_words_of_the_claim_s_own_indication_are_not_a_drug(world: World) -> None:
    """A disease no word list has, read as the drug: the claim's own indication gives it away."""
    quote = "Topline data in Clostridium difficile are expected in the fourth quarter of 2027."
    release = world.release(quote)
    claim = world.claim(release, "Clostridium difficile", quote, "recurrent Clostridium difficile infection")
    world.resolve()
    _held(world, claim, "Clostridium difficile")


@pytest.mark.parametrize("drug", ["Shingrix", "Yescarta", "FLUZONE"])
def test_a_vaccine_or_cell_therapy_licensed_to_another_company_is_not_the_company_s_drug(
    world: World, drug: str
) -> None:
    """ADR 0035's fifth review: a rival's licensed product named in a release must wait for review."""
    claim = world.stated(drug, f"{drug} in adults with a rare disease", QUOTE)
    world.resolve()
    _held(world, claim, drug)


def test_the_licence_holder_s_own_product_is_its_drug(world: World) -> None:
    with world.factory.begin() as session:
        kite = upsert_company(session, 1003, "Kite Pharma, Inc.", "2834").id
    release = world.release("Yescarta in adults with a rare disease", QUOTE, companies=(kite,))
    claim = world.claim(release, "Yescarta")
    assert world.resolve()["drugs_created"] == 1
    assert world.matches(claim)["Yescarta"][0] == "resolved"


@pytest.mark.parametrize("linked", [0, 2])
def test_a_document_of_no_one_company_names_no_drug(world: World, linked: int) -> None:
    companies = (world.company, world.other)[:linked]
    release = world.release("zetamab in adults with a rare disease", QUOTE, companies=companies)
    claim = world.claim(release, "zetamab")
    assert world.resolve()["drugs_created"] == 0
    assert world.matches(claim) == {"zetamab": ("unknown", None, None)}


def test_a_stored_copy_that_cannot_be_read_names_no_drug(world: World, tmp_path: Path) -> None:
    claim = world.stated("zetamab", "zetamab in adults with a rare disease", QUOTE)
    for copy in [p for p in tmp_path.rglob("*") if p.is_file()]:
        copy.unlink()
    assert world.resolve()["drugs_created"] == 0
    _held(world, claim, "zetamab")


# --- Name forms ---


def test_a_brand_with_its_generic_name_in_brackets_is_one_drug_with_both_names(world: World) -> None:
    lead = "BRANDEX® (zetamab) sublingual film for adults with a rare disease"
    release = world.release(lead, QUOTE, "A second study has started.", "A third study has started.")
    both = world.claim(release, "BRANDEX (zetamab) sublingual film")
    brand = world.claim(release, "Brandex", "A second study has started.")
    generic = world.claim(release, "zetamab", "A third study has started.")
    assert world.resolve()["drugs_created"] == 1
    ((status, drug_id, _),) = world.matches(both).values()
    assert status == "resolved"
    assert {n: r["kind"] for n, r in world.names(drug_id).items()} == {"BRANDEX": "brand", "zetamab": "inn"}
    (drug,) = world.rows(Drug)
    assert drug["preferred_name"] == "zetamab"  # spec: generic name if any
    # Whichever claim the run reads first, a second run leaves all three on the one drug.
    assert world.resolve()["drugs_created"] == 0
    assert [m[1] for m in (*world.matches(brand).values(), *world.matches(generic).values())] == [drug_id] * 2
    assert (world.count(Drug), world.count(DrugName)) == (1, 2)


def test_two_names_the_document_never_writes_together_are_not_joined(world: World) -> None:
    lead = "BRANDEX is approved. Separately, zetamab is in a study."
    claim = world.stated("BRANDEX (zetamab)", lead, QUOTE)
    world.resolve()
    _held(world, claim, "BRANDEX (zetamab)")


@pytest.mark.parametrize("drug", ["BRANDEX foam 0.3%", "BRANDEX cream 0.05%", "subcutaneous BRANDEX"])
def test_a_strength_or_form_is_not_part_of_the_name(world: World, drug: str) -> None:
    claim = world.stated(drug, "BRANDEX foam 0.3% and cream for adults with a rare disease", QUOTE)
    world.resolve()
    assert world.matches(claim)[drug][0] == "resolved"
    assert [r["name"] for r in world.rows(DrugName)] == ["BRANDEX"]


def test_the_form_words_also_find_a_drug_already_on_file(world: World) -> None:
    on_file = world.drug(world.company, ("BRANDEX", DrugNameKind.BRAND))
    claim = world.stated("BRANDEX foam 0.3%", "BRANDEX foam 0.3% for adults", QUOTE)
    assert world.resolve()["drugs_created"] == 0
    assert world.matches(claim) == {"BRANDEX foam 0.3%": ("resolved", on_file, "brand")}


def test_a_combination_names_each_of_its_drugs(world: World) -> None:
    on_file = world.drug(world.company, ("lumetinib", DrugNameKind.INN))
    quote = "Topline data for zetamab plus lumetinib are expected in the fourth quarter of 2027."
    claim = world.stated("zetamab plus lumetinib", quote, quote=quote)
    assert world.resolve()["drugs_created"] == 1
    matches = world.matches(claim)
    assert matches["lumetinib"] == ("resolved", on_file, "inn")
    assert (matches["zetamab"][0], matches["zetamab"][2]) == ("resolved", "company_named")
    assert set(matches) == {"zetamab", "lumetinib"}


def test_a_combination_with_a_part_the_document_does_not_name_stays_held(world: World) -> None:
    quote = "Topline data for zetamab are expected in the fourth quarter of 2027."
    claim = world.stated("zetamab plus lumetinib", quote, quote=quote)
    world.resolve()
    matches = world.matches(claim)
    assert (matches["zetamab"][0], matches["lumetinib"][0]) == ("resolved", "unknown")
    run_checks(world.factory, lambda: NOW)
    assert world.check(claim, "names_matched") == "fail"


def test_each_drug_of_a_claim_matched_to_several_must_be_named_at_the_quote(world: World) -> None:
    """Both drugs are on file, but this release names only one: the drug check holds the claim."""
    world.drug(world.company, ("zetamab", DrugNameKind.INN))
    world.drug(world.company, ("lumetinib", DrugNameKind.INN))
    quote = "Topline data for zetamab are expected in the fourth quarter of 2027."
    only_one = world.stated("zetamab plus lumetinib", quote, quote=quote)
    both_quote = "Topline data for zetamab plus lumetinib are expected in the fourth quarter of 2027."
    both = world.stated("zetamab plus lumetinib", both_quote, quote=both_quote)
    world.resolve()
    assert [m[0] for m in world.matches(only_one).values()] == ["resolved", "resolved"]
    check_drug_named(world.factory, world.store, lambda: NOW)
    assert (world.check(only_one, CHECK), world.check(both, CHECK)) == ("fail", "pass")


@pytest.mark.parametrize(
    ("drug", "quote", "role"),
    [
        ("zetamab plus lumetinib", "Data for zetamab plus lumetinib are due.", DrugRole.IN_COMBINATION),
        ("zetamab with QRX-4521", "Data for zetamab with QRX-4521 are due.", DrugRole.IN_COMBINATION),
        (
            "zetamab in combination with lumetinib",
            "Data for zetamab in combination with lumetinib are due in 2027.",
            DrugRole.IN_COMBINATION,
        ),
        # The claim states a combination, but the quote does not: a person decides, never "alone".
        ("zetamab plus lumetinib", "Topline data are due in 2027.", None),
        ("zetamab plus lumetinib", "Data for zetamab plus lumetinib, and zetamab alone, are due.", None),
        ("zetamab plus lumetinib", "Data for the zetamab doublet with lumetinib are due in 2027.", None),
        # A list is each drug alone, unless the quote speaks of a combination.
        ("QRX-4521 and QRX-4522", "Data for QRX-4521 and QRX-4522 are due in 2027.", DrugRole.ALONE),
        ("QRX-4521, QRX-4522", "Topline data are due in 2027.", DrugRole.ALONE),
        ("QRX-4521 and QRX-4522", "Data for the combination of QRX-4521 and QRX-4522 are due.", None),
        ("QRX-4521 and QRX-4522", "Data for QRX-4521 and QRX-4522 with QRX-4523 are due.", None),
    ],
)
def test_the_role_of_several_stated_drugs_is_read_from_the_claim_and_the_quote(
    drug: str, quote: str, role: DrugRole | None
) -> None:
    assert drug_role(quote, drug, frozenset()) is role


# --- Until there are more official names ---


def test_a_later_document_that_writes_the_generic_name_beside_the_code_adds_it_to_the_same_drug(
    world: World,
) -> None:
    first = world.stated("QRX-4521", "QRX-4521 in adults with a rare disease", QUOTE)
    world.resolve()
    later = world.stated("zetamab (QRX-4521)", "zetamab (QRX-4521) in adults with a rare disease", QUOTE)
    alone = world.stated("zetamab", "zetamab in adults with a rare disease", QUOTE)
    world.resolve()
    (drug,) = world.rows(Drug)  # in whichever order the run reads the two claims
    assert drug["preferred_name"] == "zetamab"  # the more official name is shown from now on
    names = world.names(drug["id"])
    assert set(names) == {"QRX-4521", "zetamab"}
    assert names["zetamab"]["source_refs"] in ([f"claim:{later}"], [f"claim:{alone}"])
    assert world.resolve()["drugs_created"] == 0
    assert {m[1] for c in (first, later, alone) for m in world.matches(c).values()} == {drug["id"]}
    assert (world.count(Drug), world.count(DrugName)) == (1, 2)


def test_two_records_the_company_s_document_writes_as_one_drug_are_joined_when_one_is_spare(
    world: World,
) -> None:
    code = world.stated("QRX-4521", "QRX-4521 in adults with a rare disease", QUOTE)
    generic = world.stated("zetamab", "zetamab in adults with a rare disease", QUOTE)
    assert world.resolve()["drugs_created"] == 2  # nothing yet says they are one drug
    both = world.stated("zetamab (QRX-4521)", "zetamab (QRX-4521) in adults with a rare disease", QUOTE)
    world.resolve()
    (drug,) = world.rows(Drug)
    assert drug["preferred_name"] == "zetamab"
    assert set(world.names(drug["id"])) == {"QRX-4521", "zetamab"}
    assert {m[:2] for c in (code, generic, both) for m in world.matches(c).values()} == {
        ("resolved", drug["id"])
    }


def test_two_records_are_not_joined_when_both_have_official_names_or_the_document_does_not_say_so(
    world: World,
) -> None:
    world.drug(world.company, ("QRX-4521", DrugNameKind.CODE))  # named by the drug list
    world.drug(world.company, ("zetamab", DrugNameKind.INN))
    world.stated("LMT-300", "LMT-300 in adults with a rare disease", QUOTE)
    world.stated("lumetinib", "lumetinib in adults with a rare disease", QUOTE)
    world.resolve()
    listed = world.stated("zetamab (QRX-4521)", "zetamab (QRX-4521) in adults with a rare disease", QUOTE)
    apart = world.stated("lumetinib (LMT-300)", "lumetinib helps. LMT-300 (our other medicine) too.", QUOTE)
    world.resolve()
    assert world.count(Drug) == 4
    assert world.matches(listed)["zetamab (QRX-4521)"][0] == "ambiguous"
    assert world.matches(apart)["lumetinib (LMT-300)"][0] == "ambiguous"


def test_a_new_name_is_never_added_to_another_company_s_drug(world: World) -> None:
    theirs = world.drug(world.other, ("zetamab", DrugNameKind.INN))
    claim = world.stated("zetamab (QRX-4521)", "zetamab (QRX-4521) in adults with a rare disease", QUOTE)
    assert world.resolve()["drugs_created"] == 0
    assert world.matches(claim)["zetamab (QRX-4521)"][0] == "unknown"  # as "A (B)" always was (ADR 0035)
    assert set(world.names(theirs)) == {"zetamab"}


def test_a_later_drug_list_run_joins_its_official_names_to_the_company_named_drug(world: World) -> None:
    claim = world.stated("QRX-4521", "QRX-4521 in adults with a rare disease", QUOTE)
    world.resolve()
    (provisional,) = world.rows(Drug)
    plan = DrugPlan(
        drugs=[
            PlannedDrug(
                world.company,
                {
                    name_key(name): PlannedName(name, kind, source, [ref])
                    for name, kind, source, ref in (
                        ("ZETAMAB", DrugNameKind.INN, DrugNameSource.OPENFDA, "BLA700001"),
                        ("BRANDEX", DrugNameKind.BRAND, DrugNameSource.OPENFDA, "BLA700001"),
                        ("QRX-4521", DrugNameKind.CODE, DrugNameSource.CTGOV, "NCT07000001"),
                    )
                },
            )
        ]
    )
    for _ in range(2):  # a second run adds nothing
        with world.factory.begin() as session:
            store_plan(session, plan, NOW)
    (drug,) = world.rows(Drug)
    assert (drug["id"], drug["preferred_name"]) == (provisional["id"], "zetamab")
    names = world.names(drug["id"])
    assert set(names) == {"QRX-4521", "ZETAMAB", "BRANDEX"}
    assert names["QRX-4521"]["source_refs"] == [f"claim:{claim}", "NCT07000001"]
    world.resolve()
    assert world.matches(claim)["QRX-4521"][:2] == ("resolved", drug["id"])
