"""Independent source: a second document states the same event, or the source is a regulator (ADR 0018).

The publish rule asks that "either a second source agrees or the source is a regulator or SEC
filing" (spec: Verification). Another claim agrees only when it comes from another source and
passed every rule check itself; it is linked to the same company and gives the same type, a
window inside the claim's window, and the same drug. A missing type, window, or drug never
agrees, except that a deal need not name a drug.

Another source means another address (ignoring case, "www.", the scheme, a query, a fragment,
and a trailing slash), another file, and other words: a copy of one release posted twice shares
its mistakes, so it is one source (Opus review of PR #46).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlsplit
from uuid import UUID

from catalyst.schemas import SourceTier
from catalyst.verify.rules import CheckResult, Outcome
from catalyst.verify.second import name_key

CHECK = "independent_source"
_NO_DRUG_TYPE = "merger_acquisition"


def address(url: str) -> str:
    """Return a page's address with the parts that do not change the page removed."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    return host + parts.path.rstrip("/")


@dataclass(frozen=True)
class Statement:
    """One extractor claim with what this check compares: its source, companies, and event."""

    claim_id: UUID
    url: str
    sha256: str  # the stored file's hash; one file posted twice is one source
    quote: str
    tier: SourceTier
    companies: frozenset[UUID]
    catalyst_type: str | None
    drug: str | None
    drug_ids: frozenset[UUID]  # the drug records the resolver matched the claim's drug name to
    window: tuple[date, date] | None
    clean: bool  # the extractor flagged nothing and every rule check passed
    page: str = field(init=False)
    words: str = field(init=False)
    drug_key: str = field(init=False)

    def __post_init__(self) -> None:
        """Work out the keys compared below once, not for every pair."""
        object.__setattr__(self, "page", address(self.url))
        object.__setattr__(self, "words", name_key(self.quote))
        object.__setattr__(self, "drug_key", name_key(self.drug))


def _same_source(a: Statement, b: Statement) -> bool:
    return a.page == b.page or a.sha256 == b.sha256 or a.words == b.words


def _drugs_agree(a: Statement, b: Statement) -> bool:
    if not a.drug_key and not b.drug_key:
        return a.catalyst_type == b.catalyst_type == _NO_DRUG_TYPE
    if not a.drug_key or not b.drug_key:
        return False
    return a.drug_key == b.drug_key or bool(a.drug_ids & b.drug_ids)


def _inside(outer: tuple[date, date] | None, inner: tuple[date, date] | None) -> bool:
    """Say whether `inner` lies within `outer`: a vaguer window confirms less than the claim says."""
    if outer is None or inner is None or outer[0] > outer[1] or inner[0] > inner[1]:
        return False
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def _agrees(claim: Statement, other: Statement) -> bool:
    return (
        other.clean
        and not _same_source(claim, other)
        and bool(claim.companies & other.companies)
        and bool(claim.catalyst_type)
        and other.catalyst_type == claim.catalyst_type
        and _inside(claim.window, other.window)
        and _drugs_agree(claim, other)
    )


def independent_source(claim: Statement, others: Iterable[Statement]) -> CheckResult:
    """Pass a claim from a regulator or SEC filing, or one that another source agrees with."""
    if claim.tier is SourceTier.REGULATOR_OR_SEC:
        return CheckResult(CHECK, Outcome.PASS)
    if any(_agrees(claim, other) for other in others):
        return CheckResult(CHECK, Outcome.PASS)
    return CheckResult(CHECK, Outcome.FAIL, "no second document states this event")


def independent_sources(statements: Sequence[Statement]) -> dict[UUID, CheckResult]:
    """Return every claim's outcome, comparing each only with its own companies' claims of its type."""
    groups: defaultdict[tuple[UUID, str | None], dict[UUID, Statement]] = defaultdict(dict)
    for statement in statements:
        for company in statement.companies:
            groups[(company, statement.catalyst_type)][statement.claim_id] = statement
    results: dict[UUID, CheckResult] = {}
    for statement in statements:
        others = {
            other.claim_id: other
            for company in statement.companies
            for other in groups[(company, statement.catalyst_type)].values()
        }
        results[statement.claim_id] = independent_source(statement, others.values())
    return results
