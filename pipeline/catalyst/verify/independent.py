"""Independent source: a second document states the same event, or the source is a regulator (ADR 0018).

The publish rule asks that "either a second source agrees or the source is a regulator or SEC
filing" (spec: Verification). Another document agrees when it is at another address, is linked
to the same company, and its own extractor claim, which the extractor flagged no problem with,
gives the same type, an overlapping window, and the same drug. A missing window or drug never
agrees, except that a deal need not name a drug.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from catalyst.schemas import SourceTier
from catalyst.verify.rules import CheckResult, Outcome
from catalyst.verify.second import name_key

CHECK = "independent_source"
_NO_DRUG_TYPE = "merger_acquisition"


@dataclass(frozen=True)
class Statement:
    """One extractor claim with what this check compares: its document, companies, and event."""

    claim_id: UUID
    url: str
    tier: SourceTier
    companies: frozenset[UUID]
    catalyst_type: str
    drug: str | None
    drug_ids: frozenset[UUID]  # the drug records the resolver matched the claim's drug name to
    window: tuple[date, date] | None
    clean: bool  # the extractor flagged no problem with it


def _drugs_agree(a: Statement, b: Statement) -> bool:
    first, second = name_key(a.drug), name_key(b.drug)
    if not first and not second:
        return a.catalyst_type == b.catalyst_type == _NO_DRUG_TYPE
    if not first or not second:
        return False
    return first == second or bool(a.drug_ids & b.drug_ids)


def _overlap(a: tuple[date, date] | None, b: tuple[date, date] | None) -> bool:
    return a is not None and b is not None and a[0] <= b[1] and b[0] <= a[1]


def _agrees(claim: Statement, other: Statement) -> bool:
    return (
        other.url != claim.url
        and other.clean
        and bool(claim.companies & other.companies)
        and other.catalyst_type == claim.catalyst_type
        and _overlap(claim.window, other.window)
        and _drugs_agree(claim, other)
    )


def independent_source(claim: Statement, others: Iterable[Statement]) -> CheckResult:
    """Pass a claim from a regulator or SEC filing, or one that another document agrees with."""
    if claim.tier is SourceTier.REGULATOR_OR_SEC:
        return CheckResult(CHECK, Outcome.PASS)
    if any(_agrees(claim, other) for other in others):
        return CheckResult(CHECK, Outcome.PASS)
    return CheckResult(CHECK, Outcome.FAIL, "no second document states this event")
