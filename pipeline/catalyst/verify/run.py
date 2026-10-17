"""The check job: run every rule check on every extractor claim and keep the latest outcomes (ADR 0018).

Plain code over stored rows: the claim, its source document's publication date, the entity
resolver's matches, and the registry phases of matched trials. Safe to re-run; each claim keeps
one row per check, updated in place.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.extract.agent import ROLE
from catalyst.extract.run import NEW_YORK
from catalyst.logs import log_event
from catalyst.schemas import Claim, ClaimCheck, EntityMatch, SourceDocument, Trial, TrialPhase
from catalyst.verify.rules import ClaimFacts, Match, Outcome, rule_checks

_log = logging.getLogger(__name__)
_CHECK = TABLES[ClaimCheck]


@dataclass
class CheckReport:
    """What one run found."""

    claims: int = 0
    claims_passing: int = 0
    outcomes: Counter[str] = field(default_factory=Counter[str])

    def summary(self) -> dict[str, Any]:
        """Return the counts as plain JSON."""
        return {"claims": self.claims, "claims_passing": self.claims_passing, "outcomes": dict(self.outcomes)}


def _matches(session: Session) -> dict[UUID, list[Match]]:
    match = TABLES[EntityMatch]
    found: defaultdict[UUID, list[Match]] = defaultdict(list)
    query = select(match.c.claim_id, match.c.entity_type, match.c.status, match.c.matched_id)
    for row in session.execute(query):
        found[row.claim_id].append(Match(row.entity_type, row.status, row.matched_id))
    return found


def _phases(session: Session) -> dict[UUID, TrialPhase | None]:
    trial = TABLES[Trial]
    return {
        row.id: TrialPhase(row.phase) if row.phase else None
        for row in session.execute(select(trial.c.id, trial.c.phase))
    }


def _value(raw: object) -> dict[str, Any]:
    return cast(dict[str, Any], raw) if isinstance(raw, dict) else {}


def _facts(session: Session) -> list[ClaimFacts]:
    claim, document = TABLES[Claim], TABLES[SourceDocument]
    matches, phases = _matches(session), _phases(session)
    query = (
        select(claim.c.id, claim.c.value, claim.c.quote, document.c.published_at)
        .join(document, document.c.id == claim.c.source_document_id)
        .where(claim.c.agent == ROLE, claim.c.field.startswith("catalyst:"))
        .order_by(claim.c.extracted_at, claim.c.id)
    )
    return [
        ClaimFacts(
            id=row.id,
            value=_value(row.value),
            quote=row.quote,
            published=row.published_at.astimezone(NEW_YORK).date(),
            matches=tuple(matches.get(row.id, [])),
            trial_phases=phases,
        )
        for row in session.execute(query)
    ]


def run_checks(session_factory: sessionmaker[Session], now: Callable[[], datetime]) -> CheckReport:
    """Run every rule check on every extractor claim; store one outcome per claim and check.

    Dates are New York dates, as in the extractor: a 9pm run on October 17 checks against October 17.
    """
    report = CheckReport()
    at = now()
    today = at.astimezone(NEW_YORK).date()
    with session_factory.begin() as session:
        for facts in _facts(session):
            report.claims += 1
            results = rule_checks(facts, today)
            for result in results:
                row = ClaimCheck(
                    claim_id=facts.id,
                    check_name=result.check,
                    outcome=result.outcome,
                    reason=result.reason[:300] if result.reason else None,
                    checked_at=at,
                ).model_dump()
                changes = {k: row[k] for k in ("outcome", "reason", "checked_at")}
                statement = insert(_CHECK).values(**row)
                session.execute(
                    statement.on_conflict_do_update(index_elements=["claim_id", "check_name"], set_=changes)
                )
                report.outcomes[f"{result.check}:{result.outcome.value}"] += 1
            if all(result.outcome is not Outcome.FAIL for result in results):
                report.claims_passing += 1
    log_event(_log, "claims_checked", **report.summary())
    return report
