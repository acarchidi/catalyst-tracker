"""A claim's company must be named in its own source document (spec: Verification; ADR 0018, part 3).

The planted-error test linked claims to the wrong company, and no other check noticed. The
company is the one the claim's document is linked to, as the resolver takes it. The check
`company_named_in_source` passes when the document's own text names that company:
- by one of its names, current or former, compared after the resolver's normalization (case,
  punctuation, legal-form words) as a run of whole words; or
- by one of its tickers in an exchange listing, such as "(NASDAQ: SYRE)" or "Nasdaq: SYRE".

A name that another company on file also goes by, or a ticker another company has held, does not
count: it cannot say which company the document is from. An SEC filing linked by the filer's own
number is held to the same rule, since its press release names the company. A document linked to
no company or to several, or whose stored copy cannot be read, fails with the reason.

The check cannot tell the issuer from another company the document also names: Moderna's release
that names Biogen passes for Biogen too.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.coverage import name_words, normalize_name
from catalyst.db.tables import TABLES
from catalyst.extract.agent import ROLE
from catalyst.extract.text import TEXT_VERSION, document_text
from catalyst.logs import log_event
from catalyst.references import company_refs
from catalyst.resolve import CompanyRef
from catalyst.schemas import Claim, CompanyDocument, SourceDocument
from catalyst.snapshots import SnapshotStore, SnapshotStoreError
from catalyst.verify.incremental import (
    Found,
    incremental_counts,
    input_digest,
    load_stamps,
    unchanged,
    write_checks,
)
from catalyst.verify.rules import CheckResult, Outcome

CHECK = "company_named_in_source"

_log = logging.getLogger(__name__)
_EXCHANGES = "NASDAQ|NYSE|AMEX|OTC|OTCQB|OTCQX|OTC Markets|TSX|TSXV|Cboe"
# "(NASDAQ: SYRE)", "Nasdaq:SYRE", "NYSE American: AEON", "Nasdaq Global Select Market: SYRE".
_LISTING = re.compile(
    rf"(?<![^\W_])(?i:{_EXCHANGES})(?:\s+[A-Z][A-Za-z]*){{0,3}}\s*:\s*"
    r"(?P<ticker>[A-Z][A-Z0-9]*(?:[.\-][A-Z0-9]+)?)(?![A-Za-z0-9])"
)


@dataclass(frozen=True)
class KnownCompany:
    """The names and tickers that point to one company and to no other company on file."""

    name: str  # the current name, for reasons
    names: frozenset[str]  # normalized, as `normalize_name` gives them
    tickers: frozenset[str]


def known_companies(companies: Iterable[CompanyRef]) -> dict[UUID, KnownCompany]:
    """Keep, for each company, only the names and tickers no other company shares."""
    listed = list(companies)
    names = {c.id: {key for name in c.names if (key := normalize_name(name))} for c in listed}
    tickers = {c.id: {ticker.upper() for ticker, _, _ in c.tickers} for c in listed}
    name_owners = Counter(key for keys in names.values() for key in keys)
    ticker_owners = Counter(ticker for held in tickers.values() for ticker in held)
    return {
        c.id: KnownCompany(
            name=c.names[0] if c.names else f"the company with SEC number {c.cik}",
            names=frozenset(key for key in names[c.id] if name_owners[key] == 1),
            tickers=frozenset(ticker for ticker in tickers[c.id] if ticker_owners[ticker] == 1),
        )
        for c in listed
    }


def company_named(text: str, company: KnownCompany) -> CheckResult:
    """Pass when the text names the company by a name or a listed ticker of its own."""
    words = f" {name_words(text)} "
    if any(f" {key} " in words for key in company.names):
        return CheckResult(CHECK, Outcome.PASS)
    if {m.group("ticker").upper() for m in _LISTING.finditer(text)} & company.tickers:
        return CheckResult(CHECK, Outcome.PASS)
    return CheckResult(CHECK, Outcome.FAIL, f"the source does not name {company.name} or its ticker")


@dataclass
class CompanyReport:
    """What one run found."""

    claims: int = 0
    documents_read: int = 0
    documents_unreadable: int = 0
    outcomes: dict[str, int] = field(default_factory=dict[str, int])
    claims_unchanged: int = 0  # skipped: same code, same inputs as their stored row (ADR 0064)
    stopped_for_time: bool = False  # the run's time ran out; the next run checks the rest

    def summary(self) -> dict[str, Any]:
        """Return the counts as plain JSON."""
        return {
            "claims": self.claims,
            "documents_read": self.documents_read,
            "documents_unreadable": self.documents_unreadable,
            "outcomes": self.outcomes,
        } | incremental_counts(self.claims_unchanged, self.stopped_for_time)


def _failed(reason: str) -> CheckResult:
    return CheckResult(CHECK, Outcome.FAIL, reason)


def _decide(
    store: SnapshotStore,
    snapshot_key: str,
    content_type: str,
    owners: set[UUID],
    known: dict[UUID, KnownCompany],
    report: CompanyReport,
) -> tuple[CheckResult, bool]:
    """Decide for one document; its copy is read only when it has exactly one company.

    Also says whether the outcome may stand while the inputs are unchanged: not when the copy
    could not be read, so that copy is tried again next run (ADR 0064).
    """
    if not owners:
        return _failed("the source document is linked to no company"), True
    if len(owners) > 1:
        return _failed(f"the source document is linked to {len(owners)} companies"), True
    (company,) = owners
    try:
        text = document_text(store.get(snapshot_key), content_type)
    except (SnapshotStoreError, httpx.HTTPError, OSError, ValueError) as error:
        report.documents_unreadable += 1
        log_event(_log, "company_check_unreadable", snapshot_key=snapshot_key, error=type(error).__name__)
        return _failed(f"the stored copy of the source could not be read ({type(error).__name__})"), False
    report.documents_read += 1
    return company_named(text, known[company]), True


def _inputs(row: Any, owners: set[UUID], known: dict[UUID, KnownCompany]) -> str:
    """Return the digest of what the check reads for one claim: its copy, its links, its company."""
    company = known.get(next(iter(owners))) if len(owners) == 1 else None
    return input_digest(row.sha256, row.content_type, TEXT_VERSION, frozenset(owners), company)


def check_company_named(
    session_factory: sessionmaker[Session],
    store: SnapshotStore,
    now: Callable[[], datetime],
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CompanyReport:
    """Store `company_named_in_source` for every new or changed extractor claim; safe to re-run.

    Each document is read at most once per run, however many claims came from it, and not at
    all while none of its claims changed (ADR 0064). Past `deadline` (a `clock` reading) no
    further claim is checked: the run stops cleanly and the next one checks the rest.
    """
    claim, document, links = TABLES[Claim], TABLES[SourceDocument], TABLES[CompanyDocument]
    report = CompanyReport()
    at = now()
    with session_factory.begin() as session:
        owners: dict[UUID, set[UUID]] = {}
        for row in session.execute(select(links.c.source_document_id, links.c.company_id)):
            owners.setdefault(row.source_document_id, set()).add(row.company_id)
        known = known_companies(company_refs(session))
        stamps = load_stamps(session, [CHECK])
        query = (
            select(
                claim.c.id,
                claim.c.source_document_id,
                document.c.snapshot_key,
                document.c.sha256,
                document.c.content_type,
            )
            .join(document, document.c.id == claim.c.source_document_id)
            .where(claim.c.agent == ROLE, claim.c.field.startswith("catalyst:"))
            .order_by(claim.c.extracted_at, claim.c.id)
        )
        decided: dict[UUID, tuple[CheckResult, bool]] = {}
        found: list[Found] = []
        for row in session.execute(query).all():
            mine = owners.get(row.source_document_id, set())
            digest = _inputs(row, mine, known)
            stamp = stamps.get((row.id, CHECK))
            if stamp is not None and unchanged(stamp, CHECK, digest):
                report.claims += 1
                report.claims_unchanged += 1
                key = f"{CHECK}:{stamp.outcome}"
                report.outcomes[key] = report.outcomes.get(key, 0) + 1
                continue
            if deadline is not None and clock() > deadline:
                report.stopped_for_time = True
                break
            if row.source_document_id not in decided:
                decided[row.source_document_id] = _decide(
                    store, row.snapshot_key, row.content_type, mine, known, report
                )
            result, keep = decided[row.source_document_id]
            found.append(Found(row.id, result, digest if keep else None))
            report.claims += 1
            key = f"{result.check}:{result.outcome.value}"
            report.outcomes[key] = report.outcomes.get(key, 0) + 1
        write_checks(session, found, at, stamps)
    log_event(_log, "company_named_checked", **report.summary())
    return report
