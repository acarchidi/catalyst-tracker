"""The decision job: store the publish gate's decision for every extractor claim (ADR 0018, part 5a).

Free and plain code: it reads the stored checks and each claim's stored copy, never a model.
Safe to re-run; each claim keeps one decision, updated in place. It writes no catalyst rows: the
catalyst writer (`catalyst.publish.write`, part 5b) checks again before it writes, so a publish
decision here is not yet final.

`window_not_past` does not apply to a claim about a catalyst that is already published: its date
passing is for the outcome tracker, not a review item.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from itertools import groupby
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.extract.agent import ROLE
from catalyst.logs import log_event
from catalyst.publish.gate import UnreadableCopy, decide, match_quote, read_copy
from catalyst.publish.live import claim_key, excuse_passed_window, load_live
from catalyst.schemas import Claim, ClaimCheck, ClaimDecision, GateDecision, SourceDocument
from catalyst.snapshots import SnapshotStore
from catalyst.verify.run import load_matches

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
        .order_by(claim.c.source_document_id, claim.c.extracted_at, claim.c.id)
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
        matches, live = load_matches(session), load_live(session)
    decisions: list[dict[str, Any]] = []
    names = list(TABLES[Claim].c.keys())
    # Claims come grouped by document, so only the current document's text is held (review of PR #52).
    for _, group in groupby(rows, key=lambda row: row.source_document_id):
        group = list(group)
        first = group[0]
        copy = read_copy(store, first.snapshot_key, first.sha256, first.content_type)
        report.copies_unreadable += isinstance(copy, UnreadableCopy)
        for row in group:
            claim = Claim.model_validate({name: getattr(row, name) for name in names})
            stored_checks = checks.get(claim.id, [])
            if live.is_live(claim.id, claim_key(claim.value, matches.get(claim.id, []))):
                stored_checks = excuse_passed_window(claim.id, stored_checks, at)
            decision = decide(claim, stored_checks, match_quote(claim, copy))
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
        del copy
    with session_factory.begin() as session:
        for start in range(0, len(decisions), BATCH):
            statement = insert(TABLES[ClaimDecision]).values(decisions[start : start + BATCH])
            changes = {key: statement.excluded[key] for key in ("decision", "reasons", "decided_at")}
            session.execute(statement.on_conflict_do_update(index_elements=["claim_id"], set_=changes))
    log_event(_log, "claims_decided", **report.summary())
    return report
