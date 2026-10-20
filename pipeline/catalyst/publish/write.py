"""The catalyst writer: the only job that writes live catalysts (ADR 0018, part 5b; spec: Agent architecture).

Plain code, no model calls. For every claim the decision job marked to publish, it checks again
and decides again in one pass, then writes what a fresh publish allows, the way the read API
reads it (ADR 0025): the catalyst, a change row for every value it sets, each citing the claim,
and drug and trial links naming that same claim.

Recomputed here, because they are plain code over stored rows and can go stale:
- every rule check (`catalyst.verify.rules.RULES`), as of today in New York;
- `independent_source`, which depends on other documents' claims;
- `advisory_meeting_noticed`, since a notice may have been amended or cancelled;
- `drug_named_near_quote` (part 3e), against the stored copy this job reads anyway, since new
  drug records and the release's later claims can change it; when the copy cannot be read, its
  stored row stands and the quote match holds the claim;
- `type_named_near_quote` (part 5d), which reads the copy only for a quote that names no kind of
  event;
- `safe_to_write` (`catalyst.publish.sanity`), against the catalysts as this run leaves them;
- the quote match against the stored copy.

Read as stored: every other check on the gate's list. Today those are `second_reading_agrees`,
which needs a model call, and counts only when its document has a reading from the current
verifier prompt and model; and `company_named_in_source`, a job of its own (part 3d). The entity
matches and registry phases the rules read are stored inputs, renewed by their own jobs.

The recomputed outcomes that differ from the stored ones are stored, so the decision job and the
review queue see what the writer saw, including every reason it held a claim.

The title it writes is composed in code from checked fields (`catalyst.publish.title`), never the
extractor's own words; its change row cites the claim whose checked fields it names (part 5d).

A claim a person approved in the review queue (ADR 0042) is checked again like any other; the
approval excuses only the holds the person saw (`gate.approve`), never the quote match, and of
the writer's own holds only a date move or a window that does not overlap, and only when no
other date reason fails with it (`person_may_approve`).

Safe to re-run: a claim a change row already cites is done, and the job holds a database lock
while it runs, so two runs never both create one catalyst. Everything is written in one
transaction, so a failure leaves nothing half written. It never deletes or unpublishes.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from itertools import groupby
from typing import Any
from uuid import UUID

from pydantic import JsonValue
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
from catalyst.publish.sanity import (
    PASSED,
    Candidate,
    drug_names,
    drug_role,
    load_ties,
    others,
    person_may_approve,
    safe_to_write,
    same_dates,
)
from catalyst.publish.title import TitleNames, load_title_names
from catalyst.review import approvals
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
from catalyst.verify import advisory, event_type, independent, second
from catalyst.verify.advisory import advisory_results
from catalyst.verify.drug import DrugFacts, load_drug_facts
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
    published_by_approval: int = 0  # claims a person approved that this run wrote
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
    title: str | None  # composed from checked fields; None when a matched record has no name on file

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
    recomputed |= {result.check for result in fresh}  # the drug check, when the copy could be read
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
    approved: dict[UUID, frozenset[str]] = field(default_factory=dict[UUID, frozenset[str]])
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
    names_on_file = drug_names(session)
    drugs = load_drug_facts(session)
    ties = load_ties(session, matches, live.cited)
    titles = load_title_names(session)
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
            results += [*_drug_result(drugs, claim, copy), _type_result(claim, copy)]
            run.results[claim.id] = results
            checks = _checks(claim.id, results, stored[claim.id], claim.source_document_id in read_now, at)
            if live.is_live(claim.id, key):
                checks = excuse_passed_window(claim.id, checks, at)
            decision = gate.decide(claim, checks, gate.match_quote(claim, copy))
            if claim.id in run.approved:
                decision = gate.approve(decision, run.approved[claim.id], person_may_approve)
            if decision.decision is not GateDecision.PUBLISH:
                report.held_on_recheck += 1
                report.reasons.update(hold.label for hold in decision.holds)
                continue
            rank = Rank(SourceTier(row.source_tier), row.published_at)
            candidate = Candidate(
                claim_value(claim.value), claim.quote, key, rank, others(names_on_file, key), claim.id in ties
            )
            if key is None:  # no single company: `safe_to_write` says why
                result = safe_to_write(candidate, Place(), at.astimezone(NEW_YORK).date())
                results.append(result)
                report.held_by_writer += 1
                report.reasons[result.reason or ""] += 1
                continue
            ready.append(_Ready(claim, candidate, key, _title(titles, key)))
        del copy
    return ready


def _drug_result(drugs: DrugFacts, claim: Claim, copy: str | gate.UnreadableCopy) -> list[CheckResult]:
    """Check again that the claim's drug is named in or just before its quote (part 3e).

    Without a readable copy its stored row stands; the quote match holds the claim anyway.
    """
    if isinstance(copy, gate.UnreadableCopy):
        return []
    span = (claim.quote, claim.quote_start, claim.quote_end)
    return [drugs.check(claim.id, claim.source_document_id, claim.value, span, copy)]


def _type_result(claim: Claim, copy: str | gate.UnreadableCopy) -> CheckResult:
    """Check again that the quote states the claim's type (part 5d); the copy only for a bare row."""
    text = None if isinstance(copy, gate.UnreadableCopy) else copy
    kind = claim_value(claim.value).get("catalyst_type")
    return event_type.type_named(text, claim.quote, claim.quote_start, claim.quote_end, kind)


def _title(titles: TitleNames, key: Key) -> str | None:
    """Compose the title from the claim's checked fields, or None when one has no name on file."""
    try:
        return titles.title(key.catalyst_type, key.drugs, key.trials)
    except (KeyError, ValueError):
        return None


def _hold(run: _Run, ready: _Ready, reason: str) -> None:
    run.report.held_by_writer += 1
    run.report.reasons[reason] += 1
    log_event(_log, "claim_held_by_writer", claim_id=str(ready.claim.id), reason=reason)


def _candidate(ready: _Ready, catalyst_id: UUID | None = None, title: str | None = None) -> Catalyst:
    """Build the catalyst the claim states; a ValueError when its values make none (no title, say).

    `title` is the live catalyst's, for a move: the title is written once, by the creating claim.
    """
    title = title or ready.title
    if title is None:
        raise ValueError("a matched drug or trial has no name on file for the title")
    value = ready.candidate.value
    fields: dict[str, Any] = {
        "catalyst_type": ready.key.catalyst_type,
        "company_id": ready.key.company_id,
        "title": title,
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
    stated = drug if isinstance(drug, str) else None
    role = drug_role(ready.claim.quote, stated, ready.candidate.other_drugs)
    if role is None and key.drugs:
        # `safe_to_write` holds an unclear role; reaching here is a bug, and a guess must never be
        # stored as if a quote said it, so the whole run stops and writes nothing (Claude review).
        raise RuntimeError(f"claim {claim_id}: the drug role is unclear and was not held")
    links = [
        CatalystDrug(catalyst_id=catalyst_id, drug_id=d, role=role or DrugRole.ALONE, claim_id=claim_id)
        for d in key.drugs
    ]
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
    report.drug_links_written += len(links)
    _link_trials(session, catalyst_id, ready, report)


def _link_trials(session: Session, catalyst_id: UUID, ready: _Ready, report: PublishReport) -> None:
    """Link each trial the claim names to the catalyst, each link naming the claim."""
    trials = [
        CatalystTrial(catalyst_id=catalyst_id, trial_id=t, claim_id=ready.claim.id) for t in ready.key.trials
    ]
    if trials:
        session.execute(insert(TABLES[CatalystTrial]).values([trial.model_dump() for trial in trials]))
    report.trial_links_written += len(trials)


def _create(session: Session, ready: _Ready, run: _Run) -> LiveCatalyst | None:
    try:
        new = _candidate(ready)
    except ValueError:  # pydantic's ValidationError is one too
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
        moved = _candidate(ready, live.row.id, live.row.title)
    except ValueError:  # pydantic's ValidationError is one too
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
    key = live.key
    if not key.trials and ready.key.trials:  # the trial is now stated: later claims about others are not this
        _link_trials(session, live.row.id, ready, run.report)
        key = replace(key, trials=ready.key.trials)
    row = live.row.model_copy(update=updated)
    return replace(live, row=row, key=key, source=ready.candidate.rank, last_learned=at)


def _write(session: Session, ready: list[_Ready], live: LiveIndex, run: _Run) -> None:
    """Place each claim, best first, against the catalysts as this run leaves them, and write it."""
    today = run.at.astimezone(NEW_YORK).date()
    catalysts = list(live.catalysts)
    for item in sorted(ready, key=_Ready.order):
        where = place(item.key, catalysts)
        result = safe_to_write(item.candidate, where, today)
        if result.outcome is CheckOutcome.PASS and where.catalyst is None and _ended(item, today):
            # `window_not_past` may have been excused because the claim fitted a live catalyst when
            # the run began; an earlier write in this run can make it a new event instead, and a new
            # catalyst whose window has ended is never published (Claude review of PR #59).
            result = CheckResult(gate.WRITER_CHECK, CheckOutcome.FAIL, PASSED)
        if result.outcome is CheckOutcome.PASS or _approved(run, item, result):
            written = _written(run.report)
            result = _apply(session, item, where.catalyst, catalysts, run) or result
            if item.claim.id in run.approved and _written(run.report) > written:
                run.report.published_by_approval += 1
                log_event(_log, "approved_claim_written", claim_id=str(item.claim.id))
        run.results.setdefault(item.claim.id, []).append(result)  # an approved hold is stored as found
        if result.outcome is CheckOutcome.FAIL and not _approved(run, item, result):
            _hold(run, item, result.reason or "")


def _written(report: PublishReport) -> int:
    return report.catalysts_created + report.catalysts_updated


def _approved(run: _Run, item: _Ready, result: CheckResult) -> bool:
    """Say whether a person approved this writer hold word for word, and every part needs only a yes.

    The reason lists every failing date check and both windows, so an approval of an older text,
    or of one reason among others, lifts nothing (Opus review of PR #84, B1 and B2).
    """
    seen = run.approved.get(item.claim.id, frozenset())
    reason = result.reason or ""
    return person_may_approve(reason) and f"{gate.WRITER_CHECK}: {reason}" in seen


def _ended(item: _Ready, today: date) -> bool:
    end = item.candidate.value.get("window_end")
    return isinstance(end, str) and date.fromisoformat(end) < today


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
        # A better source confirms the dates: nothing to write, but it now stands behind them, so a
        # weaker claim later in this run cannot move them. It is never cited, so every run places it
        # again, best first, before any weaker claim (round 2, N1).
        catalysts[catalysts.index(live)] = replace(live, source=item.candidate.rank)
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
        run.approved = approvals(session)
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
