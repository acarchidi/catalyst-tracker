"""An advisory committee meeting needs a Federal Register notice (spec: Verification; ADR 0018, part 3).

A claimed advisory committee passes only when a stored notice, not replaced by an amendment, has
its meeting inside the claim's window and names the same drug: the same name apart from case,
spaces, and punctuation; one name inside the other when the shorter has at least five letters;
or one drug in the reference tables. A later amendment that names the drug but whose new date
cannot be read sends the claim to review, because matching an amendment to its notice by title
can miss (Claude review of PR #43). Every other catalyst type is not applicable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.extract.agent import ROLE
from catalyst.logs import log_event
from catalyst.references import load_reference_data
from catalyst.resolve import ReferenceData, Status
from catalyst.schemas import Claim, CompanyDocument
from catalyst.snapshots import SnapshotStore
from catalyst.verify.fedreg_notice import NoticesInWindow, StoredNotice, notices_for
from catalyst.verify.rules import CheckResult, Outcome
from catalyst.verify.run import store_check
from catalyst.verify.second import name_key

CHECK = "advisory_meeting_noticed"
ADVISORY_TYPE = "advisory_committee"
_SHORTEST_PART = 5  # "atropine" may stand for "Atropine Sulfate Ophthalmic Solution"; "atro" may not

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdvisoryClaim:
    """What this check reads from one extractor claim."""

    catalyst_type: str | None
    drug: str | None
    window: tuple[date, date] | None


def _names_agree(claim: str, notice: str, same_drug: Callable[[str, str], bool]) -> bool:
    a, b = name_key(claim), name_key(notice)
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return a == b or (len(shorter) >= _SHORTEST_PART and shorter in longer) or same_drug(claim, notice)


def _names_drug(stored: StoredNotice, drug: str, same_drug: Callable[[str, str], bool]) -> bool:
    return any(_names_agree(drug, product.value, same_drug) for product in stored.notice.products)


def _inside(window: tuple[date, date], stored: StoredNotice) -> bool:
    meeting = stored.notice.meeting
    return meeting is not None and window[0] <= meeting.value.start and meeting.value.end <= window[1]


def advisory_notice(
    claim: AdvisoryClaim, found: NoticesInWindow, same_drug: Callable[[str, str], bool]
) -> CheckResult:
    """Return the outcome for one claim, given the stored notices for its window."""
    if claim.catalyst_type != ADVISORY_TYPE:
        return CheckResult(CHECK, Outcome.NOT_APPLICABLE)
    if claim.window is None:
        return CheckResult(CHECK, Outcome.FAIL, "the claim has no date window to look up a notice")
    if not claim.drug or not name_key(claim.drug):
        return CheckResult(CHECK, Outcome.FAIL, "the claim names no drug, so no notice can confirm it")
    drug, window = claim.drug, claim.window
    matches = [s for s in found.matching if _inside(window, s) and _names_drug(s, drug, same_drug)]
    undated = [s for s in found.undated if _names_drug(s, drug, same_drug)]
    if matches:
        newest = max(s.document.published_at for s in matches)
        later = [s for s in undated if s.notice.amendment is not None and s.document.published_at > newest]
        if later:
            reason = "a later amendment names this drug, and its new date cannot be read"
            return CheckResult(CHECK, Outcome.FAIL, reason)
        return CheckResult(CHECK, Outcome.PASS)
    if undated:
        return CheckResult(
            CHECK, Outcome.FAIL, "a notice names this drug, but its meeting date cannot be read"
        )
    if any(_names_drug(s, drug, same_drug) for s in found.superseded):
        return CheckResult(CHECK, Outcome.FAIL, "the only notice for this drug was replaced by an amendment")
    return CheckResult(CHECK, Outcome.FAIL, "no Federal Register notice for this meeting yet")


@dataclass
class AdvisoryReport:
    """What one run found."""

    claims: int = 0
    outcomes: dict[str, int] = field(default_factory=dict[str, int])

    def summary(self) -> dict[str, Any]:
        """Return the counts as plain JSON."""
        return {"claims": self.claims, "outcomes": self.outcomes}


def _value(raw: object) -> dict[str, Any]:
    return cast(dict[str, Any], raw) if isinstance(raw, dict) else {}


def _window(value: dict[str, Any]) -> tuple[date, date] | None:
    start, end = value.get("window_start"), value.get("window_end")
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        return None
    return (first, last) if first <= last else None


def _same_drug(references: ReferenceData, company: UUID | None) -> Callable[[str, str], bool]:
    def same(a: str, b: str) -> bool:
        if company is None:
            return False
        first, second = references.resolve_drug(a, company), references.resolve_drug(b, company)
        return first.status is second.status is Status.RESOLVED and first.id == second.id

    return same


def check_advisory_meetings(
    session_factory: sessionmaker[Session], store: SnapshotStore, now: Callable[[], datetime]
) -> AdvisoryReport:
    """Store `advisory_meeting_noticed` for every extractor claim; safe to re-run."""
    claim, links = TABLES[Claim], TABLES[CompanyDocument]
    report = AdvisoryReport()
    at = now()
    with session_factory.begin() as session:
        companies: dict[UUID, set[UUID]] = {}
        for row in session.execute(select(links.c.source_document_id, links.c.company_id)):
            companies.setdefault(row.source_document_id, set()).add(row.company_id)
        references = load_reference_data(session)
        query = select(claim.c.id, claim.c.value, claim.c.source_document_id).where(
            claim.c.agent == ROLE, claim.c.field.startswith("catalyst:")
        )
        for row in session.execute(query).all():
            value = _value(row.value)
            kind, drug = value.get("catalyst_type"), value.get("drug")
            subject = AdvisoryClaim(
                catalyst_type=kind if isinstance(kind, str) else None,
                drug=drug if isinstance(drug, str) else None,
                window=_window(value),
            )
            found = NoticesInWindow()
            if subject.catalyst_type == ADVISORY_TYPE and subject.window is not None:
                found = notices_for(session, store, *subject.window)
            owners = companies.get(row.source_document_id, set())
            company = next(iter(owners)) if len(owners) == 1 else None
            result = advisory_notice(subject, found, _same_drug(references, company))
            store_check(session, row.id, result, at)
            report.claims += 1
            key = f"{result.check}:{result.outcome.value}"
            report.outcomes[key] = report.outcomes.get(key, 0) + 1
    log_event(_log, "advisory_meetings_checked", **report.summary())
    return report
