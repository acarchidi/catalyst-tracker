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
from catalyst.extract.text import document_text
from catalyst.logs import log_event
from catalyst.references import company_refs
from catalyst.resolve import CompanyRef
from catalyst.schemas import Claim, CompanyDocument, SourceDocument
from catalyst.snapshots import SnapshotStore, SnapshotStoreError
from catalyst.verify.rules import CheckResult, Outcome
from catalyst.verify.run import store_check

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

    def summary(self) -> dict[str, Any]:
        """Return the counts as plain JSON."""
        return {
            "claims": self.claims,
            "documents_read": self.documents_read,
            "documents_unreadable": self.documents_unreadable,
            "outcomes": self.outcomes,
        }


def _failed(reason: str) -> CheckResult:
    return CheckResult(CHECK, Outcome.FAIL, reason)


def _decide(
    store: SnapshotStore,
    snapshot_key: str,
    content_type: str,
    owners: set[UUID],
    known: dict[UUID, KnownCompany],
    report: CompanyReport,
) -> CheckResult:
    """Decide for one document; its copy is read only when it has exactly one company."""
    if not owners:
        return _failed("the source document is linked to no company")
    if len(owners) > 1:
        return _failed(f"the source document is linked to {len(owners)} companies")
    (company,) = owners
    try:
        text = document_text(store.get(snapshot_key), content_type)
    except (SnapshotStoreError, httpx.HTTPError, ValueError) as error:
        report.documents_unreadable += 1
        log_event(_log, "company_check_unreadable", snapshot_key=snapshot_key, error=type(error).__name__)
        return _failed(f"the stored copy of the source could not be read ({type(error).__name__})")
    report.documents_read += 1
    return company_named(text, known[company])


def check_company_named(
    session_factory: sessionmaker[Session], store: SnapshotStore, now: Callable[[], datetime]
) -> CompanyReport:
    """Store `company_named_in_source` for every extractor claim; safe to re-run.

    Each document is read once per run, however many claims came from it.
    """
    claim, document, links = TABLES[Claim], TABLES[SourceDocument], TABLES[CompanyDocument]
    report = CompanyReport()
    at = now()
    with session_factory.begin() as session:
        owners: dict[UUID, set[UUID]] = {}
        for row in session.execute(select(links.c.source_document_id, links.c.company_id)):
            owners.setdefault(row.source_document_id, set()).add(row.company_id)
        known = known_companies(company_refs(session))
        query = (
            select(claim.c.id, claim.c.source_document_id, document.c.snapshot_key, document.c.content_type)
            .join(document, document.c.id == claim.c.source_document_id)
            .where(claim.c.agent == ROLE, claim.c.field.startswith("catalyst:"))
            .order_by(claim.c.extracted_at, claim.c.id)
        )
        decided: dict[UUID, CheckResult] = {}
        for row in session.execute(query).all():
            if row.source_document_id not in decided:
                decided[row.source_document_id] = _decide(
                    store,
                    row.snapshot_key,
                    row.content_type,
                    owners.get(row.source_document_id, set()),
                    known,
                    report,
                )
            result = decided[row.source_document_id]
            store_check(session, row.id, result, at)
            report.claims += 1
            key = f"{result.check}:{result.outcome.value}"
            report.outcomes[key] = report.outcomes.get(key, 0) + 1
    log_event(_log, "company_named_checked", **report.summary())
    return report
