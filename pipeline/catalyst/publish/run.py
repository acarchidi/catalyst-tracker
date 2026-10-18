"""The decision job: store the publish gate's decision for every extractor claim (ADR 0018, part 5a).

Free and plain code: it reads the stored checks and each claim's stored copy, never a model.
Safe to re-run; each claim keeps one decision, updated in place. It writes no catalyst rows yet
(part 5b).
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.extract.agent import ROLE
from catalyst.logs import log_event
from catalyst.publish.gate import UnreadableCopy, decide, match_quote, read_copy
from catalyst.schemas import Claim, ClaimCheck, ClaimDecision, GateDecision, SourceDocument
from catalyst.snapshots import SnapshotStore

TOP_REASONS = 10
BATCH = 1000  # rows per statement, well inside Postgres's limit on parameters

_log = logging.getLogger(__name__)


@dataclass
class DecisionReport:
    """What one run decided."""

    claims: int = 0
    to_publish: int = 0
    to_review: int = 0
    copies_unreadable: int = 0
    reasons: Counter[str] = field(default_factory=Counter[str])

    def summary(self) -> dict[str, Any]:
        """Return the counts as plain JSON, with the most common reasons for review."""
        return {
            "claims": self.claims,
            "to_publish": self.to_publish,
            "to_review": self.to_review,
            "copies_unreadable": self.copies_unreadable,
            "top_reasons": dict(self.reasons.most_common(TOP_REASONS)),
        }


def _load(session: Session) -> tuple[list[Any], dict[UUID, list[ClaimCheck]]]:
    claim, document, check = TABLES[Claim], TABLES[SourceDocument], TABLES[ClaimCheck]
    query = (
        select(claim, document.c.snapshot_key, document.c.sha256, document.c.content_type)
        .join(document, document.c.id == claim.c.source_document_id)
        .where(claim.c.agent == ROLE, claim.c.field.startswith("catalyst:"))
        .order_by(claim.c.extracted_at, claim.c.id)
    )
    rows = list(session.execute(query))
    checks: defaultdict[UUID, list[ClaimCheck]] = defaultdict(list)
    for row in session.execute(select(check)).mappings():
        checks[row["claim_id"]].append(ClaimCheck.model_validate(dict(row)))
    return rows, checks


def decide_claims(
    session_factory: sessionmaker[Session], store: SnapshotStore, now: Callable[[], datetime]
) -> DecisionReport:
    """Decide every extractor claim from its stored checks and stored copy, and keep the decision."""
    report = DecisionReport()
    at = now()
    with session_factory() as session:
        rows, checks = _load(session)
    copies: dict[UUID, str | UnreadableCopy] = {}
    decisions: list[dict[str, Any]] = []
    names = list(TABLES[Claim].c.keys())
    for row in rows:
        claim = Claim.model_validate({name: getattr(row, name) for name in names})
        if claim.source_document_id not in copies:
            copy = read_copy(store, row.snapshot_key, row.sha256, row.content_type)
            copies[claim.source_document_id] = copy
            report.copies_unreadable += isinstance(copy, UnreadableCopy)
        decision = decide(
            claim, checks.get(claim.id, []), match_quote(claim, copies[claim.source_document_id])
        )
        report.claims += 1
        if decision.decision is GateDecision.PUBLISH:
            report.to_publish += 1
        else:
            report.to_review += 1
            report.reasons.update(hold.label for hold in decision.holds)
        stored = ClaimDecision(
            claim_id=claim.id, decision=decision.decision, reasons=list(decision.reasons), decided_at=at
        )
        decisions.append(stored.model_dump())
    with session_factory.begin() as session:
        for start in range(0, len(decisions), BATCH):
            statement = insert(TABLES[ClaimDecision]).values(decisions[start : start + BATCH])
            changes = {key: statement.excluded[key] for key in ("decision", "reasons", "decided_at")}
            session.execute(statement.on_conflict_do_update(index_elements=["claim_id"], set_=changes))
    log_event(_log, "claims_decided", **report.summary())
    return report
