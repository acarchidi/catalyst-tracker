"""A person's decisions on the claims the publish gate holds (spec: Human review; ADR 0042, part 1).

Recording a decision writes only a `review_decision` row, never live data. An approval reaches
live data the one way any claim does (spec: Agent architecture): the decision job applies it
through the gate (`catalyst.publish.gate.approve`), then the plain-code writer checks the claim
again and writes it, each change row citing the claim and its quoted source.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.logs import log_event
from catalyst.schemas import ClaimDecision, GateDecision, ReviewAction, ReviewDecision

_log = logging.getLogger(__name__)


class ReviewError(ValueError):
    """A decision that cannot be recorded, with the reason in plain words."""


def record_decision(
    session_factory: sessionmaker[Session],
    claim_id: UUID,
    action: ReviewAction,
    reviewer: str,
    seen: Sequence[str],
    now: Callable[[], datetime],
) -> ReviewDecision:
    """Record a person's approval or rejection of one held claim, and return the stored row.

    `seen` is the list of the gate's reasons the person was shown. It must be the claim's stored
    reasons now, word for word, so a claim re-decided since the page was made is looked at again.
    """
    if not reviewer.strip():
        raise ReviewError("a decision must say who made it")
    decision = TABLES[ClaimDecision]
    with session_factory.begin() as session:
        # Locked, so the decision job cannot change the reasons between this check and the insert.
        stored = session.execute(
            select(decision.c.decision, decision.c.reasons)
            .where(decision.c.claim_id == claim_id)
            .with_for_update()
        ).one_or_none()
        if stored is None or stored.decision != GateDecision.REVIEW:
            raise ReviewError(f"claim {claim_id} is not waiting for review")
        if list(stored.reasons) != list(seen):
            raise ReviewError(f"claim {claim_id}'s reasons changed since the page was made; look again")
        row = ReviewDecision(
            claim_id=claim_id, action=action, reviewer=reviewer, reasons_seen=list(seen), decided_at=now()
        )
        session.execute(insert(TABLES[ReviewDecision]).values(**row.model_dump()))
    log_event(_log, "claim_reviewed", claim_id=str(claim_id), action=action.value, reviewer=row.reviewer)
    return row


def approvals(session: Session) -> dict[UUID, frozenset[str]]:
    """Return, for each claim whose latest decision is an approval, the reasons the person saw."""
    review = TABLES[ReviewDecision]
    latest: dict[UUID, tuple[str, list[str]]] = {}
    query = select(review.c.claim_id, review.c.action, review.c.reasons_seen).order_by(
        review.c.decided_at, review.c.id
    )
    for row in session.execute(query):  # oldest first, so each claim's latest decision wins
        latest[row.claim_id] = (row.action, row.reasons_seen)
    return {
        claim_id: frozenset(seen)
        for claim_id, (action, seen) in latest.items()
        if action == ReviewAction.APPROVE
    }
