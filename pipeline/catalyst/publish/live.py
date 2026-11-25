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
the same kind (a new FDA date after a refusal) becomes a new catalyst; but a deal that plain code
found closed (ADR 0080) is kept aside, like a withdrawn one, so an older release of its "expected to
close" date never makes it a new catalyst again. An entry merged into another as the same event
(ADR 0082) is open no more; a claim that would fit only it waits for a person, and when the entry it
was merged into is withdrawn or closed, the merged entry is set aside with it.

Once a catalyst is live, its date passing is a change for the outcome tracker, not a reason to
hold a claim about it, so `window_not_past` does not apply to such claims.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
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
MERGED_EVENT = "the claim fits only an entry merged into another as the same event; a person must decide"
# The fields whose newest change row names the claim behind the current dates (B1; round 2, N1).
WINDOW_FIELDS = frozenset(
    {
        CatalystField.WINDOW_START.value,
        CatalystField.WINDOW_END.value,
        CatalystField.WINDOW_PRECISION.value,
        CatalystField.DATE_CONFIDENCE.value,
    }
)
# The plain-code role whose claims close a deal that a stored document says was completed (ADR 0080).
DEAL_CLOSER = "deal_closer"
# The plain-code role whose claims mark an entry merged into another as the same event (ADR 0082).
DEDUPLICATOR = "deduplicator"
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
    # A closed deal's names (ADR 0080): its counterparty names and the company's own; else empty.
    names: frozenset[str] = frozenset()
    parties: frozenset[str] = frozenset()  # a closed deal's counterparty names alone; else empty
    into: UUID | None = None  # a merged entry's kept entry (ADR 0082); else empty

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


def place_among(key: Key, catalysts: Sequence[LiveCatalyst], merged: Sequence[LiveCatalyst] = ()) -> Place:
    """Place a claim like `place`; one that would be a new event but fits a merged entry waits (ADR 0082).

    It is never sent to the kept entry: the merged entry may have named no trial, so the claim may be
    another trial's event (Opus review of 66ecb0f, finding 2). A person decides.
    """
    where = place(key, catalysts)
    if where.catalyst is not None or where.hold is not None or not merged:
        return where
    alias = place(key, merged)
    return Place(hold=MERGED_EVENT) if alias.catalyst is not None or alias.hold is not None else where


@dataclass(frozen=True)
class LiveIndex:
    """Every open published catalyst, and the catalyst each cited claim is about."""

    cited: Mapping[UUID, UUID]  # claim -> catalyst
    catalysts: Sequence[LiveCatalyst]
    # Withdrawn catalysts (ADR 0070); each `source.published_at` is when it was withdrawn.
    withdrawn: Sequence[LiveCatalyst] = ()
    # Deals found closed (ADR 0080); each `source.published_at` is when its completion document was
    # published, and `names` are the counterparty names it matched.
    closed: Sequence[LiveCatalyst] = ()
    # Entries merged into another as the same event (ADR 0082), each with `into` set.
    merged: Sequence[LiveCatalyst] = ()

    def was_withdrawn(self, key: Key, published_at: datetime) -> bool:
        """Say whether the claim's event was withdrawn after review and its source is no newer.

        Matched like `place`, but on company, type, drugs and trials only: a stated indication
        never lets a stale event back in. Only a document published after the withdrawal itself is
        new: later releases often repeat the stale date (Opus re-review of PR #151).
        """
        return _set_aside(self.withdrawn, key, published_at)

    def was_closed(self, key: Key, published_at: datetime, names: frozenset[str]) -> bool:
        """Say whether the claim is about a deal found closed, from a source no newer (ADR 0080).

        Matched on company, type, drugs and trials as `was_withdrawn` is, and held by default: a
        claim that names any counterparty of the closed deal is held, even beside a new name; one
        goes through only when it names a deal party (`names`) outside the closed deal's names (its
        counterparties and the company's own), so it is about another deal of the company (Opus
        reviews of PR #163). Only a document published after the completion document is new.
        """
        held = [c for c in self.closed if names & c.parties or not names - c.names]
        return _set_aside(held, key, published_at)

    def place(self, key: Key | None) -> Place:
        """Place a claim's event among the open published catalysts (a new event when it has no key)."""
        return place_among(key, self.catalysts, self.merged) if key is not None else Place()

    def is_live(self, claim_id: UUID, key: Key | None) -> bool:
        """Say whether the claim is about a catalyst that is already published."""
        return claim_id in self.cited or self.place(key).catalyst is not None


def _set_aside(catalysts: Iterable[LiveCatalyst], key: Key, published_at: datetime) -> bool:
    same = [
        c
        for c in catalysts
        if (c.key.company_id, c.key.catalyst_type, c.key.drugs)
        == (key.company_id, key.catalyst_type, key.drugs)
        and (c.key.trials == key.trials or not c.key.trials or not key.trials)
    ]
    return any(published_at <= c.source.published_at for c in same)


def _listed(stated: Mapping[str, Any], parts: Iterable[str]) -> frozenset[str]:
    """Return the names a closing claim's value lists under `parts` (ADR 0080)."""
    return frozenset(
        name
        for part in parts
        if isinstance(listed := stated.get(part), list)
        for name in cast(list[object], listed)
        if isinstance(name, str)
    )


def _merged_into(catalyst_id: UUID, agent: str, value: object) -> dict[UUID, UUID]:
    """Return {entry: kept entry} for a status change that merged an entry (ADR 0082); else nothing."""
    kept = claim_value(value).get("kept") if agent == DEDUPLICATOR else None
    return {catalyst_id: UUID(kept)} if isinstance(kept, str) else {}


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
    status_source: dict[UUID, Rank] = {}
    status_agent: dict[UUID, str] = {}
    status_names: dict[UUID, frozenset[str]] = {}
    status_parties: dict[UUID, frozenset[str]] = {}
    status_into: dict[UUID, UUID] = {}
    changes = (
        select(
            change.c.catalyst_id,
            change.c.claim_id,
            change.c.field,
            change.c.learned_at,
            claim.c.agent,
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
        elif row.field == CatalystField.STATUS.value:  # the last one withdrew or closed it, if it is
            closer = row.agent == DEAL_CLOSER  # a closing dates from its completion document
            status_source[row.catalyst_id] = Rank(
                SourceTier(row.source_tier), row.published_at if closer else row.learned_at
            )
            status_agent[row.catalyst_id] = row.agent
            stated = claim_value(row.value) if closer else {}
            status_parties[row.catalyst_id] = _listed(stated, ("names", "deal_names"))  # its counterparties
            status_names[row.catalyst_id] = status_parties[row.catalyst_id] | _listed(stated, ("own_names",))
            status_into.update(_merged_into(row.catalyst_id, row.agent, row.value))
        elif row.field == CatalystField.TITLE.value:  # set once, by the claim that created it
            indication[row.catalyst_id] = normal_indication(claim_value(row.value).get("indication"))
    found: list[LiveCatalyst] = []
    withdrawn: list[LiveCatalyst] = []
    closed: list[LiveCatalyst] = []
    merged: list[LiveCatalyst] = []
    for row in session.execute(select(catalyst)):
        live = Catalyst.model_validate(row, from_attributes=True)
        gone = live.status is CatalystStatus.WITHDRAWN
        done = live.status is CatalystStatus.RESOLVED
        joined = live.status is CatalystStatus.MERGED
        if (done and status_agent.get(live.id) != DEAL_CLOSER) or (joined and live.id not in status_into):
            continue  # resolved or merged another way: left out
        source = (status_source if gone or done else window_source).get(live.id)
        if source is None:  # no change row: not written by the gate, so never matched to a claim
            continue
        key = Key(
            live.company_id,
            live.catalyst_type.value,
            frozenset(drugs[live.id]),
            frozenset(trials[live.id]),
            indication.get(live.id, ""),
        )
        (merged if joined else withdrawn if gone else closed if done else found).append(
            LiveCatalyst(
                live,
                key,
                source,
                last_learned[live.id],
                status_names.get(live.id, frozenset()),
                status_parties.get(live.id, frozenset()),
                status_into.get(live.id),
            )
        )
    merged = _set_aside_with_kept(merged, withdrawn, closed)
    return LiveIndex(cited=cited, catalysts=found, withdrawn=withdrawn, closed=closed, merged=merged)


def _set_aside_with_kept(
    merged: list[LiveCatalyst], withdrawn: list[LiveCatalyst], closed: list[LiveCatalyst]
) -> list[LiveCatalyst]:
    """Set each merged entry aside as its kept entry is, withdrawn or closed (Opus review, finding 3).

    It takes the kept entry's cutoff (when it was withdrawn, or when its completion document was
    published) and a closed deal's names, so a claim fitting only the merged entry is held by the
    same rules as ADR 0070 and ADR 0080. Return the merged entries whose kept entry is still open.
    """
    by_id = {c.row.id: c for c in merged}
    aside = {c.row.id: c for c in withdrawn} | {c.row.id: c for c in closed}
    gone = {c.row.id for c in withdrawn}
    still: list[LiveCatalyst] = []
    for entry in merged:
        target, seen = entry.into, {entry.row.id}
        while target is not None and target in by_id and target not in seen:  # merged again: follow it
            seen.add(target)
            target = by_id[target].into
        kept = aside.get(target) if target is not None else None
        if kept is None:
            still.append(entry)
            continue
        moved = replace(entry, source=kept.source, names=kept.names, parties=kept.parties)
        (withdrawn if kept.row.id in gone else closed).append(moved)
    return still


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
