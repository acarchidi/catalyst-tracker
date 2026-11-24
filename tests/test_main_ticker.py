"""The main ticker of a company SEC lists with several (ADR 0070), on SEC's recorded ticker file."""

import json
from datetime import date

from sqlalchemy import insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.api.read import company_views
from catalyst.collectors.coverage import listed_companies
from catalyst.db.tables import TABLES
from catalyst.references import CompanyIdentity, record_company_identity
from catalyst.schemas import Company
from catalyst.tickers import main_ticker
from conftest import FIXTURES

TICKERS = FIXTURES / "coverage-8731" / "00_www.sec.gov_files_company_tickers_exchange.json"
KIORA, LIGAND = 1372514, 886163


def _listed_by_sec(cik: int) -> list[tuple[str, str | None]]:
    """Every (ticker, exchange) SEC's file lists for the company, in its order, OTC lines first."""
    data = json.loads(TICKERS.read_bytes())
    rows = [(row[2], row[3]) for row in data["data"] if row[0] == cik]
    return sorted(rows, key=lambda row: row[1] != "OTC")  # the worst order for the rule


def test_an_exchange_listed_share_wins_over_otc_warrants_and_rights() -> None:
    assert ("KPHMW", "OTC") in _listed_by_sec(KIORA)
    assert main_ticker(_listed_by_sec(KIORA)) == "KPRX"
    assert ("LGNZZ", "OTC") in _listed_by_sec(LIGAND)
    assert main_ticker(_listed_by_sec(LIGAND)) == "LGND"


def test_suffixes_lose_to_the_plain_shares_and_ties_keep_seconds_order() -> None:
    assert main_ticker([("ACHR-WT", "NYSE"), ("ACHR", "NYSE")]) == "ACHR"
    assert main_ticker([("ABCDW", "Nasdaq"), ("ABCDU", "Nasdaq"), ("ABCD", "Nasdaq")]) == "ABCD"
    assert main_ticker([("XYZF", "OTC"), ("XYZ", "CBOE")]) == "XYZ"
    assert main_ticker([("TXA", "TXSE"), ("TXAW", "OTC")]) == "TXA"
    assert main_ticker([("GOOGL", "Nasdaq"), ("GOOG", "Nasdaq")]) == "GOOGL"
    assert main_ticker([("OTCQ", "OTC")]) == "OTCQ"  # only over the counter: it is all there is


def test_the_coverage_build_takes_the_main_ticker() -> None:
    listings = listed_companies(TICKERS.read_bytes())
    assert (listings[KIORA].ticker, listings[LIGAND].ticker) == ("KPRX", "LGND")


def test_the_read_layer_shows_the_main_ticker_of_the_stored_ones(
    session_factory: sessionmaker[Session],
) -> None:
    kiora, ligand = Company(cik=KIORA, name="Kiora"), Company(cik=LIGAND, name="Ligand")
    with session_factory.begin() as session:
        for company in (kiora, ligand):
            session.execute(insert(TABLES[Company]).values(**company.model_dump()))
            identity = CompanyIdentity(
                name=company.name, former_names=(), tickers=tuple(_listed_by_sec(company.cik))
            )
            record_company_identity(session, company.id, identity, date(2026, 11, 24))
    for _ in range(2):  # the same answer on every read: nothing stored needs correcting
        with session_factory() as session:
            views = company_views(session, [kiora.id, ligand.id])
        assert (views[kiora.id].ticker, views[ligand.id].ticker) == ("KPRX", "LGND")
