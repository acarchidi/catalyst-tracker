"""An advisory committee meeting needs a Federal Register notice (spec: Verification; ADR 0018, part 3).

A claimed advisory committee passes only when a stored notice, not replaced by an amendment, has
its meeting inside the claim's window and names the same drug, and no other stored notice casts
doubt on it. Names agree when they are the same apart from case, spaces, and punctuation; when
the longer one only adds dosage-form words ("atropine" and "Atropine Sulfate Ophthalmic Solution,
0.01%"); or when the reference tables call them one drug. A longer name that adds anything else
("insulin" and "Insulin Efsitora Alfa", "adalimumab" and "adalimumab-aaty") is another product.

It fails when a later amendment names the drug and does not put the meeting inside the window,
when any stored notice could not be read, or when the notice names a sponsor that the reference
tables match to another company (Opus review of PR #49). Every other catalyst type is not
applicable.
"""

from __future__ import annotations

import logging
import re
import unicodedata
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
from catalyst.verify.fedreg_notice import (
    NoticesInWindow,
    StoredNotice,
    StoredNotices,
    load_notices,
    notices_in,
)
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


_FORM_WORDS = frozenset(
    [
        "sulfate",
        "sulphate",
        "hydrochloride",
        "hcl",
        "sodium",
        "potassium",
        "calcium",
        "magnesium",
        "acetate",
        "citrate",
        "maleate",
        "mesylate",
        "tartrate",
        "phosphate",
        "succinate",
        "besylate",
        "bromide",
        "chloride",
        "fumarate",
        "ophthalmic",
        "solution",
        "suspension",
        "emulsion",
        "injection",
        "injectable",
        "tablet",
        "tablets",
        "capsule",
        "capsules",
        "oral",
        "topical",
        "cream",
        "ointment",
        "gel",
        "spray",
        "nasal",
        "inhalation",
        "powder",
        "for",
        "extended",
        "delayed",
        "release",
        "film",
        "coated",
        "subcutaneous",
        "intravenous",
        "kit",
        "drops",
        "mg",
        "mcg",
        "ml",
        "g",
        "units",
    ]
)
_WORD = re.compile(r"[0-9]+(?:\.[0-9]+)?[a-z]*|[a-z]+")
_DOSE = re.compile(r"[0-9]+(?:\.[0-9]+)?(?:mg|mcg|ml|g|iu)?")


def _words(name: str) -> list[str]:
    return _WORD.findall(unicodedata.normalize("NFKC", name).casefold())


def _adds_only_form_words(shorter: list[str], longer: list[str]) -> bool:
    """Say whether `longer` is `shorter` plus salt, dosage-form, and strength words only."""
    for start in range(len(longer) - len(shorter) + 1):
        if longer[start : start + len(shorter)] == shorter:
            extra = longer[:start] + longer[start + len(shorter) :]
            return all(w in _FORM_WORDS or _DOSE.fullmatch(w) for w in extra)
    return False


def _names_agree(claim: str, notice: str, same_drug: Callable[[str, str], bool]) -> bool:
    a, b = name_key(claim), name_key(notice)
    if not a or not b:
        return False
    if a == b or same_drug(claim, notice):
        return True
    first, second = _words(claim), _words(notice)
    shorter, longer = (first, second) if len(first) <= len(second) else (second, first)
    return (
        bool(shorter) and len("".join(shorter)) >= _SHORTEST_PART and _adds_only_form_words(shorter, longer)
    )


def _names_drug(stored: StoredNotice, drug: str, same_drug: Callable[[str, str], bool]) -> bool:
    return any(_names_agree(drug, product.value, same_drug) for product in stored.notice.products)


def _inside(window: tuple[date, date], stored: StoredNotice) -> bool:
    meeting = stored.notice.meeting
    return meeting is not None and window[0] <= meeting.value.start and meeting.value.end <= window[1]


def advisory_notice(
    claim: AdvisoryClaim,
    found: NoticesInWindow,
    same_drug: Callable[[str, str], bool],
    other_company: Callable[[str], bool] = lambda _sponsor: False,
) -> CheckResult:
    """Return the outcome for one claim, given the stored notices for its window.

    `other_company` says whether a sponsor's name matches a company other than the claim's own.
    """
    if claim.catalyst_type != ADVISORY_TYPE:
        return CheckResult(CHECK, Outcome.NOT_APPLICABLE)
    if claim.window is None:
        return CheckResult(CHECK, Outcome.FAIL, "the claim has no date window to look up a notice")
    if not claim.drug or not name_key(claim.drug):
        return CheckResult(CHECK, Outcome.FAIL, "the claim names no drug, so no notice can confirm it")
    drug, window = claim.drug, claim.window
    matches = [s for s in found.matching if _inside(window, s) and _names_drug(s, drug, same_drug)]
    if matches:
        newest = max(s.document.published_at for s in matches)
        moved = [
            s
            for s in found.amendments
            if s.document.published_at >= newest
            and not any(s is m for m in matches)
            and _names_drug(s, drug, same_drug)
            and not _inside(window, s)
        ]
        if moved:
            reason = "a later amendment names this drug and does not put the meeting in this window"
            return CheckResult(CHECK, Outcome.FAIL, reason)
        if found.unreadable:
            reason = f"{len(found.unreadable)} stored notice(s) could not be read, so a change may be missed"
            return CheckResult(CHECK, Outcome.FAIL, reason)
        if any(other_company(sponsor.value) for m in matches for sponsor in m.notice.sponsors):
            return CheckResult(CHECK, Outcome.FAIL, "the notice names another company as the sponsor")
        return CheckResult(CHECK, Outcome.PASS)
    if any(_names_drug(s, drug, same_drug) for s in found.undated):
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
        return first.status is Status.RESOLVED and second.status is Status.RESOLVED and first.id == second.id

    return same


def _other_company(references: ReferenceData, company: UUID | None) -> Callable[[str], bool]:
    def other(sponsor: str) -> bool:
        found = references.resolve_company(name=sponsor)
        return company is not None and found.status is Status.RESOLVED and found.id != company

    return other


def company_of(owners: set[UUID]) -> UUID | None:
    """Return a document's company, or None when it is linked to none or to several."""
    return next(iter(owners)) if len(owners) == 1 else None


def check_advisory_meetings(
    session_factory: sessionmaker[Session], store: SnapshotStore, now: Callable[[], datetime]
) -> AdvisoryReport:
    """Store `advisory_meeting_noticed` for every extractor claim; safe to re-run.

    The stored notices are read once per run, not once per claim (Opus review of PR #49).
    """
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
        rows = session.execute(query).all()
        loaded: StoredNotices | None = None
        for row in rows:
            value = _value(row.value)
            kind, drug = value.get("catalyst_type"), value.get("drug")
            subject = AdvisoryClaim(
                catalyst_type=kind if isinstance(kind, str) else None,
                drug=drug if isinstance(drug, str) else None,
                window=_window(value),
            )
            company = company_of(companies.get(row.source_document_id, set()))
            if subject.catalyst_type == ADVISORY_TYPE and subject.window is not None:
                loaded = loaded or load_notices(session, store)
                try:
                    found = notices_in(loaded, *subject.window)
                except (ValueError, OverflowError):
                    result = CheckResult(CHECK, Outcome.FAIL, "the claim's window cannot be looked up")
                else:
                    result = advisory_notice(
                        subject, found, _same_drug(references, company), _other_company(references, company)
                    )
            else:
                result = advisory_notice(subject, NoticesInWindow(), _same_drug(references, company))
            store_check(session, row.id, result, at)
            report.claims += 1
            key = f"{result.check}:{result.outcome.value}"
            report.outcomes[key] = report.outcomes.get(key, 0) + 1
    log_event(_log, "advisory_meetings_checked", **report.summary())
    return report
