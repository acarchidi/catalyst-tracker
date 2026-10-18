"""The catalyst writer: the only job that writes live catalysts (ADR 0018, part 5b; spec: Agent architecture).

Plain code, no model calls. For every claim the decision job marked to publish, it checks again
and decides again in one pass, then writes what a fresh publish allows, the way the read API
reads it (ADR 0025): the catalyst, a change row for every value it sets, each citing the claim,
and drug and trial links naming that same claim.

Recomputed here, because they are plain code over stored rows and can go stale:
- every rule check (`catalyst.verify.rules.RULES`), as of today in New York;
- `independent_source`, which depends on other documents' claims;
- `advisory_meeting_noticed`, since a notice may have been amended or cancelled;
- `safe_to_write` (`catalyst.publish.sanity`), against the catalysts as this run leaves them;
- the quote match against the stored copy.

Read as stored: every other check on the gate's list. Today those are `second_reading_agrees`,
which needs a model call, and counts only when its document has a reading from the current
verifier prompt and model; and `company_named_in_source`, a job of its own (part 3d). The entity
matches and registry phases the rules read are stored inputs, renewed by their own jobs.

The recomputed outcomes that differ from the stored ones are stored, so the decision job and the
review queue see what the writer saw, including every reason it held a claim.

Safe to re-run: a claim a change row already cites is done, and the job holds a database lock
while it runs, so two runs never both create one catalyst. Everything is written in one
transaction, so a failure leaves nothing half written. It never deletes or unpublishes.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from itertools import groupby
from typing import Any
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
    Place,
    Rank,
    claim_key,
    claim_value,
    excuse_passed_window,
    load_live,
    place,
    ranks_above,
)
from catalyst.publish.sanity import Candidate, drug_role, safe_to_write, same_dates
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
    ClaimDecision,
    DocumentReading,
    DrugRole,
    GateDecision,
    SourceDocument,
    SourceTier,
)
from catalyst.snapshots import SnapshotStore
from catalyst.verify import advisory, independent, second
from catalyst.verify.advisory import advisory_results
from catalyst.verify.rules import CheckResult
from catalyst.verify.run import check_results, load_matches, store_check
from catalyst.verify.second_run import current_reading

__all__ = ["PublishReport", "publish_claims", "ranks_above"]

TOP_REASONS = 10
LOCK = 0x43_41_54_31  # one writer at a time: pg_advisory_xact_lock key ("CAT1")
DATE_FIELDS = (
    CatalystField.WINDOW_START,
    CatalystField.WINDOW_END,
    CatalystField.WINDOW_PRECISION,
    CatalystField.WINDOW_RAW_TEXT,
    CatalystField.DATE_CONFIDENCE,
)
INVALID = "the claim's values do not make a valid catalyst"
_DECIDED_WHILE_WRITING = "decided as the writer places the claim"

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
    checks_updated: int = 0
    reasons: Counter[str] = field(default_factory=Counter[str])

    def summary(self) -> dict[str, Any]:
        """Return the counts as plain JSON, with the most common reasons a claim was not written."""
        counts = {k: v for k, v in vars(self).items() if k != "reasons"}
        return counts | {"top_reasons": dict(self.reasons.most_common(TOP_REASONS))}


@dataclass(frozen=True)
class _Ready:
    """A claim that passed every check again just now, with the event it states."""

    claim: Claim
    candidate: Candidate
    key: Key

    def order(self) -> tuple[object, ...]:
        """Sort best first: rank, then the later reading, then the id, so ties break the same way."""
        rank = self.candidate.rank
        return (
            tuple(SourceTier).index(rank.tier),
            -rank.published_at.timestamp(),
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


def _read_now(session: Session, verifier_model: str) -> set[UUID]:
    """Documents the verifier has read with its current prompt and model (Opus review of PR #59, O2)."""
    reading = TABLES[DocumentReading]
    query = select(reading.c.source_document_id).where(current_reading(verifier_model))
    return set(session.execute(query).scalars())


def _row(claim_id: UUID, result: CheckResult, at: datetime) -> ClaimCheck:
    reason = result.reason[:300] if result.reason else None
    return ClaimCheck(
        claim_id=claim_id, check_name=result.check, outcome=result.outcome, reason=reason, checked_at=at
    )


def _checks(
    claim_id: UUID, fresh: Sequence[CheckResult], stored: Iterable[ClaimCheck], read_now: bool, at: datetime
) -> list[ClaimCheck]:
    """Return the fresh outcomes, plus the stored row of each required check this job does not recompute.

    `safe_to_write` passes here: the writer decides it as it places the claim. A stored second
    reading counts only when its document has a current reading.
    """
    recomputed = {*gate.RULE_CHECKS, independent.CHECK, advisory.CHECK, gate.WRITER_CHECK}
    rows = [_row(claim_id, result, at) for result in fresh]
    rows.append(_row(claim_id, CheckResult(gate.WRITER_CHECK, CheckOutcome.PASS, _DECIDED_WHILE_WRITING), at))
    kept = [
        r
        for r in stored
        if r.check_name in gate.REQUIRED_CHECKS
        and r.check_name not in recomputed
        and (r.check_name != second.CHECK or read_now)
    ]
    return rows + kept


@dataclass
class _Run:
    """One run's state: what to store, and the report."""

    at: datetime
    report: PublishReport
    results: dict[UUID, list[CheckResult]] = field(default_factory=dict[UUID, list[CheckResult]])


def _recheck(
    session: Session, store: SnapshotStore, rows: list[Any], live: LiveIndex, verifier_model: str, run: _Run
) -> list[_Ready]:
    """Check and decide each claim again; hold only the current document's text while doing it."""
    report, at = run.report, run.at
    matches = load_matches(session)
    ids = {row.id for row in rows}
    fresh = check_results(session, at.astimezone(NEW_YORK).date(), matches)
    noticed = advisory_results(session, store, ids)
    stored = _stored(session, ids)
    read_now = _read_now(session, verifier_model)
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
            run.results[claim.id] = results
            checks = _checks(claim.id, results, stored[claim.id], claim.source_document_id in read_now, at)
            if live.is_live(claim.id, key):
                checks = excuse_passed_window(claim.id, checks, at)
            decision = gate.decide(claim, checks, gate.match_quote(claim, copy))
            if decision.decision is not GateDecision.PUBLISH:
                report.held_on_recheck += 1
                report.reasons.update(hold.label for hold in decision.holds)
                continue
            rank = Rank(SourceTier(row.source_tier), row.published_at)
            candidate = Candidate(claim_value(claim.value), claim.quote, key, rank)
            if key is None:  # no single company: `safe_to_write` says why
                result = safe_to_write(candidate, Place(), at.astimezone(NEW_YORK).date())
                results.append(result)
                report.held_by_writer += 1
                report.reasons[result.reason or ""] += 1
                continue
            ready.append(_Ready(claim, candidate, key))
        del copy
    return ready


def _hold(run: _Run, ready: _Ready, reason: str) -> None:
    run.report.held_by_writer += 1
    run.report.reasons[reason] += 1
    log_event(_log, "claim_held_by_writer", claim_id=str(ready.claim.id), reason=reason)


def _candidate(ready: _Ready, catalyst_id: UUID | None = None) -> Catalyst:
    value = ready.candidate.value
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


def _write_links(
    session: Session, catalyst_id: UUID, ready: _Ready, at: datetime, report: PublishReport
) -> None:
    """Link the drugs and trials the claim names, each link naming that claim, and record `drugs`."""
    key = ready.key
    claim_id = ready.claim.id
    drug = ready.candidate.value.get("drug")
    role = drug_role(ready.claim.quote, drug if isinstance(drug, str) else None) or DrugRole.ALONE
    links = [
        CatalystDrug(catalyst_id=catalyst_id, drug_id=d, role=role, claim_id=claim_id) for d in key.drugs
    ]
    trials = [CatalystTrial(catalyst_id=catalyst_id, trial_id=t, claim_id=claim_id) for t in key.trials]
    if links:
        session.execute(insert(TABLES[CatalystDrug]).values([link.model_dump() for link in links]))
        listed = sorted((str(link.drug_id), link.role.value) for link in links)
        drugs: JsonValue = [{"drug_id": d, "role": kind} for d, kind in listed]
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


def _create(session: Session, ready: _Ready, run: _Run) -> LiveCatalyst | None:
    try:
        new = _candidate(ready)
    except ValidationError:
        return None
    at, report = run.at, run.report
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
    return LiveCatalyst(new, ready.key, ready.candidate.rank, at)


def _move(
    session: Session, live: LiveCatalyst, ready: _Ready, fields: Sequence[CatalystField], run: _Run
) -> LiveCatalyst | None:
    """Change a live catalyst's date fields to the claim's, with a change row citing the claim for each."""
    try:
        moved = _candidate(ready, live.row.id)
    except ValidationError:
        return None
    # The read API takes the newest row of each field as its value, so a change always comes later.
    at = max(run.at, live.last_learned + timedelta(microseconds=1))
    old, new = live.row.model_dump(mode="json"), moved.model_dump(mode="json")
    changed = [name for name in fields if old[name.value] != new[name.value]]
    updated = {name.value: getattr(moved, name.value) for name in changed}
    session.execute(update(TABLES[Catalyst]).where(TABLES[Catalyst].c.id == live.row.id).values(**updated))
    rows = [_change(live.row.id, n, old[n.value], new[n.value], ready.claim.id, at) for n in changed]
    session.execute(insert(TABLES[CatalystChange]).values(rows))
    run.report.changes_written += len(rows)
    run.report.catalysts_updated += 1
    log_event(_log, "catalyst_changed", catalyst_id=str(live.row.id), claim_id=str(ready.claim.id))
    window_moved = any(name in changed for name in DATE_FIELDS[:3])
    source = ready.candidate.rank if window_moved else live.source
    return replace(live, row=live.row.model_copy(update=updated), source=source, last_learned=at)


def _write(session: Session, ready: list[_Ready], live: LiveIndex, run: _Run) -> None:
    """Place each claim, best first, against the catalysts as this run leaves them, and write it."""
    today = run.at.astimezone(NEW_YORK).date()
    catalysts = list(live.catalysts)
    for item in sorted(ready, key=_Ready.order):
        where = place(item.key, catalysts)
        result = safe_to_write(item.candidate, where, today)
        if result.outcome is CheckOutcome.PASS:
            result = _apply(session, item, where.catalyst, catalysts, run) or result
        run.results.setdefault(item.claim.id, []).append(result)
        if result.outcome is CheckOutcome.FAIL:
            _hold(run, item, result.reason or "")


def _apply(
    session: Session, item: _Ready, live: LiveCatalyst | None, catalysts: list[LiveCatalyst], run: _Run
) -> CheckResult | None:
    """Write one claim that is safe to write; return a failure only when its values make no catalyst."""
    if live is None:
        created = _create(session, item, run)
        if created is None:
            return CheckResult(gate.WRITER_CHECK, CheckOutcome.FAIL, INVALID)
        catalysts.append(created)
        return None
    if not ranks_above(
        (item.candidate.rank.tier, item.candidate.rank.published_at),
        (live.source.tier, live.source.published_at),
    ):
        run.report.not_written_lower_rank += 1
        return None
    same = same_dates(live, item.candidate.value)
    if same and live.row.date_confidence.value == item.candidate.value.get("date_confidence"):
        run.report.not_written_same_dates += 1
        return None
    moved = _move(session, live, item, (CatalystField.DATE_CONFIDENCE,) if same else DATE_FIELDS, run)
    if moved is None:
        return CheckResult(gate.WRITER_CHECK, CheckOutcome.FAIL, INVALID)
    catalysts[catalysts.index(live)] = moved
    return None


def _store_changed(session: Session, results: dict[UUID, list[CheckResult]], at: datetime) -> int:
    """Store each recomputed outcome that differs from the stored one; return how many changed."""
    stored = _stored(session, results)
    changed = 0
    for claim_id, claim_results in results.items():
        before = {r.check_name: (r.outcome, r.reason) for r in stored[claim_id]}
        for result in claim_results:
            row = _row(claim_id, result, at)
            if before.get(row.check_name) != (row.outcome, row.reason):
                store_check(session, claim_id, result, at)
                changed += 1
    return changed


def publish_claims(
    session_factory: sessionmaker[Session],
    store: SnapshotStore,
    now: Callable[[], datetime],
    *,
    verifier_model: str,
) -> PublishReport:
    """Check again, decide again, and write the live catalysts every fresh publish allows.

    `verifier_model` is the second reading's configured model: only its readings vouch.
    """
    run = _Run(at=now(), report=PublishReport())
    report = run.report
    with session_factory.begin() as session:
        session.execute(select(func.pg_advisory_xact_lock(LOCK)))
        rows = _marked(session)
        report.claims_marked_publish = len(rows)
        live = load_live(session)
        todo = [row for row in rows if row.id not in live.cited]
        report.already_published = len(rows) - len(todo)
        if todo:
            _write(session, _recheck(session, store, todo, live, verifier_model, run), live, run)
            report.checks_updated = _store_changed(session, run.results, run.at)
    log_event(_log, "claims_published", **report.summary())
    return report
