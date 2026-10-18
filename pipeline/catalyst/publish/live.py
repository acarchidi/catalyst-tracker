"""Which published catalyst a claim is about, and whether that catalyst is live yet (ADR 0018, part 5b).

Plain code over stored rows. A claim is about the same event as a published catalyst when they
share the company, the catalyst type, the matched drugs, and the matched trials. A claim already
cited by a change row is about that change's catalyst. Resolved catalysts are left out of the
lookup, so a new event of the same kind (a new FDA date after a refusal) becomes a new catalyst.

Once a catalyst is live, its date passing is a change for the outcome tracker, not a reason to
hold a claim about it, so `window_not_past` does not apply to such claims.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from catalyst.db.tables import TABLES
from catalyst.publish import gate
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CatalystDrug,
    CatalystField,
    CatalystStatus,
    CatalystTrial,
    CheckOutcome,
    Claim,
    ClaimCheck,
    MatchedEntity,
    MatchStatus,
    SourceDocument,
    SourceTier,
)
from catalyst.verify.rules import Match

WINDOW_CHECK = "window_not_past"
LIVE_REASON = "the catalyst is already published; a date that has passed is for the outcome tracker"
if WINDOW_CHECK not in gate.RULE_CHECKS or WINDOW_CHECK not in gate.MAY_NOT_APPLY:
    raise RuntimeError(f"{WINDOW_CHECK} must be a rule check that may not apply")
_TIERS: tuple[SourceTier, ...] = tuple(SourceTier)  # highest first (spec: Provenance, source ranking)


@dataclass(frozen=True)
class Key:
    """What makes two statements one event: the company, the type, and the matched drugs and trials."""

    company_id: UUID
    catalyst_type: str
    drugs: frozenset[UUID]
    trials: frozenset[UUID]


@dataclass(frozen=True)
class Rank:
    """Where a claim's source stands: its tier, then when it was published (spec: Which claim wins)."""

    tier: SourceTier
    published_at: datetime


def ranks_above(claim: tuple[SourceTier, datetime], other: tuple[SourceTier, datetime]) -> bool:
    """Say whether a source outranks another: a higher tier wins; among equals, the more recent one."""
    (tier, published), (other_tier, other_published) = claim, other
    if tier is not other_tier:
        return _TIERS.index(tier) < _TIERS.index(other_tier)
    return published > other_published


def _resolved(matches: Iterable[Match], entity: MatchedEntity) -> frozenset[UUID]:
    return frozenset(
        m.matched_id
        for m in matches
        if m.entity == entity.value and m.status == MatchStatus.RESOLVED.value and m.matched_id is not None
    )


def claim_key(value: object, matches: Sequence[Match]) -> Key | None:
    """Return the event a claim states, or None when it has no type or not exactly one company."""
    stated: dict[str, Any] = cast(dict[str, Any], value) if isinstance(value, dict) else {}
    kind: object = stated.get("catalyst_type")
    companies = _resolved(matches, MatchedEntity.COMPANY)
    if not isinstance(kind, str) or not kind or len(companies) != 1:
        return None
    return Key(
        company_id=next(iter(companies)),
        catalyst_type=kind,
        drugs=_resolved(matches, MatchedEntity.DRUG),
        trials=_resolved(matches, MatchedEntity.TRIAL),
    )


@dataclass(frozen=True)
class LiveCatalyst:
    """A published catalyst as the writer compares claims with it."""

    row: Catalyst
    key: Key
    source: Rank  # the claim behind its current window
    last_learned: datetime  # its newest change row's time; a later change must come after it

    @property
    def window(self) -> tuple[date, date]:
        """The window shown now."""
        return self.row.window_start, self.row.window_end


@dataclass(frozen=True)
class LiveIndex:
    """Every published catalyst, by the claims its change rows cite and by event."""

    cited: Mapping[UUID, UUID]  # claim -> catalyst
    open_by_key: Mapping[Key, Sequence[LiveCatalyst]]

    def about(self, key: Key | None) -> list[LiveCatalyst]:
        """Return the open published catalysts with this claim's event (empty when none or no key)."""
        return list(self.open_by_key.get(key, ())) if key is not None else []

    def is_live(self, claim_id: UUID, key: Key | None) -> bool:
        """Say whether the claim is about a catalyst that is already published."""
        return claim_id in self.cited or bool(self.about(key))


def load_live(session: Session) -> LiveIndex:
    """Read every published catalyst with its links and the source of its current window."""
    catalyst, change, claim, document = (
        TABLES[Catalyst],
        TABLES[CatalystChange],
        TABLES[Claim],
        TABLES[SourceDocument],
    )
    drugs: defaultdict[UUID, set[UUID]] = defaultdict(set)
    for row in session.execute(select(TABLES[CatalystDrug].c.catalyst_id, TABLES[CatalystDrug].c.drug_id)):
        drugs[row.catalyst_id].add(row.drug_id)
    trials: defaultdict[UUID, set[UUID]] = defaultdict(set)
    for row in session.execute(select(TABLES[CatalystTrial].c.catalyst_id, TABLES[CatalystTrial].c.trial_id)):
        trials[row.catalyst_id].add(row.trial_id)
    cited: dict[UUID, UUID] = {}
    window_source: dict[UUID, Rank] = {}
    last_learned: dict[UUID, datetime] = {}
    changes = (
        select(
            change.c.catalyst_id,
            change.c.claim_id,
            change.c.field,
            change.c.learned_at,
            document.c.source_tier,
            document.c.published_at,
        )
        .join(claim, claim.c.id == change.c.claim_id)
        .join(document, document.c.id == claim.c.source_document_id)
        .order_by(change.c.learned_at, change.c.id)
    )
    for row in session.execute(changes):
        cited[row.claim_id] = row.catalyst_id
        last_learned[row.catalyst_id] = row.learned_at
        if row.field == CatalystField.WINDOW_START.value:  # oldest first, so the last one is current
            window_source[row.catalyst_id] = Rank(SourceTier(row.source_tier), row.published_at)
    open_by_key: defaultdict[Key, list[LiveCatalyst]] = defaultdict(list)
    for row in session.execute(select(catalyst).where(catalyst.c.status != CatalystStatus.RESOLVED.value)):
        live = Catalyst.model_validate(row, from_attributes=True)
        source = window_source.get(live.id)
        if source is None:  # no change row: not written by the gate, so never matched to a claim
            continue
        key = Key(
            live.company_id, live.catalyst_type.value, frozenset(drugs[live.id]), frozenset(trials[live.id])
        )
        open_by_key[key].append(LiveCatalyst(live, key, source, last_learned[live.id]))
    return LiveIndex(cited=cited, open_by_key=dict(open_by_key))


def excuse_passed_window(claim_id: UUID, checks: Iterable[ClaimCheck], at: datetime) -> list[ClaimCheck]:
    """Return the checks with `window_not_past` marked not applicable: the claim's catalyst is live."""
    kept = [row for row in checks if row.check_name != WINDOW_CHECK]
    excused = ClaimCheck(
        claim_id=claim_id,
        check_name=WINDOW_CHECK,
        outcome=CheckOutcome.NOT_APPLICABLE,
        reason=LIVE_REASON,
        checked_at=at,
    )
    return [*kept, excused]
