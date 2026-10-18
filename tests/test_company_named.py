"""A claim's company must be named in its own source document (spec: Verification; ADR 0018, part 3).

The planted-error test found that a claim whose document is linked to the wrong company passed
every check. These tests use the recorded SEC 8-K press releases from the gold set and Vertex's
recorded release: each names its own company, and none names another company in the set.
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.repository import link_company_document, save_source_document, upsert_company
from catalyst.db.tables import TABLES
from catalyst.extract.text import document_text
from catalyst.references import CompanyIdentity, record_company_identity
from catalyst.resolve import CompanyRef
from catalyst.schemas import Claim, ClaimCheck, ClaimSubject, DocumentMatch, SourceDocument, SourceTier
from catalyst.snapshots import LocalSnapshotStore, SnapshotStore
from catalyst.verify.company import CHECK, CompanyReport, check_company_named, company_named, known_companies
from catalyst.verify.rules import Outcome
from conftest import FIXTURES, REPO_ROOT

GOLD = REPO_ROOT / "evals" / "extract_catalysts"
VERTEX_RELEASE = (
    FIXTURES / "sec" / "875320" / "Archives_edgar_data_875320_000087532026000256_ex-991_q22026.htm"
)
SPYRE_RELEASE = GOLD / "documents" / "b1c8060030d379c487d6fda3bc3199902418affc1ca18ad3305b7f2618648e65.htm"
MIRUM_RELEASE = GOLD / "documents" / "24272f6faabdc564770f7d3eac45e2893d7fe25c56a04dd2ca232a5f43f66c80.htm"
TG_RELEASE = GOLD / "documents" / "2e997d3283037ccba17242897e11cf8e108ceb7e943b37d416cf5be7c886f1a0.htm"
MODERNA_BOARD = GOLD / "documents" / "bfd65b7ad747ba9206525d6fce26ab8995ce2b54d149b4c7621433f0d0638db5.htm"
TICKERS = {
    "TG THERAPEUTICS, INC.": "TGTX",
    "Aptevo Therapeutics Inc.": "APVO",
    "AEON Biopharma, Inc.": "AEON",
    "Spyre Therapeutics, Inc.": "SYRE",
    "Coherus Oncology, Inc.": "CHRS",
    "Mirum Pharmaceuticals, Inc.": "MIRM",
    "ACADIA PHARMACEUTICALS INC": "ACAD",
    "Moderna, Inc.": "MRNA",
    "IONIS PHARMACEUTICALS INC": "IONS",
    "Kardigan, Inc.": "KARD",
    "VERTEX PHARMACEUTICALS INC / MA": "VRTX",
}
SINCE = date(2020, 1, 1)


def _releases() -> list[tuple[str, Path]]:
    """Every recorded release with the company that issued it."""
    gold = [json.loads(line) for line in (GOLD / "gold.jsonl").read_text().splitlines() if line]
    found = [(g["company"], GOLD / "documents" / f"{g['sha256']}.htm") for g in gold]
    return [*found, ("VERTEX PHARMACEUTICALS INC / MA", VERTEX_RELEASE)]


def _text(path: Path) -> str:
    return document_text(path.read_bytes(), "text/html")


def _company(name: str, *more: str, ticker: str | None = None) -> CompanyRef:
    tickers = ((ticker, SINCE, None),) if ticker else ()
    return CompanyRef(id=uuid4(), cik=1, names=(name, *more), tickers=tickers)


def _every_company() -> list[CompanyRef]:
    return [_company(name, ticker=ticker) for name, ticker in TICKERS.items()]


def _check(
    text: str, company: CompanyRef, others: list[CompanyRef] | None = None
) -> tuple[Outcome, str | None]:
    known = known_companies([company, *(others or [])])
    result = company_named(text, known[company.id])
    assert result.check == CHECK
    return result.outcome, result.reason


@pytest.mark.parametrize(("company", "path"), _releases(), ids=lambda v: str(v)[-20:])
def test_each_release_names_its_own_company_and_no_other(company: str, path: Path) -> None:
    text = _text(path)
    companies = _every_company()
    for candidate in companies:
        outcome, reason = _check(text, candidate, [c for c in companies if c is not candidate])
        if candidate.names[0] == company:
            assert (outcome, reason) == (Outcome.PASS, None)
        else:
            assert outcome is Outcome.FAIL, candidate.names[0]
            assert reason == f"the source does not name {candidate.names[0]} or its ticker"


@pytest.mark.parametrize(
    ("text", "ticker"),
    [
        ("Spyre Therapeutics (NASDAQ: SYRE) announced", "SYRE"),
        ("the company (Nasdaq:SYRE) announced", "SYRE"),
        ("listed as Nasdaq: SYRE since 2023", "SYRE"),
        ("(Nasdaq Global Select Market: SYRE)", "SYRE"),
        ("AEON Biopharma (NYSE American: AEON)", "AEON"),
    ],
)
def test_a_ticker_in_an_exchange_listing_names_the_company(text: str, ticker: str) -> None:
    company = _company("Listed Under Another Name Corp", ticker=ticker)
    assert _check(text, company) == (Outcome.PASS, None)


@pytest.mark.parametrize(
    "text",
    [
        "SYRE shares rose.",  # a bare ticker can be an ordinary word or another symbol
        "(NASDAQ: SYREX)",
        "(NASDAQ: syre)",
        "Nasdaq said SYRE",
    ],
)
def test_a_ticker_outside_an_exchange_listing_does_not_count(text: str) -> None:
    assert _check(text, _company("Listed Under Another Name Corp", ticker="SYRE"))[0] is Outcome.FAIL


def test_the_tg_release_does_not_name_a_company_whose_ticker_is_tg() -> None:
    """The name TG Therapeutics starts with TG, but only "(NASDAQ: TGTX)" is a listing."""
    assert _check(_text(TG_RELEASE), _company("Tee Gee Holdings Corp", ticker="TG"))[0] is Outcome.FAIL


def test_the_spyre_release_names_spyre_by_its_ticker_alone() -> None:
    assert _check(_text(SPYRE_RELEASE), _company("Renamed Holdings Corp", ticker="SYRE"))[0] is Outcome.PASS


def test_a_former_name_counts() -> None:
    company = _company("Brand New Name Corp", "Spyre Therapeutics, Inc.")
    assert _check(_text(SPYRE_RELEASE), company) == (Outcome.PASS, None)


@pytest.mark.parametrize(
    "name", ["SPYRE THERAPEUTICS INC /DE/", "Spyre Therapeutics Inc", "spyre therapeutics, inc."]
)
def test_names_are_compared_as_the_resolver_compares_them(name: str) -> None:
    assert _check(_text(SPYRE_RELEASE), _company(name))[0] is Outcome.PASS


@pytest.mark.parametrize(
    "name",
    ["Spyre Therapeutic Holdings Inc", "Spyre Bio Inc", "Inc."],
    ids=["a longer name", "another name sharing a word", "only the legal form"],
)
def test_part_of_a_name_or_a_different_name_does_not_count(name: str) -> None:
    assert _check(_text(SPYRE_RELEASE), _company(name))[0] is Outcome.FAIL


def test_a_name_another_company_also_goes_by_does_not_count() -> None:
    """Two records share the name, so the name alone cannot say which company the release is from."""
    spyre, twin = _company("Spyre Therapeutics, Inc."), _company("SPYRE THERAPEUTICS INC /DE/")
    assert _check(_text(SPYRE_RELEASE), spyre, [twin])[0] is Outcome.FAIL
    with_ticker = _company("Spyre Therapeutics, Inc.", ticker="SYRE")
    assert _check(_text(SPYRE_RELEASE), with_ticker, [twin])[0] is Outcome.PASS


def test_a_ticker_another_company_has_held_does_not_count() -> None:
    renamed = _company("Renamed Holdings Corp", ticker="SYRE")
    earlier = CompanyRef(
        id=uuid4(), cik=1, names=("Earlier Holder Corp",), tickers=(("SYRE", SINCE, date(2022, 1, 1)),)
    )
    assert _check(_text(SPYRE_RELEASE), renamed, [earlier])[0] is Outcome.FAIL


def test_a_partner_named_in_the_release_still_passes() -> None:
    """The limit of this check: Moderna's board release names Biogen, so a link to Biogen passes."""
    assert _check(_text(MODERNA_BOARD), _company("Biogen Inc."))[0] is Outcome.PASS


# The job: every extractor claim, against the document its claim came from.

PUBLISHED = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 18, 18, 0, tzinfo=UTC)


class _CountingStore:
    def __init__(self, store: SnapshotStore) -> None:
        self.store = store
        self.reads: list[str] = []

    def put(self, content: bytes) -> Any:
        return self.store.put(content)

    def get(self, key: str) -> bytes:
        self.reads.append(key)
        return self.store.get(key)


@pytest.fixture
def store(tmp_path: Path) -> _CountingStore:
    return _CountingStore(LocalSnapshotStore(tmp_path / "snapshots"))


def _company_row(session: Session, cik: int, name: str, ticker: str, *former: str) -> UUID:
    company = upsert_company(session, cik, name, "2834").id
    identity = CompanyIdentity(
        name=name, former_names=tuple((f, None, None) for f in former), tickers=((ticker, "Nasdaq"),)
    )
    record_company_identity(session, company, identity, date(2026, 10, 1))
    return company


def _document(session: Session, store: SnapshotStore, key: str, content: bytes) -> UUID:
    snapshot = store.put(content)
    document, _ = save_source_document(
        session,
        SourceDocument(
            source_key=key,
            url=f"https://www.sec.gov/{key}.htm",
            publisher="SEC EDGAR",
            source_tier=SourceTier.REGULATOR_OR_SEC,
            document_type="8-K EX-99.1",
            published_at=PUBLISHED,
            retrieved_at=PUBLISHED,
            sha256=snapshot.sha256,
            snapshot_key=snapshot.key,
            content_type="text/html",
        ),
    )
    return document.id


def _claim(
    session: Session, document: UUID, field: str = "catalyst:0000000000000001", agent: str = "extractor"
) -> UUID:
    quote = "Topline proof-of-concept data are expected in September 2026."
    claim = Claim(
        source_document_id=document,
        subject_type=ClaimSubject.CATALYST,
        field=field,
        value={"catalyst_type": "topline_readout", "drug": "SPY072", "problems": []},
        quote=quote,
        quote_start=0,
        quote_end=len(quote),
        agent=agent,
        prompt_id="extract_catalysts",
        prompt_version="1.0.0",
        provider="anthropic",
        model="m",
        extracted_at=PUBLISHED,
    )
    session.execute(insert(TABLES[Claim]).values(**claim.model_dump()))
    return claim.id


def _outcomes(session_factory: sessionmaker[Session]) -> dict[UUID, tuple[str, str | None]]:
    table = TABLES[ClaimCheck]
    with session_factory() as session:
        rows = session.execute(select(table).where(table.c.check_name == CHECK)).mappings()
        return {r["claim_id"]: (r["outcome"], r["reason"]) for r in rows}


@pytest.fixture
def companies(session_factory: sessionmaker[Session]) -> dict[str, UUID]:
    with session_factory.begin() as session:
        return {
            "spyre": _company_row(
                session, 1636282, "Spyre Therapeutics, Inc.", "SYRE", "Aeglea BioTherapeutics, Inc."
            ),
            "mirum": _company_row(session, 1759425, "Mirum Pharmaceuticals, Inc.", "MIRM"),
        }


def test_the_job_passes_the_right_company_and_fails_another(
    session_factory: sessionmaker[Session], store: _CountingStore, companies: dict[str, UUID]
) -> None:
    with session_factory.begin() as session:
        right = _document(session, store, "sec:spyre", SPYRE_RELEASE.read_bytes())
        link_company_document(session, companies["spyre"], right, DocumentMatch.SEC_CIK)
        wrong = _document(session, store, "sec:mirum-linked-to-spyre", MIRUM_RELEASE.read_bytes())
        link_company_document(session, companies["spyre"], wrong, DocumentMatch.SEC_CIK)
        good, bad = _claim(session, right), _claim(session, wrong)
    report = check_company_named(session_factory, store, lambda: NOW)
    assert _outcomes(session_factory) == {
        good: ("pass", None),
        bad: ("fail", "the source does not name Spyre Therapeutics, Inc. or its ticker"),
    }
    assert report.summary() == {
        "claims": 2,
        "documents_read": 2,
        "documents_unreadable": 0,
        "outcomes": {f"{CHECK}:pass": 1, f"{CHECK}:fail": 1},
    }


def test_the_job_reads_each_document_once_and_only_extractor_claims(
    session_factory: sessionmaker[Session], store: _CountingStore, companies: dict[str, UUID]
) -> None:
    with session_factory.begin() as session:
        document = _document(session, store, "sec:spyre", SPYRE_RELEASE.read_bytes())
        link_company_document(session, companies["spyre"], document, DocumentMatch.SEC_CIK)
        first = _claim(session, document, "catalyst:0000000000000001")
        second = _claim(session, document, "catalyst:0000000000000002")
        verifier = _claim(session, document, "catalyst:0000000000000003", agent="verifier")
    report = check_company_named(session_factory, store, lambda: NOW)
    assert len(store.reads) == 1
    outcomes = _outcomes(session_factory)
    assert (outcomes[first], outcomes[second]) == (("pass", None), ("pass", None))
    assert verifier not in outcomes
    assert report.claims == 2


@pytest.mark.parametrize("links", [0, 2], ids=["no company", "two companies"])
def test_a_document_without_exactly_one_company_fails_without_being_read(
    session_factory: sessionmaker[Session], store: _CountingStore, companies: dict[str, UUID], links: int
) -> None:
    with session_factory.begin() as session:
        document = _document(session, store, "sec:spyre", SPYRE_RELEASE.read_bytes())
        for company in list(companies.values())[:links]:
            link_company_document(session, company, document, DocumentMatch.SEC_CIK)
        claim = _claim(session, document)
    check_company_named(session_factory, store, lambda: NOW)
    reason = (
        "the source document is linked to no company"
        if links == 0
        else "the source document is linked to 2 companies"
    )
    assert _outcomes(session_factory) == {claim: ("fail", reason)}
    assert store.reads == []


def test_an_unreadable_copy_fails_its_claims_and_the_run_goes_on(
    session_factory: sessionmaker[Session], store: _CountingStore, companies: dict[str, UUID], tmp_path: Path
) -> None:
    elsewhere = LocalSnapshotStore(tmp_path / "elsewhere")  # stored where the job's store cannot see it
    with session_factory.begin() as session:
        missing = _document(session, elsewhere, "sec:missing", MIRUM_RELEASE.read_bytes())
        link_company_document(session, companies["mirum"], missing, DocumentMatch.SEC_CIK)
        readable = _document(session, store, "sec:spyre", SPYRE_RELEASE.read_bytes())
        link_company_document(session, companies["spyre"], readable, DocumentMatch.SEC_CIK)
        lost, kept = _claim(session, missing), _claim(session, readable)
    report = check_company_named(session_factory, store, lambda: NOW)
    assert _outcomes(session_factory) == {
        lost: ("fail", "the stored copy of the source could not be read (SnapshotNotFoundError)"),
        kept: ("pass", None),
    }
    assert (report.documents_read, report.documents_unreadable) == (1, 1)


def test_a_former_name_on_file_counts_and_a_re_run_keeps_one_row(
    session_factory: sessionmaker[Session], store: _CountingStore
) -> None:
    with session_factory.begin() as session:
        renamed = _company_row(session, 1636282, "Brand New Name Corp", "ZZZZ", "Spyre Therapeutics, Inc.")
        document = _document(session, store, "sec:spyre", SPYRE_RELEASE.read_bytes())
        link_company_document(session, renamed, document, DocumentMatch.SEC_CIK)
        claim = _claim(session, document)
    check_company_named(session_factory, store, lambda: NOW)
    check_company_named(session_factory, store, lambda: NOW)
    assert _outcomes(session_factory) == {claim: ("pass", None)}
    with session_factory() as session:
        assert len(session.execute(select(TABLES[ClaimCheck])).all()) == 1


def test_an_empty_database_stores_nothing(
    session_factory: sessionmaker[Session], store: _CountingStore
) -> None:
    assert check_company_named(session_factory, store, lambda: NOW) == CompanyReport()
