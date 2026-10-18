"""The check job: run every rule check on every extractor claim and keep the latest outcomes (ADR 0018).

Plain code over stored rows: the claim, its source document's publication date, the entity
resolver's matches, and the registry phases, numbers, and study names of matched trials. The
independent-source check also reads every other document's extractor claims for the same company.
Safe to re-run; each claim keeps one row per check, updated in place.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.extract.agent import ROLE
from catalyst.extract.run import NEW_YORK
from catalyst.logs import log_event
from catalyst.schemas import (
    Claim,
    ClaimCheck,
    CompanyDocument,
    EntityMatch,
    MatchedEntity,
    SourceDocument,
    SourceTier,
    Trial,
    TrialPhase,
)
from catalyst.verify.independent import Statement, independent_sources
from catalyst.verify.rules import CheckResult, ClaimFacts, Match, Outcome, TrialNames, rule_checks

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


def _trial_names(session: Session) -> dict[UUID, TrialNames]:
    trial = TABLES[Trial]
    return {
        row.id: TrialNames(row.nct_id, row.study_name)
        for row in session.execute(select(trial.c.id, trial.c.nct_id, trial.c.study_name))
    }


def _value(raw: object) -> dict[str, Any]:
    return cast(dict[str, Any], raw) if isinstance(raw, dict) else {}


def _window(value: dict[str, Any]) -> tuple[date, date] | None:
    start, end = value.get("window_start"), value.get("window_end")
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        return date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        return None


def _statements(
    session: Session, matches: dict[UUID, list[Match]], passed_rules: set[UUID]
) -> list[Statement]:
    """Every extractor claim from each document's latest reading, as the independent check sees it.

    A claim vouches only when the extractor flagged nothing and every rule check passed; an older
    reading of a document is left out, so a superseded answer never vouches (Opus review of PR #46).
    """
    claim, document, link = TABLES[Claim], TABLES[SourceDocument], TABLES[CompanyDocument]
    companies: defaultdict[UUID, set[UUID]] = defaultdict(set)
    for row in session.execute(select(link.c.source_document_id, link.c.company_id)):
        companies[row.source_document_id].add(row.company_id)
    query = (
        select(
            claim.c.id,
            claim.c.value,
            claim.c.quote,
            claim.c.extracted_at,
            claim.c.source_document_id,
            document.c.url,
            document.c.sha256,
            document.c.source_tier,
        )
        .join(document, document.c.id == claim.c.source_document_id)
        .where(claim.c.agent == ROLE, claim.c.field.startswith("catalyst:"))
    )
    rows = list(session.execute(query))
    latest: dict[UUID, datetime] = {}
    for row in rows:
        latest[row.source_document_id] = max(
            row.extracted_at, latest.get(row.source_document_id, row.extracted_at)
        )
    statements: list[Statement] = []
    for row in rows:
        value = _value(row.value)
        drug, kind = value.get("drug"), value.get("catalyst_type")
        drug_ids = frozenset(
            m.matched_id
            for m in matches.get(row.id, [])
            if m.entity == MatchedEntity.DRUG and m.status == "resolved" and m.matched_id is not None
        )
        statements.append(
            Statement(
                claim_id=row.id,
                url=row.url,
                sha256=row.sha256,
                quote=row.quote,
                tier=SourceTier(row.source_tier),
                companies=frozenset(companies.get(row.source_document_id, set())),
                catalyst_type=kind if isinstance(kind, str) and kind else None,
                drug=drug if isinstance(drug, str) else None,
                drug_ids=drug_ids,
                window=_window(value),
                clean=(
                    value.get("problems") == []
                    and row.id in passed_rules
                    and row.extracted_at == latest[row.source_document_id]
                ),
            )
        )
    return statements


def _facts(session: Session, matches: dict[UUID, list[Match]]) -> list[ClaimFacts]:
    claim, document = TABLES[Claim], TABLES[SourceDocument]
    phases, names = _phases(session), _trial_names(session)
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
            trial_names=names,
        )
        for row in session.execute(query)
    ]


def store_check(session: Session, claim_id: UUID, result: CheckResult, at: datetime) -> None:
    """Keep one check's latest outcome for one claim, replacing any earlier one."""
    row = ClaimCheck(
        claim_id=claim_id,
        check_name=result.check,
        outcome=result.outcome,
        reason=result.reason[:300] if result.reason else None,
        checked_at=at,
    ).model_dump()
    changes = {k: row[k] for k in ("outcome", "reason", "checked_at")}
    statement = insert(_CHECK).values(**row)
    session.execute(statement.on_conflict_do_update(index_elements=["claim_id", "check_name"], set_=changes))


def run_checks(session_factory: sessionmaker[Session], now: Callable[[], datetime]) -> CheckReport:
    """Run every rule check and the independent-source check on every extractor claim.

    One outcome is stored per claim and check.

    Dates are New York dates, as in the extractor: a 9pm run on October 17 checks against October 17.
    """
    report = CheckReport()
    at = now()
    today = at.astimezone(NEW_YORK).date()
    with session_factory.begin() as session:
        matches = _matches(session)
        facts_list = _facts(session, matches)
        rules = {facts.id: rule_checks(facts, today) for facts in facts_list}
        passed = {i for i, results in rules.items() if all(r.outcome is not Outcome.FAIL for r in results)}
        independent = independent_sources(_statements(session, matches, passed))
        for facts in facts_list:
            report.claims += 1
            results = [*rules[facts.id], independent[facts.id]]
            for result in results:
                store_check(session, facts.id, result, at)
                report.outcomes[f"{result.check}:{result.outcome.value}"] += 1
            if all(result.outcome is not Outcome.FAIL for result in results):
                report.claims_passing += 1
    log_event(_log, "claims_checked", **report.summary())
    return report
