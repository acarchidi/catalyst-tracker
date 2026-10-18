"""The catalyst writer: the only job that writes live catalysts (ADR 0018, part 5b; spec: Agent architecture).

Plain code, no model calls. For every claim the decision job marked to publish, it checks again
and decides again in one pass, then writes what a fresh publish allows, the way the read API
reads it (ADR 0025): the catalyst, a change row for every value it sets, each citing the claim,
and drug and trial links naming that same claim.

Recomputed here, because they are plain code over stored rows and can go stale:
- every rule check (`catalyst.verify.rules.RULES`), as of today in New York;
- `independent_source`, which depends on other documents' claims;
- `advisory_meeting_noticed`, since a notice may have been amended or cancelled;
- the quote match against the stored copy.

Read as stored: every other check on the gate's list. Today those are `second_reading_agrees`,
which needs a model call (its job removes outcomes no current reading supports), and
`company_named_in_source`, a job of its own (part 3d). The entity
matches and registry phases the rules read are stored inputs, renewed by their own jobs.

A claim the fresh checks hold, or one the writer cannot place safely (a window that does not
overlap the live one, a move of more than 6 months), gets a review decision with its reasons.

Safe to re-run: a claim a change row already cites is done, and the job holds a database lock
while it runs, so two runs never both create one catalyst. Everything is written in one
transaction, so a failure leaves nothing half written. It never deletes or unpublishes.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import groupby
from typing import Any, cast
from uuid import UUID

from pydantic import JsonValue, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.extract.agent import ROLE
from catalyst.extract.run import NEW_YORK
from catalyst.logs import log_event
from catalyst.publish import gate
from catalyst.publish.live import (
    Key,
    LiveCatalyst,
    LiveIndex,
    Rank,
    claim_key,
    excuse_passed_window,
    load_live,
    ranks_above,
)
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CatalystDrug,
    CatalystField,
    CatalystStatus,
    CatalystTrial,
    Claim,
    ClaimCheck,
    ClaimDecision,
    DrugRole,
    GateDecision,
    SourceDocument,
    SourceTier,
)
from catalyst.snapshots import SnapshotStore
from catalyst.verify import advisory, independent
from catalyst.verify.advisory import advisory_results
from catalyst.verify.rules import CheckResult, months_after
from catalyst.verify.run import check_results, load_matches

__all__ = ["PublishReport", "publish_claims", "ranks_above"]

TOP_REASONS = 10
MOVE_MONTHS = 6  # spec, Change sanity: a date that moves more than 6 months always goes to review
LOCK = 0x43_41_54_31  # one writer at a time: pg_advisory_xact_lock key ("CAT1")
DATE_FIELDS = (
    CatalystField.WINDOW_START,
    CatalystField.WINDOW_END,
    CatalystField.WINDOW_PRECISION,
    CatalystField.WINDOW_RAW_TEXT,
    CatalystField.DATE_CONFIDENCE,
)
NOT_OVERLAPPING = "the window does not overlap the published one"
TOO_FAR = "the date would move more than 6 months"
SEVERAL = "the claim matches more than one published catalyst"
_COMBINATION = re.compile(r"\bcombination\b", re.IGNORECASE)

_log = logging.getLogger(__name__)


@dataclass
class PublishReport:
    """What one run wrote, and why each claim it did not write waits."""

    claims_marked_publish: int = 0
    already_published: int = 0
    held_on_recheck: int = 0
    held_by_writer: int = 0
    not_written_lower_rank: int = 0
    not_written_same_dates: int = 0
    catalysts_created: int = 0
    catalysts_updated: int = 0
    changes_written: int = 0
    drug_links_written: int = 0
    trial_links_written: int = 0
    copies_unreadable: int = 0
    reasons: Counter[str] = field(default_factory=Counter[str])
    held: dict[UUID, list[str]] = field(default_factory=dict[UUID, list[str]])  # claim -> why it waits

    def summary(self) -> dict[str, Any]:
        """Return the counts as plain JSON, with the most common reasons a claim was not written."""
        counts = {k: v for k, v in vars(self).items() if k not in ("reasons", "held")}
        return counts | {"top_reasons": dict(self.reasons.most_common(TOP_REASONS))}


@dataclass(frozen=True)
class _Ready:
    """A claim that passed every check again just now, with the event it states."""

    claim: Claim
    key: Key
    rank: Rank
    live: tuple[LiveCatalyst, ...]

    @property
    def window(self) -> tuple[date, date]:
        value = cast(dict[str, Any], self.claim.value)
        return date.fromisoformat(value["window_start"]), date.fromisoformat(value["window_end"])

    def order(self) -> tuple[object, ...]:
        """Sort best first: rank, then the later reading, then the id, so ties break the same way."""
        return (
            tuple(SourceTier).index(self.rank.tier),
            -self.rank.published_at.timestamp(),
            -self.claim.extracted_at.timestamp(),
            str(self.claim.id),
        )


def _marked(session: Session) -> list[Any]:
    """Every extractor claim the decision job marked to publish, grouped by document."""
    claim, document, decision = TABLES[Claim], TABLES[SourceDocument], TABLES[ClaimDecision]
    query = (
        select(
            claim,
            document.c.snapshot_key,
            document.c.sha256,
            document.c.content_type,
            document.c.source_tier,
            document.c.published_at,
        )
        .join(document, document.c.id == claim.c.source_document_id)
        .join(decision, decision.c.claim_id == claim.c.id)
        .where(
            claim.c.agent == ROLE,
            claim.c.field.startswith("catalyst:"),
            decision.c.decision == GateDecision.PUBLISH.value,
        )
        .order_by(claim.c.source_document_id, claim.c.extracted_at, claim.c.id)
    )
    return list(session.execute(query))


def _stored(session: Session, ids: Iterable[UUID]) -> defaultdict[UUID, list[ClaimCheck]]:
    check = TABLES[ClaimCheck]
    stored: defaultdict[UUID, list[ClaimCheck]] = defaultdict(list)
    for row in session.execute(select(check).where(check.c.claim_id.in_(list(ids)))).mappings():
        stored[row["claim_id"]].append(ClaimCheck.model_validate(dict(row)))
    return stored


def _row(claim_id: UUID, result: CheckResult, at: datetime) -> ClaimCheck:
    reason = result.reason[:300] if result.reason else None
    return ClaimCheck(
        claim_id=claim_id, check_name=result.check, outcome=result.outcome, reason=reason, checked_at=at
    )


def _checks(
    claim_id: UUID, fresh: Sequence[CheckResult], stored: Iterable[ClaimCheck], at: datetime
) -> list[ClaimCheck]:
    """Return the fresh outcomes, plus the stored row of each required check this job does not recompute."""
    recomputed = {*gate.RULE_CHECKS, independent.CHECK, advisory.CHECK}
    rows = [_row(claim_id, result, at) for result in fresh]
    kept = [r for r in stored if r.check_name in gate.REQUIRED_CHECKS and r.check_name not in recomputed]
    return rows + kept


def _recheck(
    session: Session,
    store: SnapshotStore,
    rows: list[Any],
    live: LiveIndex,
    at: datetime,
    report: PublishReport,
) -> list[_Ready]:
    """Check and decide each claim again; hold only the current document's text while doing it."""
    today = at.astimezone(NEW_YORK).date()
    matches = load_matches(session)
    ids = {row.id for row in rows}
    fresh = check_results(session, today, matches)
    noticed = advisory_results(session, store, ids)
    stored = _stored(session, ids)
    names = list(TABLES[Claim].c.keys())
    ready: list[_Ready] = []
    for _, group in groupby(rows, key=lambda row: row.source_document_id):
        group = list(group)
        first = group[0]
        copy = gate.read_copy(store, first.snapshot_key, first.sha256, first.content_type)
        report.copies_unreadable += isinstance(copy, gate.UnreadableCopy)
        for row in group:
            claim = Claim.model_validate({name: getattr(row, name) for name in names})
            key = claim_key(claim.value, matches.get(claim.id, []))
            results = [*fresh.get(claim.id, []), *([noticed[claim.id]] if claim.id in noticed else [])]
            checks = _checks(claim.id, results, stored[claim.id], at)
            if live.is_live(claim.id, key):
                checks = excuse_passed_window(claim.id, checks, at)
            decision = gate.decide(claim, checks, gate.match_quote(claim, copy))
            if decision.decision is not GateDecision.PUBLISH or key is None:
                unmatched = "no single company is matched"
                report.held_on_recheck += 1
                report.reasons.update([hold.label for hold in decision.holds] or [unmatched])
                report.held[claim.id] = list(decision.reasons) or [unmatched]
                continue
            rank = Rank(SourceTier(row.source_tier), row.published_at)
            ready.append(_Ready(claim, key, rank, tuple(live.about(key))))
        del copy
    return ready


def _hold(report: PublishReport, ready: _Ready, reason: str) -> None:
    report.held_by_writer += 1
    report.reasons[reason] += 1
    report.held[ready.claim.id] = [reason]
    log_event(_log, "claim_held_by_writer", claim_id=str(ready.claim.id), reason=reason)


def _overlap(a: tuple[date, date], b: tuple[date, date]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def _too_far(old: tuple[date, date], new: tuple[date, date]) -> bool:
    """Say whether a move takes either end more than 6 months; a narrowing never does."""
    if old[0] <= new[0] and new[1] <= old[1]:
        return False
    return any(
        moved > months_after(was, MOVE_MONTHS) or moved < months_after(was, -MOVE_MONTHS)
        for was, moved in zip(old, new, strict=True)
    )


def _candidate(ready: _Ready, catalyst_id: UUID | None = None) -> Catalyst:
    value = cast(dict[str, Any], ready.claim.value)
    fields: dict[str, Any] = {
        "catalyst_type": ready.key.catalyst_type,
        "company_id": ready.key.company_id,
        "title": value.get("title"),
        "window_start": value.get("window_start"),
        "window_end": value.get("window_end"),
        "window_precision": value.get("window_precision"),
        "window_raw_text": value.get("date_text"),
        "date_confidence": value.get("date_confidence"),
        "status": CatalystStatus.UPCOMING,
    }
    return Catalyst.model_validate(fields | ({"id": catalyst_id} if catalyst_id else {}))


def _change(
    catalyst_id: UUID, name: CatalystField, old: JsonValue, new: JsonValue, claim_id: UUID, at: datetime
) -> dict[str, Any]:
    row = CatalystChange(
        catalyst_id=catalyst_id, field=name, old_value=old, new_value=new, claim_id=claim_id, learned_at=at
    )
    return row.model_dump()


def _role(quote: str) -> DrugRole:
    """Return the drug's role: the extractor names none yet, so a quote saying "combination" marks it."""
    return DrugRole.IN_COMBINATION if _COMBINATION.search(quote) else DrugRole.ALONE


def _write_links(
    session: Session, catalyst_id: UUID, ready: _Ready, at: datetime, report: PublishReport
) -> None:
    """Link the drugs and trials the claim names, each link naming that claim, and record `drugs`."""
    claim_id, role = ready.claim.id, _role(ready.claim.quote)
    links = [
        CatalystDrug(catalyst_id=catalyst_id, drug_id=d, role=role, claim_id=claim_id)
        for d in ready.key.drugs
    ]
    trials = [CatalystTrial(catalyst_id=catalyst_id, trial_id=t, claim_id=claim_id) for t in ready.key.trials]
    if links:
        session.execute(insert(TABLES[CatalystDrug]).values([link.model_dump() for link in links]))
        listed = sorted((str(link.drug_id), link.role.value) for link in links)
        drugs: JsonValue = [{"drug_id": drug, "role": kind} for drug, kind in listed]
        session.execute(
            insert(TABLES[CatalystChange]).values(
                _change(catalyst_id, CatalystField.DRUGS, None, drugs, claim_id, at)
            )
        )
        report.changes_written += 1
    if trials:
        session.execute(insert(TABLES[CatalystTrial]).values([trial.model_dump() for trial in trials]))
    report.drug_links_written += len(links)
    report.trial_links_written += len(trials)


def _create(session: Session, ready: _Ready, at: datetime, report: PublishReport) -> None:
    try:
        new = _candidate(ready)
    except ValidationError:
        _hold(report, ready, "the claim's values do not make a valid catalyst")
        return
    session.execute(insert(TABLES[Catalyst]).values(**new.model_dump()))
    values = new.model_dump(mode="json")
    changes = [
        _change(new.id, name, None, values[name.value], ready.claim.id, at)
        for name in CatalystField
        if name is not CatalystField.DRUGS and values[name.value] is not None
    ]
    session.execute(insert(TABLES[CatalystChange]).values(changes))
    report.changes_written += len(changes)
    _write_links(session, new.id, ready, at, report)
    report.catalysts_created += 1
    log_event(_log, "catalyst_created", catalyst_id=str(new.id), claim_id=str(ready.claim.id))


def _move(session: Session, live: LiveCatalyst, ready: _Ready, at: datetime, report: PublishReport) -> None:
    """Move a live catalyst's date to the claim's, with a change row citing the claim for each field."""
    try:
        moved = _candidate(ready, live.row.id)
    except ValidationError:
        _hold(report, ready, "the claim's values do not make a valid catalyst")
        return
    # The read API takes the newest row of each field as its value, so a move always comes later.
    at = max(at, live.last_learned + timedelta(microseconds=1))
    old, new = live.row.model_dump(mode="json"), moved.model_dump(mode="json")
    changed = [name for name in DATE_FIELDS if old[name.value] != new[name.value]]
    updated = {name.value: getattr(moved, name.value) for name in changed}
    session.execute(update(TABLES[Catalyst]).where(TABLES[Catalyst].c.id == live.row.id).values(**updated))
    rows = [_change(live.row.id, n, old[n.value], new[n.value], ready.claim.id, at) for n in changed]
    session.execute(insert(TABLES[CatalystChange]).values(rows))
    report.changes_written += len(rows)
    report.catalysts_updated += 1
    log_event(_log, "catalyst_date_moved", catalyst_id=str(live.row.id), claim_id=str(ready.claim.id))


def _same_dates(live: LiveCatalyst, ready: _Ready) -> bool:
    value = cast(dict[str, Any], ready.claim.value)
    row = live.row
    return (row.window_start.isoformat(), row.window_end.isoformat(), row.window_precision.value) == (
        value.get("window_start"),
        value.get("window_end"),
        value.get("window_precision"),
    )


def _new_event(session: Session, claims: list[_Ready], at: datetime, report: PublishReport) -> None:
    """Create the catalyst from the best-ranked claim; the others lose, or wait if their dates disagree."""
    winner, *others = sorted(claims, key=_Ready.order)
    _create(session, winner, at, report)
    for other in others:
        if not _overlap(other.window, winner.window):
            _hold(report, other, NOT_OVERLAPPING)
        else:
            report.not_written_lower_rank += 1


def _known_event(
    session: Session, live: LiveCatalyst, claims: list[_Ready], at: datetime, report: PublishReport
) -> None:
    """Move the date when the best overlapping claim outranks the live one's source and the move is sane."""
    decided = False
    for ready in sorted(claims, key=_Ready.order):
        if not _overlap(ready.window, live.window):
            _hold(report, ready, NOT_OVERLAPPING)
        elif decided or not ranks_above(
            (ready.rank.tier, ready.rank.published_at), (live.source.tier, live.source.published_at)
        ):
            report.not_written_lower_rank += 1
        else:
            decided = True
            if _same_dates(live, ready):
                report.not_written_same_dates += 1
            elif _too_far(live.window, ready.window):
                _hold(report, ready, TOO_FAR)
            else:
                _move(session, live, ready, at, report)


def _write(session: Session, ready: list[_Ready], at: datetime, report: PublishReport) -> None:
    new: defaultdict[Key, list[_Ready]] = defaultdict(list)
    known: defaultdict[UUID, list[_Ready]] = defaultdict(list)
    catalysts: dict[UUID, LiveCatalyst] = {}
    for item in ready:
        if len(item.live) > 1:
            _hold(report, item, SEVERAL)
        elif item.live:
            catalysts[item.live[0].row.id] = item.live[0]
            known[item.live[0].row.id].append(item)
        else:
            new[item.key].append(item)
    for claims in new.values():
        _new_event(session, claims, at, report)
    for catalyst_id, claims in known.items():
        _known_event(session, catalysts[catalyst_id], claims, at, report)


def _store_holds(session: Session, held: dict[UUID, list[str]], at: datetime) -> None:
    """Keep each held claim's fresh reasons as its decision, so the review queue shows them."""
    table = TABLES[ClaimDecision]
    for claim_id, reasons in held.items():
        decision = ClaimDecision(
            claim_id=claim_id, decision=GateDecision.REVIEW, reasons=reasons, decided_at=at
        )
        session.execute(
            update(table)
            .where(table.c.claim_id == claim_id)
            .values(decision=decision.decision, reasons=decision.reasons, decided_at=at)
        )


def publish_claims(
    session_factory: sessionmaker[Session], store: SnapshotStore, now: Callable[[], datetime]
) -> PublishReport:
    """Check again, decide again, and write the live catalysts every fresh publish allows."""
    report = PublishReport()
    at = now()
    with session_factory.begin() as session:
        session.execute(select(func.pg_advisory_xact_lock(LOCK)))
        rows = _marked(session)
        report.claims_marked_publish = len(rows)
        live = load_live(session)
        todo = [row for row in rows if row.id not in live.cited]
        report.already_published = len(rows) - len(todo)
        if todo:
            _write(session, _recheck(session, store, todo, live, at, report), at, report)
            _store_holds(session, report.held, at)
    log_event(_log, "claims_published", **report.summary())
    return report
