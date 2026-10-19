"""Loading the drug list: fetched politely, stored with each name's records, safe to re-run (ADR 0035).

The requests are answered from recorded copies of the real public APIs
(`tests/fixtures/drug-list/`, recorded 2026-10-19); nothing reaches a live website.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import insert, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.cli import main
from catalyst.collectors.http import PoliteHttpClient
from catalyst.collectors.run import PilotCompany, build_clients, collect_all
from catalyst.collectors.sources import ctgov, openfda
from catalyst.db.repository import link_company_document, save_source_document, upsert_company
from catalyst.db.tables import TABLES
from catalyst.drug_list_store import load_drugs
from catalyst.resolve import name_key
from catalyst.resolve_claims import resolve_claims
from catalyst.schemas import (
    Claim,
    ClaimSubject,
    DocumentMatch,
    Drug,
    DrugName,
    DrugNameKind,
    EntityMatch,
    SourceDocument,
    SourceTier,
    WithheldDrugName,
)
from catalyst.snapshots import LocalSnapshotStore
from conftest import RecordedServer

NOW = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
FIXTURE = "drug-list"
# The fixture's Drugs@FDA search: these applications only, so the test pages one recorded answer.
APPLICATIONS = "NDA207356 NDA217673 NDA050710 NDA220305 BLA761174 NDA218730 NDA202192 NDA215309 NDA050495"
SEARCH = " ".join(f"application_number:{number}" for number in APPLICATIONS.split())
INSMED = PilotCompany(ticker="INSM", cik=1104506, ctgov_sponsors=frozenset({"Insmed Incorporated"}))
KURA = PilotCompany(ticker="KURA", cik=1422143, ctgov_sponsors=frozenset({"Kura Oncology, Inc."}))
PFIZER = PilotCompany(ticker="PFE", cik=78003)
COMPANIES = [INSMED, KURA, PFIZER]
BOTH = frozenset({"openfda", "ctgov"})


@pytest.fixture
def companies(session_factory: sessionmaker[Session]) -> dict[str, UUID]:
    with session_factory.begin() as session:
        return {
            "INSM": upsert_company(session, INSMED.cik, "INSMED Inc", "2834").id,
            "KURA": upsert_company(session, KURA.cik, "Kura Oncology, Inc.", "2834").id,
            "PFE": upsert_company(session, PFIZER.cik, "PFIZER INC", "2834").id,
        }


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> RecordedServer:
    """Answer from the recorded copies, with the Drugs@FDA search narrowed to the recorded applications."""
    original = openfda.collect_drug_list

    def narrowed(client: PoliteHttpClient, page_size: int = 100, search: str = SEARCH) -> Any:
        return original(client, page_size=100, search=SEARCH)

    monkeypatch.setattr(openfda, "collect_drug_list", narrowed)
    return RecordedServer(FIXTURE)


def _load(
    session_factory: sessionmaker[Session], server: RecordedServer, sources: frozenset[str] = BOTH
) -> dict[str, Any]:
    client = build_clients("data@example.org", server.transport, rate_limits=False).web
    return load_drugs(session_factory, client, COMPANIES, lambda: NOW, sources).summary()


def _names(session_factory: sessionmaker[Session]) -> list[dict[str, Any]]:
    drug, name = TABLES[Drug], TABLES[DrugName]
    query = select(name, drug.c.company_id, drug.c.preferred_name).join(drug, drug.c.id == name.c.drug_id)
    with session_factory() as session:
        return [dict(row) for row in session.execute(query).mappings()]


def _drug_names(rows: list[dict[str, Any]], company: UUID, key: str) -> dict[str, dict[str, Any]]:
    drug = next(r["drug_id"] for r in rows if r["company_id"] == company and name_key(r["name"]) == key)
    return {name_key(r["name"]): r for r in rows if r["drug_id"] == drug}


def test_the_drug_list_stores_each_company_s_names_with_their_records(
    session_factory: sessionmaker[Session], companies: dict[str, UUID], recorded: RecordedServer
) -> None:
    report = _load(session_factory, recorded)
    rows = _names(session_factory)
    brensocatib = _drug_names(rows, companies["INSM"], "brensocatib")
    assert {"brensocatib", "brinsupri", "ins1007"} <= set(brensocatib)
    assert (brensocatib["brinsupri"]["source"], brensocatib["brinsupri"]["source_refs"]) == (
        "openfda",
        ["NDA217673"],
    )
    assert brensocatib["ins1007"]["source"] == "ctgov"
    assert all(ref.startswith("NCT") for ref in brensocatib["ins1007"]["source_refs"])
    assert brensocatib["brensocatib"]["preferred_name"] == "brensocatib"
    assert {"ziftomenib", "ko539", "komzifti"} <= set(_drug_names(rows, companies["KURA"], "ziftomenib"))
    # Azithromycin, a comparator in Insmed's trials, is Pfizer's (ZITHROMAX), never Insmed's.
    insmed = {name_key(r["name"]) for r in rows if r["company_id"] == companies["INSM"]}
    assert "azithromycin" not in insmed
    assert "azithromycin" in {name_key(r["name"]) for r in rows if r["company_id"] == companies["PFE"]}
    assert report["drugs_created"] > 0
    assert report["comparators"] > 0
    # Only the two public APIs were asked, and only through their documented endpoints.
    assert {url.split("?")[0] for url in recorded.requested} == {openfda.API_URL, ctgov.API_URL}


def test_a_second_run_adds_nothing(
    session_factory: sessionmaker[Session], companies: dict[str, UUID], recorded: RecordedServer
) -> None:
    first = _load(session_factory, recorded)
    before = _names(session_factory)
    second = _load(session_factory, recorded)
    assert sorted((r["drug_id"], r["name"], r["source_refs"]) for r in _names(session_factory)) == sorted(
        (r["drug_id"], r["name"], r["source_refs"]) for r in before
    )
    assert (second["drugs_created"], second["names_added"]) == (0, 0)
    assert first["names_added"] == len(before)


def test_the_company_s_code_name_drug_on_file_gains_its_generic_and_brand_names(
    session_factory: sessionmaker[Session], companies: dict[str, UUID], recorded: RecordedServer
) -> None:
    """A drug the matching job created from a release's code name keeps its record and its marker."""
    with session_factory.begin() as session:
        drug = Drug(company_id=companies["INSM"], preferred_name="INS1007")
        session.execute(insert(TABLES[Drug]).values(**drug.model_dump()))
        code = DrugName(drug_id=drug.id, name="INS1007", kind=DrugNameKind.CODE)
        session.execute(insert(TABLES[DrugName]).values(**code.model_dump()))
    _load(session_factory, recorded)
    names = _drug_names(_names(session_factory), companies["INSM"], "ins1007")
    assert names["ins1007"]["drug_id"] == drug.id
    assert names["ins1007"]["source"] == "claim"  # first found in the company's own release
    assert names["ins1007"]["source_refs"]  # and now also backed by its registry records
    assert {"brensocatib", "brinsupri"} <= set(names)
    # The spec shows a drug by its generic name: FDA's replaces the code name (Opus review of PR #73).
    assert names["ins1007"]["preferred_name"] == "brensocatib"


def test_a_name_another_company_s_drug_already_has_is_withheld_for_review(
    session_factory: sessionmaker[Session], companies: dict[str, UUID], recorded: RecordedServer
) -> None:
    with session_factory.begin() as session:
        drug = Drug(company_id=companies["PFE"], preferred_name="ziftomenib")
        session.execute(insert(TABLES[Drug]).values(**drug.model_dump()))
        name = DrugName(drug_id=drug.id, name="ziftomenib", kind=DrugNameKind.INN)
        session.execute(insert(TABLES[DrugName]).values(**name.model_dump()))
    _load(session_factory, recorded)
    kura = {name_key(r["name"]) for r in _names(session_factory) if r["company_id"] == companies["KURA"]}
    assert "ziftomenib" not in kura
    assert "ko539" in kura
    with session_factory() as session:
        withheld = session.execute(
            select(TABLES[WithheldDrugName]).where(TABLES[WithheldDrugName].c.name.ilike("ziftomenib"))
        ).mappings()
        row = dict(next(iter(withheld)))
    assert row["reason"] == "shared"
    assert set(row["company_ids"]) == {str(companies["KURA"]), str(companies["PFE"])}
    assert row["source_refs"]


def test_a_failed_request_stores_nothing(
    session_factory: sessionmaker[Session], companies: dict[str, UUID], recorded: RecordedServer
) -> None:
    """Kura's trials are not answered, so a list built without them could misplace a shared name."""
    kura_page = next(url for url in recorded.responses if "Kura" in url)
    del recorded.responses[kura_page]
    with pytest.raises(httpx.HTTPStatusError, match="404"):
        _load(session_factory, recorded)
    assert _names(session_factory) == []


def test_a_run_of_drugs_at_fda_alone_keeps_the_names_withheld_for_registry_records(
    session_factory: sessionmaker[Session], companies: dict[str, UUID], recorded: RecordedServer
) -> None:
    """Kura's registry name darlifarnib, already on file under Pfizer here, is withheld as shared."""
    with session_factory.begin() as session:
        drug = Drug(company_id=companies["PFE"], preferred_name="Darlifarnib")
        session.execute(insert(TABLES[Drug]).values(**drug.model_dump()))
        name = DrugName(drug_id=drug.id, name="Darlifarnib", kind=DrugNameKind.INN)
        session.execute(insert(TABLES[DrugName]).values(**name.model_dump()))
    _load(session_factory, recorded)
    withheld = TABLES[WithheldDrugName]
    with session_factory() as session:
        before = set(session.execute(select(withheld.c.name)).scalars())
    assert "Darlifarnib" in before
    _load(session_factory, recorded, frozenset({"openfda"}))
    with session_factory() as session:
        assert set(session.execute(select(withheld.c.name)).scalars()) >= before


def test_registry_names_need_drugs_at_fda(
    session_factory: sessionmaker[Session], companies: dict[str, UUID]
) -> None:
    client = build_clients("data@example.org", RecordedServer(FIXTURE).transport, rate_limits=False).web
    with pytest.raises(ValueError, match="Drugs@FDA"):
        load_drugs(session_factory, client, COMPANIES, lambda: NOW, frozenset({"ctgov"}))


def test_the_command_refuses_registry_names_without_drugs_at_fda(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["load-drugs", "--sources", "ctgov"])
    assert "ctgov needs openfda" in capsys.readouterr().err


def test_collect_runs_the_drug_list_only_when_named(
    session_factory: sessionmaker[Session],
    companies: dict[str, UUID],
    recorded: RecordedServer,
    tmp_path: Any,
) -> None:
    clients = build_clients("data@example.org", recorded.transport, rate_limits=False)
    store = LocalSnapshotStore(tmp_path)
    stats = collect_all(
        COMPANIES, clients, store, session_factory, NOW.date(), lambda: NOW, sources=frozenset({"drugs"})
    )
    assert [(s.source, s.error) for s in stats] == [("drugs", None)]
    assert stats[0].created == len(_names(session_factory))


# The resolver with the loaded list (CP4 review, O1)


def _claim(session: Session, company: UUID, key: str, drug: str) -> UUID:
    document, _ = save_source_document(
        session,
        SourceDocument(
            source_key=key,
            url=f"https://www.sec.gov/{key}.htm",
            publisher="SEC EDGAR",
            source_tier=SourceTier.REGULATOR_OR_SEC,
            document_type="8-K EX-99.1",
            published_at=NOW,
            retrieved_at=NOW,
            sha256=key[-1] * 64,
            snapshot_key="sha256/" + key[-1] * 2 + "/" + key[-1] * 64,
            content_type="text/html",
        ),
    )
    link_company_document(session, company, document.id, DocumentMatch.SEC_CIK)
    quote = f"Topline {drug} data are expected in 2027."
    claim = Claim(
        source_document_id=document.id,
        subject_type=ClaimSubject.CATALYST,
        field=f"catalyst:{key}",
        value={"catalyst_type": "topline_readout", "drug": drug, "title": "t", "problems": []},
        quote=quote,
        quote_start=0,
        quote_end=len(quote),
        agent="extractor",
        prompt_id="extract_catalysts",
        prompt_version="1.0.0",
        provider="anthropic",
        model="m",
        extracted_at=NOW,
    )
    session.execute(insert(TABLES[Claim]).values(**claim.model_dump()))
    return claim.id


def test_loaded_names_resolve_for_their_company_and_wait_for_review_elsewhere(
    session_factory: sessionmaker[Session], companies: dict[str, UUID], recorded: RecordedServer
) -> None:
    _load(session_factory, recorded)
    with session_factory.begin() as session:
        own = _claim(session, companies["INSM"], "sec:insmed-a", "brensocatib")
        theirs = _claim(session, companies["KURA"], "sec:kura-b", "BRINSUPRI")
    resolve_claims(session_factory, lambda: NOW)
    match = TABLES[EntityMatch]
    with session_factory() as session:
        rows = {
            row.claim_id: (row.status, row.matched_by)
            for row in session.execute(select(match).where(match.c.entity_type == "drug"))
        }
    assert rows[own] == ("resolved", "inn")
    assert rows[theirs] == ("ambiguous", "brand")  # Insmed's drug named in Kura's release
