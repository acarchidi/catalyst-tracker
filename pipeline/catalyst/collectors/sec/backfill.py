"""Backfill of older press releases: 8-K Exhibit 99.1 filed since 2025 (ADR 0065).

The weekday collection reads only each company's last 10 days. This job reads every covered
company's 8-K filings from `BACKFILL_SINCE` up to that window, and stores the Exhibit 99.1 press
releases not yet stored, for the extractor to read newest first. It makes no model call.

It is safe to stop and re-run: a filing already stored is skipped by its accession number without
a request, an 8-K whose items list no exhibits (item 9.01) is skipped without a request, and the
run starts no new company after its time limit. EDGAR's own rate limit applies (`EdgarClient`).
When a company's recent filing list does not reach back to `BACKFILL_SINCE`, the older pages of
the list that overlap the period are read too.
"""

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Any

import httpx
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.documents import CollectionStats, FetchedDocument
from catalyst.collectors.run import Collector, PilotCompany
from catalyst.collectors.sec.edgar import (
    SUBMISSIONS_URL,
    EdgarClient,
    ExhibitRef,
    FilingRef,
    company_profile,
    list_documents,
)
from catalyst.collectors.sec.filings import PUBLISHER, source_key
from catalyst.db.repository import known_source_keys, upsert_company
from catalyst.logs import log_alert, log_event
from catalyst.schemas import DocumentMatch, SourceTier
from catalyst.snapshots import SnapshotStore

# The period the owner asked for (2026-11-23): press releases from 2025 and 2026.
BACKFILL_SINCE = date(2025, 1, 1)
# The promotional credit that pays for reading them expires after this day (ADR 0065).
BACKFILL_LAST_DAY = date(2026, 12, 8)
# Newer filings are the weekday collection's (ADR 0051), which also stores their other exhibits.
LIVE_WINDOW_DAYS = 10
FORM = "8-K"
EXHIBIT = "EX-99.1"  # the only exhibit the extractor reads (extract/run.py, PRESS_RELEASE_TYPES)
DOCUMENT_TYPE = f"{FORM} {EXHIBIT}"
OLDER_PAGE_URL = "https://data.sec.gov/submissions/{name}"
EXHIBITS_ITEM = "9.01"  # "Financial Statements and Exhibits": an 8-K without it attaches no exhibit

_log = logging.getLogger(__name__)


class _Columns(BaseModel):
    """One page of a company's filing list: the "recent" block, or one of the older pages."""

    accession_number: list[str] = Field(alias="accessionNumber")
    filing_date: list[date] = Field(alias="filingDate")
    acceptance_date_time: list[AwareDatetime] = Field(alias="acceptanceDateTime")
    form: list[str]
    primary_document: list[str] = Field(alias="primaryDocument")
    items: list[str] | None = None


class _OlderPage(BaseModel):
    name: str
    filing_from: date = Field(alias="filingFrom")
    filing_to: date = Field(alias="filingTo")


class _Filings(BaseModel):
    recent: _Columns
    files: list[_OlderPage] = []


class _Submissions(BaseModel):
    cik: int
    filings: _Filings


@dataclass(frozen=True)
class Filing:
    """An 8-K from the filing list, with the items it reports ("" when the list gives none)."""

    ref: FilingRef
    items: str

    @property
    def may_have_exhibits(self) -> bool:
        """False only when the list names the filing's items and none is 9.01."""
        listed = [item.strip() for item in self.items.split(",") if item.strip()]
        return not listed or EXHIBITS_ITEM in listed


def _filings(cik: int, columns: _Columns) -> list[Filing]:
    items = columns.items if columns.items is not None else [""] * len(columns.form)
    rows = zip(
        columns.accession_number,
        columns.filing_date,
        columns.acceptance_date_time,
        columns.form,
        columns.primary_document,
        items,
        strict=True,
    )
    return [
        Filing(
            FilingRef(
                cik=cik,
                accession_number=accession,
                form=form,
                filed_on=filed_on,
                accepted_at=accepted_at,
                primary_document=primary,
            ),
            listed,
        )
        for accession, filed_on, accepted_at, form, primary, listed in rows
        if form == FORM
    ]


def press_release_filings(
    client: EdgarClient, submissions_json: bytes, since: date, before: date
) -> list[Filing]:
    """Return the company's 8-K filings filed on or after `since` and before `before`, newest first.

    Reads the older pages of the filing list only when the recent block does not reach `since`,
    and then only the pages whose filings overlap the period.
    """
    data = _Submissions.model_validate_json(submissions_json)
    recent = data.filings.recent
    filings = _filings(data.cik, recent)
    if not recent.filing_date or min(recent.filing_date) > since:
        for page in data.filings.files:
            if page.filing_to >= since and page.filing_from < before:
                content = client.get(OLDER_PAGE_URL.format(name=page.name)).content
                filings += _filings(data.cik, _Columns.model_validate_json(content))
    unique = {filing.ref.accession_number: filing for filing in filings}  # pages never overlap; be safe
    chosen = [f for f in unique.values() if since <= f.ref.filed_on < before]
    return sorted(chosen, key=lambda filing: filing.ref.accepted_at, reverse=True)


def press_releases(documents: list[ExhibitRef]) -> list[ExhibitRef]:
    """Keep only the filing's Exhibit 99.1: the press release the extractor reads."""
    return [document for document in documents if document.type == EXHIBIT]


def collect_press_releases(
    client: EdgarClient,
    submissions_json: bytes,
    since: date,
    before: date,
    already_stored: Callable[[str], bool],
    not_found: Callable[[str], None] = lambda _url: None,
) -> Iterator[FetchedDocument]:
    """Yield the Exhibit 99.1 of each 8-K in the period whose filing is not stored yet.

    A filing whose index page or press release EDGAR answers 404 for is passed to `not_found` and
    skipped; the company's other filings are still read, and the next run tries it again. Any
    other error ends the company's collection, as before.
    """
    for filing in press_release_filings(client, submissions_json, since, before):
        ref = filing.ref
        if not filing.may_have_exhibits or already_stored(f"sec:{ref.accession_number}:"):
            continue
        try:
            fetched = _fetch_filing(client, ref)
        except _NotFoundError as missing:
            not_found(missing.url)
            continue
        yield from fetched


class _NotFoundError(Exception):
    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.url = url


def _get(client: EdgarClient, url: str) -> httpx.Response:
    try:
        return client.get(url)
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:  # Not Found
            raise _NotFoundError(url) from error
        raise


def _fetch_filing(client: EdgarClient, ref: FilingRef) -> list[FetchedDocument]:
    """Fetch every kept document of one filing before storing any, so a failure part-way stores nothing."""
    fetched: list[FetchedDocument] = []
    for document in press_releases(list_documents(_get(client, ref.index_url).text)):
        response = _get(client, document.url)
        fetched.append(
            FetchedDocument(
                source_key=source_key(ref, document),
                url=document.url,
                publisher=PUBLISHER,
                source_tier=SourceTier.REGULATOR_OR_SEC,
                document_type=DOCUMENT_TYPE,
                published_at=ref.accepted_at,
                content=response.content,
                content_type=response.headers.get("content-type", "application/octet-stream").split(";")[0],
            )
        )
    return fetched


def backfill_window(today: date) -> tuple[date, date] | None:
    """Return the filing dates to backfill on `today` (from, before), or None once it has ended."""
    if today > BACKFILL_LAST_DAY:
        return None
    return BACKFILL_SINCE, today - timedelta(days=LIVE_WINDOW_DAYS)


@dataclass
class BackfillReport:
    """What one collection run did."""

    active: bool
    reason: str | None = None
    since: date | None = None
    before: date | None = None
    companies_done: int = 0
    companies_left: int = 0
    sources: list[CollectionStats] = field(default_factory=list[CollectionStats])
    not_found: list[str] = field(default_factory=list[str])  # filings EDGAR answered 404 for

    def summary(self) -> dict[str, Any]:
        """Return the counts the owner reads in the job summary."""
        return {
            "active": self.active,
            "reason": self.reason,
            "since": self.since.isoformat() if self.since else None,
            "before": self.before.isoformat() if self.before else None,
            "companies_done": self.companies_done,
            "companies_left": self.companies_left,
            "press_releases_stored": sum(s.created for s in self.sources),
            "filings_not_found": len(self.not_found),
            "not_found": self.not_found,
            "sources": [
                {"source": s.source, "created": s.created, "existing": s.existing, "error": s.error}
                for s in self.sources
            ],
        }

    @property
    def failed(self) -> bool:
        """Whether any company could not be collected."""
        return any(s.error for s in self.sources)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _company(
    client: EdgarClient, session_factory: sessionmaker[Session], pilot: PilotCompany
) -> tuple[Any, bytes] | CollectionStats:
    """Read the company's filing list and store the company; on failure, alert and say so."""
    try:
        submissions = client.get(SUBMISSIONS_URL.format(cik=pilot.cik)).content
        cik, name, sic = company_profile(submissions)
        with session_factory.begin() as session:
            return upsert_company(session, cik, name, sic).id, submissions
    except Exception as error:
        stats = CollectionStats(f"sec-backfill:{pilot.ticker}", error=f"{type(error).__name__}: {error}")
        log_alert(_log, "collector_failed", source=stats.source, error=stats.error)
        return stats


def backfill_press_releases(
    companies: list[PilotCompany],
    client: EdgarClient,
    store: SnapshotStore,
    session_factory: sessionmaker[Session],
    *,
    today: date,
    max_minutes: float | None = None,
    now: Callable[[], datetime] = _utc_now,
    since: date = BACKFILL_SINCE,
) -> BackfillReport:
    """Store the press releases of `companies` in the backfill window; nothing once it has ended.

    `since` is for tests on recorded responses; the job always starts at `BACKFILL_SINCE`.
    """
    window = backfill_window(today)
    if window is None:
        return BackfillReport(active=False, reason=f"the backfill ended on {BACKFILL_LAST_DAY.isoformat()}")
    before = window[1]
    report = BackfillReport(active=True, since=since, before=before)
    collector = Collector(store, session_factory, now)
    deadline = now() + timedelta(minutes=max_minutes) if max_minutes is not None else None

    def report_missing(url: str) -> None:
        report.not_found.append(url)
        log_event(_log, "filing_not_found", url=url)

    def already_stored(prefix: str) -> bool:
        with session_factory() as session:
            return bool(known_source_keys(session, prefix))

    for position, pilot in enumerate(companies):
        if deadline is not None and now() >= deadline:
            report.companies_left = len(companies) - position
            report.reason = "time limit reached; the next run carries on"
            break
        found = _company(client, session_factory, pilot)
        if isinstance(found, CollectionStats):
            report.sources.append(found)
            continue
        company_id, submissions = found
        documents = partial(
            collect_press_releases, client, submissions, since, before, already_stored, report_missing
        )
        report.sources.append(
            collector.run(f"sec-backfill:{pilot.ticker}", documents, (company_id, DocumentMatch.SEC_CIK))
        )
        report.companies_done += 1
    counts = {"sources": len(report.sources), "not_found": len(report.not_found)}
    log_event(_log, "backfill_collected", **report.summary() | counts)
    return report
