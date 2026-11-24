"""The reviewed withdrawal list: claims a person found stale or misread after publication (ADR 0070).

Data is never edited by hand, so a withdrawal is a reviewed entry in `config/claim_withdrawals.toml`.
It only ever withholds: the gate holds a listed claim with "withdrawn after review: <reason>",
which no approval excuses, and the writer unpublishes every live catalyst that cites one, with a
status change in that catalyst's history. An entry names a full claim id or a prefix of at
least 8 hex characters; one that names no claim, or several, stops the run with an error, which
the run's alert reports, so a typo never passes silently.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalyst.db.tables import TABLES
from catalyst.publish.gate import Decision, Hold
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CatalystField,
    CatalystStatus,
    CatalystTrial,
    Claim,
    ClaimDecision,
    GateDecision,
)

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "claim_withdrawals.toml"
CHECK = "withdrawn_after_review"
PREFIX = "withdrawn after review: "


class WithdrawalError(ValueError):
    """The withdrawal list is unreadable, or an entry names no claim or several."""


class Withdrawal(BaseModel):
    """One reviewed entry: which claim, why, the evidence, and when it was added."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim: str = Field(pattern=r"^[0-9a-f]{8}[0-9a-f-]{0,28}$", description="A claim id, or a prefix of it.")
    reason: str = Field(min_length=1, max_length=280)
    evidence: str = Field(pattern=r"^(https://\S+|own source)$")
    added: date


def load_withdrawals(path: Path = DEFAULT_PATH) -> list[Withdrawal]:
    """Read the list; a missing or malformed file is an error, never an empty list."""
    try:
        data = tomllib.loads(path.read_text())
        return [Withdrawal.model_validate(entry) for entry in data.get("withdrawal", [])]
    except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
        raise WithdrawalError(f"the withdrawal list {path.name} cannot be read: {error}") from error


def resolve(withdrawals: Sequence[Withdrawal], claim_ids: Iterable[UUID]) -> dict[UUID, Withdrawal]:
    """Return each listed claim's entry; refuse an entry that names no claim or several."""
    ids = [(claim_id.hex, claim_id) for claim_id in claim_ids]
    found: dict[UUID, Withdrawal] = {}
    for entry in withdrawals:
        prefix = entry.claim.replace("-", "")
        hits = [claim_id for text, claim_id in ids if text.startswith(prefix)]
        if len(hits) != 1:
            raise WithdrawalError(f"withdrawal {entry.claim} names {len(hits)} claims, not exactly one")
        found.setdefault(hits[0], entry)
    return found


def load_withdrawn(session: Session, withdrawals: Sequence[Withdrawal]) -> dict[UUID, Withdrawal]:
    """Resolve the list against every stored claim."""
    if not withdrawals:
        return {}
    return resolve(withdrawals, session.execute(select(TABLES[Claim].c.id)).scalars())


def withdrawals_pending(session: Session, withdrawals: Sequence[Withdrawal]) -> int:
    """Count listed claims not yet applied: still marked to publish, or behind a catalyst not withdrawn."""
    withdrawn = load_withdrawn(session, withdrawals)
    if not withdrawn:
        return 0
    decision, change, catalyst = TABLES[ClaimDecision], TABLES[CatalystChange], TABLES[Catalyst]
    ids = list(withdrawn)
    marked = select(decision.c.claim_id).where(
        decision.c.claim_id.in_(ids), decision.c.decision == GateDecision.PUBLISH.value
    )
    shown = (
        select(change.c.claim_id)
        .join(catalyst, catalyst.c.id == change.c.catalyst_id)
        .where(change.c.claim_id.in_(ids), catalyst.c.status != CatalystStatus.WITHDRAWN.value)
    )
    return len({*session.execute(marked).scalars(), *session.execute(shown).scalars()})


def withdraw(decision: Decision, entry: Withdrawal | None) -> Decision:
    """Hold a listed claim, whatever else was decided; applied after any approval, so none lifts it."""
    if entry is None:
        return decision
    return Decision(GateDecision.REVIEW, (*decision.holds, Hold(CHECK, PREFIX + entry.reason)))


def unpublish(session: Session, withdrawn: Mapping[UUID, Withdrawal], at: datetime) -> int:
    """Withdraw every catalyst a listed claim stands behind; return how many changed now.

    Each gets a `status` change citing the listed claim, later than its last change. Safe to
    re-run: a withdrawn catalyst is left as it is.
    """
    if not withdrawn:
        return 0
    change, trial, catalyst = TABLES[CatalystChange], TABLES[CatalystTrial], TABLES[Catalyst]
    ids = list(withdrawn)
    pairs = [
        *session.execute(select(change.c.catalyst_id, change.c.claim_id).where(change.c.claim_id.in_(ids)))
    ]
    pairs += [
        *session.execute(select(trial.c.catalyst_id, trial.c.claim_id).where(trial.c.claim_id.in_(ids)))
    ]
    by_catalyst: dict[UUID, UUID] = {}
    for catalyst_id, claim_id in sorted(pairs, key=lambda pair: (str(pair[0]), str(pair[1]))):
        by_catalyst.setdefault(catalyst_id, claim_id)
    changed = 0
    for catalyst_id, claim_id in by_catalyst.items():
        status = session.execute(select(catalyst.c.status).where(catalyst.c.id == catalyst_id)).scalar_one()
        if status == CatalystStatus.WITHDRAWN.value:
            continue
        last = session.execute(
            select(func.max(change.c.learned_at)).where(change.c.catalyst_id == catalyst_id)
        ).scalar_one()
        row = CatalystChange(
            catalyst_id=catalyst_id,
            field=CatalystField.STATUS,
            old_value=status,
            new_value=CatalystStatus.WITHDRAWN.value,
            claim_id=claim_id,
            learned_at=max(at, last + timedelta(microseconds=1)) if last else at,
            is_correction=True,
        )
        session.execute(
            update(catalyst).where(catalyst.c.id == catalyst_id).values(status=CatalystStatus.WITHDRAWN.value)
        )
        session.execute(insert(change).values(**row.model_dump()))
        changed += 1
    return changed
