"""Run every collector for a list of companies, plus the source-wide feeds.

One failing source is logged as an alert and the run continues, so a single outage never
blocks the other sources.
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
from catalyst.schemas import DocumentMatch
from catalyst.snapshots import LocalSnapshotStore

_log = logging.getLogger(__name__)


class PilotCompany(BaseModel):
    """A company to collect, with the names it is registered under at each source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    cik: int = Field(ge=1)
    ctgov_sponsors: frozenset[str] = frozenset()
    openfda_manufacturer: str | None = None
    openfda_sponsors: frozenset[str] = frozenset()


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

    store: LocalSnapshotStore
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


def collect_all(
    companies: list[PilotCompany],
    clients: Clients,
    store: LocalSnapshotStore,
    session_factory: sessionmaker[Session],
    since: date,
    now: Callable[[], datetime] = _utc_now,
    sec_forms: frozenset[str] = DEFAULT_FORMS,
) -> list[CollectionStats]:
    """Collect every company's SEC filings, trials, and FDA applications, then the source-wide feeds."""
    collector = Collector(store, session_factory, now)
    results: list[CollectionStats] = []

    def already_stored(prefix: str) -> bool:
        with session_factory() as session:
            return bool(known_source_keys(session, prefix))

    for pilot in companies:
        submissions = clients.sec.get(SUBMISSIONS_URL.format(cik=pilot.cik)).content
        cik, name, sic = company_profile(submissions)
        with session_factory.begin() as session:
            company_id = upsert_company(session, cik, name, sic).id
        results.append(
            collector.run(
                f"sec:{pilot.ticker}",
                partial(collect_filings, clients.sec, submissions, since, already_stored, sec_forms),
                (company_id, DocumentMatch.SEC_CIK),
            )
        )
        if pilot.ctgov_sponsors:
            results.append(
                collector.run(
                    f"ctgov:{pilot.ticker}",
                    partial(ctgov.collect_sponsor_studies, clients.web, pilot.ctgov_sponsors),
                    (company_id, DocumentMatch.CTGOV_LEAD_SPONSOR),
                )
            )
        if pilot.openfda_manufacturer:
            manufacturer = pilot.openfda_manufacturer
            results.append(
                collector.run(
                    f"openfda:{pilot.ticker}",
                    partial(openfda.collect_applications, clients.web, manufacturer, pilot.openfda_sponsors),
                    (company_id, DocumentMatch.OPENFDA_SPONSOR),
                )
            )
    results.append(
        collector.run("fedreg", partial(fedreg.collect_advisory_committee_notices, clients.web, since))
    )
    for feed in wires.FEEDS:
        results.append(collector.run(f"wire:{feed.slug}", partial(wires.collect_feed, clients.web, feed)))
    return results
