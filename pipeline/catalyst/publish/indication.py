"""The writer's indication pass: a catalyst's disease, from its latest confirmed try (ADR 0053, decision 2).

Plain code, no model calls, under the writer's lock (ADR 0018, part 5b). For each published
catalyst with no indication, it takes the catalyst's latest `indication_attempt`; only when that
try found one, it checks again before writing:

- the claim is this catalyst's own indication claim (`indication:<catalyst id>`);
- the second reader's pass (`indication_confirmed`) is stored beside it;
- the quote is, character for character, at its stored place in the stored copy, for the current
  text version (`gate.match_quote`), so every published value keeps its source words.

Then it writes the catalyst's `indication_id` and a change row citing the claim. A later refusal
always wins over an earlier find, because only the latest try counts; claims are never chosen by
field. It never changes an indication already set, deletes, or unpublishes. Safe to re-run.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.extract.indicate import CONFIRMED_CHECK, claim_field
from catalyst.logs import log_event
from catalyst.publish import gate
from catalyst.publish.live import claim_value
from catalyst.publish.write import LOCK
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CatalystField,
    CheckOutcome,
    Claim,
    ClaimCheck,
    Indication,
    IndicationAttempt,
    IndicationResult,
    SourceDocument,
)
from catalyst.snapshots import SnapshotStore

_log = logging.getLogger(__name__)

NOT_ITS_OWN = "the claim is not this catalyst's indication claim"
NOT_CONFIRMED = "the second reader's pass is not stored beside the claim"
NO_DISEASE = "the claim names no MeSH disease"


@dataclass
class IndicationWrites:
    """What one pass wrote, and why each found indication it did not write waits."""

    written: int = 0
    held: Counter[str] = field(default_factory=Counter[str])

    def summary(self) -> dict[str, Any]:
        """Return the counts as plain JSON."""
        return {"indications_written": self.written, "indications_held": dict(sorted(self.held.items()))}


def _latest_found(session: Session) -> list[Any]:
    """Return each catalyst's latest try when it found an indication, for catalysts that have none."""
    attempt, catalyst = TABLES[IndicationAttempt], TABLES[Catalyst]
    latest = (
        select(attempt)
        .ext(distinct_on(attempt.c.catalyst_id))
        .order_by(attempt.c.catalyst_id, attempt.c.attempted_at.desc(), attempt.c.id.desc())
        .subquery()
    )
    query = (
        select(latest)
        .join(catalyst, catalyst.c.id == latest.c.catalyst_id)
        .where(latest.c.outcome == IndicationResult.FOUND.value, catalyst.c.indication_id.is_(None))
        .order_by(latest.c.catalyst_id)
    )
    return list(session.execute(query))


def _claim_and_copy(session: Session, claim_id: UUID) -> Any:
    claim, document = TABLES[Claim], TABLES[SourceDocument]
    return session.execute(
        select(claim, document.c.snapshot_key, document.c.sha256, document.c.content_type)
        .join(document, document.c.id == claim.c.source_document_id)
        .where(claim.c.id == claim_id)
    ).one()


def _confirmed(session: Session, claim_id: UUID) -> bool:
    check = TABLES[ClaimCheck]
    outcome = session.execute(
        select(check.c.outcome).where(check.c.claim_id == claim_id, check.c.check_name == CONFIRMED_CHECK)
    ).scalar_one_or_none()
    return outcome == CheckOutcome.PASS.value


def _indication_id(session: Session, mesh_id: str, name: str) -> UUID:
    """Return the indication row for this MeSH disease with no line or biomarker, adding it once."""
    table = TABLES[Indication]
    row = Indication(mesh_id=mesh_id, name=name)
    inserted = session.execute(
        insert(table).values(**row.model_dump()).on_conflict_do_nothing().returning(table.c.id)
    ).scalar_one_or_none()
    if inserted is not None:
        return inserted
    return session.execute(
        select(table.c.id).where(
            table.c.mesh_id == mesh_id, table.c.line_of_therapy.is_(None), table.c.biomarker.is_(None)
        )
    ).scalar_one()


def _why_not(session: Session, store: SnapshotStore, attempt: Any) -> tuple[str | None, Any]:
    """Return why this found indication may not be written (None when it may), and its claim row."""
    row = _claim_and_copy(session, attempt.indication_claim_id)
    if row.subject_id != attempt.catalyst_id or row.field != claim_field(attempt.catalyst_id):
        return NOT_ITS_OWN, row
    if not _confirmed(session, row.id):
        return NOT_CONFIRMED, row
    value = claim_value(row.value)
    if not isinstance(value.get("mesh_id"), str) or not isinstance(value.get("mesh_name"), str):
        return NO_DISEASE, row
    claim = Claim.model_validate({k: getattr(row, k) for k in Claim.model_fields})
    quote = gate.match_quote(claim, gate.read_copy(store, row.snapshot_key, row.sha256, row.content_type))
    if quote.outcome is not CheckOutcome.PASS:
        return quote.reason or "the quote check failed", row
    return None, row


def write_indications(session: Session, store: SnapshotStore, at: datetime) -> IndicationWrites:
    """Write each catalyst's indication from its latest confirmed try, inside the caller's transaction."""
    report = IndicationWrites()
    for attempt in _latest_found(session):
        reason, row = _why_not(session, store, attempt)
        if reason is not None:
            report.held[reason] += 1
            log_event(_log, "indication_held", catalyst_id=str(attempt.catalyst_id), reason=reason)
            continue
        value = claim_value(row.value)
        indication_id = _indication_id(session, str(value["mesh_id"]), str(value["mesh_name"]))
        table = TABLES[Catalyst]
        session.execute(
            update(table).where(table.c.id == attempt.catalyst_id).values(indication_id=indication_id)
        )
        change = CatalystChange(
            catalyst_id=attempt.catalyst_id,
            field=CatalystField.INDICATION_ID,
            new_value=str(indication_id),
            claim_id=row.id,
            learned_at=at,
        )
        session.execute(insert(TABLES[CatalystChange]).values(**change.model_dump()))
        report.written += 1
        log_event(_log, "indication_written", catalyst_id=str(attempt.catalyst_id), claim_id=str(row.id))
    return report


def publish_indications(
    session_factory: sessionmaker[Session], store: SnapshotStore, now: Callable[[], datetime]
) -> IndicationWrites:
    """Run the indication pass on its own, under the writer's lock, in one transaction."""
    with session_factory.begin() as session:
        session.execute(select(func.pg_advisory_xact_lock(LOCK)))
        report = write_indications(session, store, now())
    log_event(_log, "indications_published", **report.summary())
    return report
