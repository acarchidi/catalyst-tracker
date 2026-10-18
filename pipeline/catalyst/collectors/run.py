"""Run every collector for a list of companies, plus the source-wide feeds.

One failing source is logged as an alert and the run continues, so a single outage never
blocks the other sources. A run can be limited to some sources, by the names in `SOURCES`.
"""

import logging
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.documents import CollectionStats, FetchedDocument, store_fetched
from catalyst.collectors.http import HostPolicy, PoliteHttpClient, shared_limiter
from catalyst.collectors.ratelimit import Limiter, NoWait
from catalyst.collectors.sec.edgar import SEC_LIMITER, SUBMISSIONS_URL, EdgarClient, company_profile
from catalyst.collectors.sec.filings import DEFAULT_FORMS, collect_filings
from catalyst.collectors.sources import ctgov, fedreg, openfda, wires
from catalyst.db.repository import known_source_keys, upsert_company
from catalyst.logs import log_alert, log_event
from catalyst.references import company_identity, record_company_identity, recording_trials
from catalyst.schemas import DocumentMatch
from catalyst.snapshots import SnapshotStore

_log = logging.getLogger(__name__)
# Every source a run collects, by the name that starts its counts: "sec:VRTX", "fedreg", "wire:...".
SOURCES = ("sec", "ctgov", "openfda", "fedreg", "wire")
_PER_COMPANY = frozenset({"sec", "ctgov", "openfda"})


class PilotCompany(BaseModel):
    """A company to collect, with the names it is registered under at each source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    cik: int = Field(ge=1)
    ctgov_sponsors: frozenset[str] = frozenset()
    openfda_manufacturer: str | None = None
    openfda_sponsors: frozenset[str] = frozenset()
    # Written by `catalyst coverage-build` for people reading the list; collectors ignore them.
    name: str | None = None
    sic: int | None = None
    exchange: str | None = None
    ctgov_trials: int | None = Field(default=None, ge=0)


def load_companies(path: Path) -> list[PilotCompany]:
    """Read a company list such as config/pilot_companies.toml."""
    with path.open("rb") as handle:
        return [PilotCompany.model_validate(entry) for entry in tomllib.load(handle)["company"]]


@dataclass(frozen=True)
class Clients:
    """HTTP clients for every source."""

    sec: EdgarClient
    web: PoliteHttpClient


def build_clients(
    contact_email: str, transport: httpx.BaseTransport | None = None, rate_limits: bool = True
) -> Clients:
    """Create the clients, with one shared rate limiter per host.

    `rate_limits=False` is only for tests that replay recorded responses.
    """

    def limiter(host: str, per_second: float) -> Limiter:
        return shared_limiter(host, per_second) if rate_limits else NoWait()

    hosts = {
        "clinicaltrials.gov": HostPolicy(limiter("clinicaltrials.gov", ctgov.REQUESTS_PER_SECOND)),
        "api.fda.gov": HostPolicy(limiter("api.fda.gov", openfda.REQUESTS_PER_SECOND)),
        "www.federalregister.gov": HostPolicy(limiter("www.federalregister.gov", fedreg.REQUESTS_PER_SECOND)),
        "www.prnewswire.com": HostPolicy(
            limiter("www.prnewswire.com", wires.REQUESTS_PER_SECOND), check_robots=True
        ),
    }
    sec = EdgarClient(contact_email, transport, SEC_LIMITER if rate_limits else NoWait())
    return Clients(sec=sec, web=PoliteHttpClient(contact_email, hosts, transport))


@dataclass
class Collector:
    """Stores what collectors yield and keeps per-source counts."""

    store: SnapshotStore
    session_factory: sessionmaker[Session]
    now: Callable[[], datetime]

    def run(
        self,
        source: str,
        documents: Callable[[], Iterable[FetchedDocument]],
        company: tuple[UUID, DocumentMatch] | None = None,
    ) -> CollectionStats:
        """Store every document from `documents`; on a source failure, alert and return what was stored."""
        stats = CollectionStats(source)
        try:
            for fetched in documents():
                saved, created = store_fetched(fetched, self.store, self.session_factory, company, self.now)
                stats.add(saved, created)
        except Exception as error:
            stats.error = f"{type(error).__name__}: {error}"
            log_alert(_log, "collector_failed", source=source, error=stats.error)
        log_event(_log, "collector_finished", source=source, created=stats.created, existing=stats.existing)
        return stats


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _record_names(
    session_factory: sessionmaker[Session], company_id: UUID, submissions: bytes, today: date, ticker: str
) -> CollectionStats | None:
    """Record the company's names and tickers; on failure, alert and return the failed step."""
    try:
        with session_factory.begin() as session:
            record_company_identity(session, company_id, company_identity(submissions), today)
    except Exception as error:
        stats = CollectionStats(f"names:{ticker}", error=f"{type(error).__name__}: {error}")
        log_alert(_log, "collector_failed", source=stats.source, error=stats.error)
        return stats
    return None


def _company_record(
    sec: EdgarClient, session_factory: sessionmaker[Session], cik: int, ticker: str
) -> tuple[UUID, bytes] | CollectionStats:
    """Read the company's SEC record and store the company; on failure, alert and return the failed step."""
    try:
        submissions = sec.get(SUBMISSIONS_URL.format(cik=cik)).content
        found_cik, name, sic = company_profile(submissions)
        with session_factory.begin() as session:
            return upsert_company(session, found_cik, name, sic).id, submissions
    except Exception as error:
        stats = CollectionStats(f"sec:{ticker}", error=f"{type(error).__name__}: {error}")
        log_alert(_log, "collector_failed", source=stats.source, error=stats.error)
        return stats


def collect_all(
    companies: list[PilotCompany],
    clients: Clients,
    store: SnapshotStore,
    session_factory: sessionmaker[Session],
    since: date,
    now: Callable[[], datetime] = _utc_now,
    sec_forms: frozenset[str] = DEFAULT_FORMS,
    sources: frozenset[str] = frozenset(SOURCES),
) -> list[CollectionStats]:
    """Collect every company's SEC filings, trials, and FDA applications, then the source-wide feeds.

    Only the collectors named in `sources` run. Any per-company source still reads the company's
    SEC record, which names the company; with none of them, no company is read at all.
    """
    if unknown := sources - set(SOURCES):
        raise ValueError(f"unknown sources: {', '.join(sorted(unknown))}")
    collector = Collector(store, session_factory, now)
    results: list[CollectionStats] = []

    def already_stored(prefix: str) -> bool:
        with session_factory() as session:
            return bool(known_source_keys(session, prefix))

    for pilot in companies if sources & _PER_COMPANY else ():
        found = _company_record(clients.sec, session_factory, pilot.cik, pilot.ticker)
        if isinstance(found, CollectionStats):
            results.append(found)  # this company's sources cannot run; the next company's still do
            continue
        company_id, submissions = found
        names_failed = _record_names(session_factory, company_id, submissions, now().date(), pilot.ticker)
        if names_failed:
            results.append(names_failed)
        if "sec" in sources:
            results.append(
                collector.run(
                    f"sec:{pilot.ticker}",
                    partial(collect_filings, clients.sec, submissions, since, already_stored, sec_forms),
                    (company_id, DocumentMatch.SEC_CIK),
                )
            )
        if "ctgov" in sources and pilot.ctgov_sponsors:
            results.append(
                collector.run(
                    f"ctgov:{pilot.ticker}",
                    partial(
                        recording_trials,
                        session_factory,
                        partial(ctgov.collect_sponsor_studies, clients.web, pilot.ctgov_sponsors),
                    ),
                    (company_id, DocumentMatch.CTGOV_LEAD_SPONSOR),
                )
            )
        if "openfda" in sources and pilot.openfda_manufacturer:
            manufacturer = pilot.openfda_manufacturer
            results.append(
                collector.run(
                    f"openfda:{pilot.ticker}",
                    partial(openfda.collect_applications, clients.web, manufacturer, pilot.openfda_sponsors),
                    (company_id, DocumentMatch.OPENFDA_SPONSOR),
                )
            )
    return results + _feeds(collector, clients.web, since, sources)


def _feeds(
    collector: Collector, web: PoliteHttpClient, since: date, sources: frozenset[str]
) -> list[CollectionStats]:
    """Collect the source-wide feeds named in `sources`: Federal Register notices, then the wires."""
    results: list[CollectionStats] = []
    if "fedreg" in sources:
        results.append(
            collector.run("fedreg", partial(fedreg.collect_advisory_committee_notices, web, since))
        )
    for feed in wires.FEEDS if "wire" in sources else ():
        results.append(collector.run(f"wire:{feed.slug}", partial(wires.collect_feed, web, feed)))
    return results
