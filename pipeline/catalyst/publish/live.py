"""Which published catalyst a claim is about, and whether that catalyst is live yet (ADR 0018, part 5b).

Plain code over stored rows. A claim states the same event as an open published catalyst when
they share the company, the catalyst type, and the matched drugs, and then:
- the matched trials are the same; or, when no open catalyst has the same trials, one side names
  no trial and exactly one such catalyst exists (Opus review of PR #59, B4);
- and the stated indication is the same, compared after normalizing case and punctuation. A
  published catalyst's indication is the one its creating claim stated (B3).

When a claim fits more than one catalyst, or fits one except for the indication, it waits for a
person: plain code cannot tell a reworded indication from another event. A claim a change row
already cites is about that change's catalyst. Resolved catalysts are left out, so a new event of
the same kind (a new FDA date after a refusal) becomes a new catalyst.

Once a catalyst is live, its date passing is a change for the outcome tracker, not a reason to
hold a claim about it, so `window_not_past` does not apply to such claims.
"""

from __future__ import annotations

import re
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
SEVERAL = "the claim matches more than one published catalyst"
OTHER_INDICATION = "the indication differs from the published one"
# The fields whose newest change row names the claim behind the current dates (B1; round 2, N1).
WINDOW_FIELDS = frozenset(
    {
        CatalystField.WINDOW_START.value,
        CatalystField.WINDOW_END.value,
        CatalystField.WINDOW_PRECISION.value,
        CatalystField.DATE_CONFIDENCE.value,
    }
)
_TIERS: tuple[SourceTier, ...] = tuple(SourceTier)  # highest first (spec: Provenance, source ranking)
_WORD = re.compile(r"\w+")


@dataclass(frozen=True)
class Key:
    """The event a statement names: company, type, matched drugs and trials, and the indication."""

    company_id: UUID
    catalyst_type: str
    drugs: frozenset[UUID]
    trials: frozenset[UUID]
    indication: str  # normalized; empty when none is stated


@dataclass(frozen=True)
class Rank:
    """Where a claim's source stands: its tier, then when it was published (spec: Which claim wins)."""

    tier: SourceTier
    published_at: datetime


def ranks_above(claim: tuple[SourceTier, datetime], other: tuple[SourceTier, datetime]) -> bool:
    """Say whether a source outranks another: a higher tier wins; among equals, the more recent one.

    A tie does not outrank, so an equal source never replaces the one already shown.
    """
    (tier, published), (other_tier, other_published) = claim, other
    if tier is not other_tier:
        return _TIERS.index(tier) < _TIERS.index(other_tier)
    return published > other_published


def normal_indication(text: object) -> str:
    """Return an indication for comparison: its words, case-folded, punctuation and spacing ignored."""
    return " ".join(_WORD.findall(text.casefold())) if isinstance(text, str) else ""


def _resolved(matches: Iterable[Match], entity: MatchedEntity) -> frozenset[UUID]:
    return frozenset(
        m.matched_id
        for m in matches
        if m.entity == entity.value and m.status == MatchStatus.RESOLVED.value and m.matched_id is not None
    )


def claim_value(value: object) -> dict[str, Any]:
    """Return a claim's value as a dictionary, or an empty one when it is not one."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def claim_key(value: object, matches: Sequence[Match]) -> Key | None:
    """Return the event a claim states, or None when it has no type or not exactly one company."""
    stated = claim_value(value)
    kind: object = stated.get("catalyst_type")
    companies = _resolved(matches, MatchedEntity.COMPANY)
    if not isinstance(kind, str) or not kind or len(companies) != 1:
        return None
    return Key(
        company_id=next(iter(companies)),
        catalyst_type=kind,
        drugs=_resolved(matches, MatchedEntity.DRUG),
        trials=_resolved(matches, MatchedEntity.TRIAL),
        indication=normal_indication(stated.get("indication")),
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
class Place:
    """Where a claim belongs: a published catalyst, a new event (neither set), or a person's review."""

    catalyst: LiveCatalyst | None = None
    hold: str | None = None


def place(key: Key, catalysts: Iterable[LiveCatalyst]) -> Place:
    """Find the open catalyst a claim's event is, a new event, or why a person must decide."""
    same = [
        c
        for c in catalysts
        if (c.key.company_id, c.key.catalyst_type, c.key.drugs)
        == (key.company_id, key.catalyst_type, key.drugs)
    ]
    exact = [c for c in same if c.key.trials == key.trials]
    pool = exact or [c for c in same if not c.key.trials or not key.trials]
    if not pool:
        return Place()
    if not exact and len(pool) > 1:
        return Place(hold=SEVERAL)
    fit = [c for c in pool if c.key.indication == key.indication]
    if len(fit) > 1:
        return Place(hold=SEVERAL)
    return Place(catalyst=fit[0]) if fit else Place(hold=OTHER_INDICATION)


@dataclass(frozen=True)
class LiveIndex:
    """Every open published catalyst, and the catalyst each cited claim is about."""

    cited: Mapping[UUID, UUID]  # claim -> catalyst
    catalysts: Sequence[LiveCatalyst]

    def place(self, key: Key | None) -> Place:
        """Place a claim's event among the open published catalysts (a new event when it has no key)."""
        return place(key, self.catalysts) if key is not None else Place()

    def is_live(self, claim_id: UUID, key: Key | None) -> bool:
        """Say whether the claim is about a catalyst that is already published."""
        return claim_id in self.cited or self.place(key).catalyst is not None


def load_live(session: Session) -> LiveIndex:
    """Read every published catalyst with its links, its indication, and the source of its window."""
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
    indication: dict[UUID, str] = {}
    last_learned: dict[UUID, datetime] = {}
    changes = (
        select(
            change.c.catalyst_id,
            change.c.claim_id,
            change.c.field,
            change.c.learned_at,
            claim.c.value,
            document.c.source_tier,
            document.c.published_at,
        )
        .join(claim, claim.c.id == change.c.claim_id)
        .join(document, document.c.id == claim.c.source_document_id)
        .order_by(change.c.learned_at, change.c.id)
    )
    for row in session.execute(changes):  # oldest first, so the last row of each kind is current
        cited[row.claim_id] = row.catalyst_id
        last_learned[row.catalyst_id] = row.learned_at
        if row.field in WINDOW_FIELDS:
            window_source[row.catalyst_id] = Rank(SourceTier(row.source_tier), row.published_at)
        elif row.field == CatalystField.TITLE.value:  # set once, by the claim that created it
            indication[row.catalyst_id] = normal_indication(claim_value(row.value).get("indication"))
    found: list[LiveCatalyst] = []
    for row in session.execute(select(catalyst).where(catalyst.c.status != CatalystStatus.RESOLVED.value)):
        live = Catalyst.model_validate(row, from_attributes=True)
        source = window_source.get(live.id)
        if source is None:  # no change row: not written by the gate, so never matched to a claim
            continue
        key = Key(
            live.company_id,
            live.catalyst_type.value,
            frozenset(drugs[live.id]),
            frozenset(trials[live.id]),
            indication.get(live.id, ""),
        )
        found.append(LiveCatalyst(live, key, source, last_learned[live.id]))
    return LiveIndex(cited=cited, catalysts=found)


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
