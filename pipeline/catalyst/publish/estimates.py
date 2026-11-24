"""Estimated dates from the registry (owner decision 2026-11-23; spec: Date precision; ADR 0067).

Plain code, no model calls. For each covered company's trial with no company-stated date, the
registry's estimated primary completion date becomes a catalyst of its own, with the date label
`registry_estimate`, which the weekly issue prints as "Estimated".

Every value it writes rests on a registry-field claim (spec: Provenance, rule 9): the quote is the
field exactly as the stored copy of the trial's latest registry record writes it, for example
`"primaryCompletionDateStruct":{"date":"2027-03","type":"ESTIMATED"}`. Before it writes, plain code
checks the copy against its recorded hash, that the quote stands once in the copy's text at its
place, and that the field at that path holds that value.

- A trial counts when its latest stored registry record is linked to exactly one covered company
  by its exact lead sponsor (ADR 0005, ADR 0009), is recruiting or about to, or active, and is phase
  2 or later (spec: Catalyst types, topline readouts of phase 2 and later).
- A company-stated date always wins: a trial whose open published catalyst has a company or
  regulator date never gets an estimate, and an estimate already published is marked `replaced`,
  with a change row citing the claim behind the company's date.
- A newer estimate moves the window, each change citing the newer claim. A move of more than 6
  months, or one to a window that has passed, waits for a person (spec: Change sanity). A record
  that gives the actual date, or says the trial stopped, resolves the estimate.
- A window that has passed is never written; the read API never lists a passed window as upcoming.

Safe to re-run: claims are unique per record, field, and place, and a step happens only when what
the record says differs from what is published. It runs under the catalyst writer's lock.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

import httpx
from pydantic import JsonValue
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.extract.run import NEW_YORK
from catalyst.extract.text import TEXT_VERSION, document_text
from catalyst.logs import log_event
from catalyst.publish import gate
from catalyst.publish.sanity import too_far
from catalyst.publish.title import compose_title
from catalyst.publish.write import LOCK
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CatalystField,
    CatalystStatus,
    CatalystTrial,
    CatalystType,
    CheckOutcome,
    Claim,
    ClaimSubject,
    CompanyDocument,
    DateConfidence,
    DatePrecision,
    DocumentMatch,
    SourceDocument,
    Trial,
    TrialPhase,
)
from catalyst.snapshots import SnapshotStore, SnapshotStoreError, snapshot_key
from catalyst.verify.rules import CheckResult
from catalyst.verify.run import store_check

AGENT = "registry_field"
PROMPT = ("registry_field", "1.0.0")  # no prompt: the version of these plain-code rules
PROVIDER = "plain_code"
MODEL = "catalyst.publish.estimates"
CHECK = "registry_field_at_path"
COMPLETION_PATH = ("protocolSection", "statusModule", "primaryCompletionDateStruct")
STATUS_PATH = ("protocolSection", "statusModule", "overallStatus")
PHASES_PATH = ("protocolSection", "designModule", "phases")
ID_PATH = ("protocolSection", "identificationModule", "nctId")
ACTIVE = frozenset({"NOT_YET_RECRUITING", "RECRUITING", "ENROLLING_BY_INVITATION", "ACTIVE_NOT_RECRUITING"})
STOPPED = frozenset({"TERMINATED", "WITHDRAWN", "SUSPENDED"})
PHASE_2_OR_LATER = frozenset({"PHASE2", "PHASE3", "PHASE4"})
_PHASE_2_OR_LATER_ROWS = frozenset(
    phase.value
    for phase in (
        TrialPhase.PHASE_1_2,
        TrialPhase.PHASE_2,
        TrialPhase.PHASE_2_3,
        TrialPhase.PHASE_3,
        TrialPhase.PHASE_4,
    )
)
_OPEN = (CatalystStatus.UPCOMING, CatalystStatus.SLIPPED)
_COMPANY_DATES = (DateConfidence.COMPANY_CONFIRMED, DateConfidence.REGULATOR_SCHEDULED)
_MONTH = re.compile(r"(\d{4})-(\d{2})")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

COMPANY_DATED = "the trial has a company-stated date"
TOO_FAR = "the estimate would move more than 6 months"
PAST = "the new estimate has passed while the published one has not"

_log = logging.getLogger(__name__)


# --- The field and its quote -----------------------------------------------------------------


@dataclass(frozen=True)
class FieldQuote:
    """A registry field as the stored copy's text writes it, and where."""

    value: JsonValue
    quote: str
    start: int
    end: int


def at_path(record: Mapping[str, Any], path: Sequence[str]) -> JsonValue | None:
    """Return the value at `path` in the record, or None when any part of it is missing."""
    found: Any = record
    for part in path:
        if not isinstance(found, dict) or part not in found:
            return None
        found = cast(dict[str, Any], found)[part]
    return cast(JsonValue, found)


def field_quote(text: str, record: Mapping[str, Any], path: Sequence[str]) -> FieldQuote | None:
    """Return the field at `path` as the copy writes it, when its text stands exactly once in the copy.

    The collector stores each record as compact JSON with sorted keys (`ctgov.to_document`), so a
    field is its key and its value written the same way. Text that stands twice has no one place.
    """
    value = at_path(record, path)
    if value is None:
        return None
    quote = json.dumps({path[-1]: value}, sort_keys=True, separators=(",", ":"))[1:-1]
    start = text.find(quote)
    if start < 0 or text.find(quote, start + 1) >= 0:
        return None
    return FieldQuote(value, quote, start, start + len(quote))


def claim_field(path: Sequence[str]) -> str:
    """Return a registry-field claim's field: "registry:" and the field's path."""
    return "registry:" + ".".join(path)


def field_check(claim: Claim, text: str, record: Mapping[str, Any]) -> CheckResult:
    """Check a registry-field claim against the copy: its quote at its place, and the value at its path."""
    stated = claim.value if isinstance(claim.value, dict) else {}
    path = stated.get("path")
    if not isinstance(path, list) or not all(isinstance(part, str) for part in path) or not path:
        return CheckResult(CHECK, CheckOutcome.FAIL, "the claim names no field path")
    parts = [str(part) for part in cast(list[JsonValue], path)]
    if stated.get("nct_id") != at_path(record, ID_PATH):
        return CheckResult(CHECK, CheckOutcome.FAIL, "the record is another trial's")
    found = field_quote(text, record, parts)
    if found is None:
        return CheckResult(CHECK, CheckOutcome.FAIL, "the field is not written once in the stored copy")
    if (found.quote, found.start, found.end) != (claim.quote, claim.quote_start, claim.quote_end):
        return CheckResult(CHECK, CheckOutcome.FAIL, "the quote is not the field at its place")
    if found.value != stated.get("value"):
        return CheckResult(CHECK, CheckOutcome.FAIL, "the claim's value is not the field's value")
    return CheckResult(CHECK, CheckOutcome.PASS)


def completion_window(text: str) -> tuple[date, date, DatePrecision] | None:
    """Read the registry's date: "2027-03" is the whole month, "2027-03-15" the day; anything else none."""
    try:
        if _DAY.fullmatch(text):
            day = date.fromisoformat(text)
            return day, day, DatePrecision.DAY
        month = _MONTH.fullmatch(text)
        if month:
            year, number = int(month.group(1)), int(month.group(2))
            last = calendar.monthrange(year, number)[1]
            return date(year, number, 1), date(year, number, last), DatePrecision.MONTH
    except ValueError:
        return None
    return None


# --- What to do for one trial ------------------------------------------------------------------


class Step(StrEnum):
    """What the job does for one trial."""

    CREATE = "create"
    MOVE = "move"
    REPLACE = "replace"
    RESOLVE = "resolve"
    HOLD = "hold"
    NONE = "none"


@dataclass(frozen=True)
class RegistryFacts:
    """What the trial's latest registry record says: status, phases, and primary completion date."""

    status: str | None
    phases: tuple[str, ...]
    completion: Mapping[str, Any] | None


@dataclass(frozen=True)
class Estimate:
    """A published estimate entry: its status and its window."""

    status: CatalystStatus
    window: tuple[date, date]


def _estimated(facts: RegistryFacts) -> tuple[date, date, DatePrecision] | None:
    completion = facts.completion or {}
    when = completion.get("date")
    if completion.get("type") != "ESTIMATED" or not isinstance(when, str):
        return None
    return completion_window(when)


def _cannot_create(facts: RegistryFacts, today: date) -> str | None:
    if not PHASE_2_OR_LATER & set(facts.phases):
        return "the trial is not phase 2 or later"
    if facts.status not in ACTIVE:
        return "the trial is not recruiting or active"
    window = _estimated(facts)
    if window is None:
        return "the registry gives no estimated primary completion month or day"
    if window[1] < today:
        return "the estimated date has passed"
    return None


def next_step(
    facts: RegistryFacts, live: Estimate | None, company_dated: bool, today: date
) -> tuple[Step, str]:
    """Decide what to do for one trial, and say why in plain words."""
    if live is None:
        if company_dated:
            return Step.NONE, COMPANY_DATED
        why = _cannot_create(facts, today)
        return (
            (Step.NONE, why) if why else (Step.CREATE, "no company-stated date; the registry estimates one")
        )
    if live.status not in _OPEN:
        return Step.NONE, "the estimate entry was already replaced or resolved"
    if company_dated:
        return Step.REPLACE, "a company-stated date replaces the estimate"
    if (facts.completion or {}).get("type") == "ACTUAL":
        return Step.RESOLVE, "the registry gives the actual primary completion date"
    if facts.status in STOPPED:
        return Step.RESOLVE, "the registry says the trial has stopped"
    return _follow(facts, live, today)


def _follow(facts: RegistryFacts, live: Estimate, today: date) -> tuple[Step, str]:
    """Decide whether an open estimate entry follows the registry's newer estimate."""
    window = _estimated(facts)
    if window is None or facts.status not in ACTIVE:
        return Step.NONE, "the registry gives no estimate to follow"
    new = (window[0], window[1])
    if new == live.window:
        return Step.NONE, "the estimate is unchanged"
    if new[1] < today:
        return Step.HOLD, PAST
    if too_far(live.window, new):
        return Step.HOLD, TOO_FAR
    return Step.MOVE, "the registry moved its estimate"


# --- The job ------------------------------------------------------------------------------------


@dataclass
class EstimateReport:
    """What one run wrote, and why each trial it left alone was left."""

    trials_read: int = 0
    created: int = 0
    moved: int = 0
    replaced: int = 0
    resolved: int = 0
    copies_unreadable: int = 0
    held: Counter[str] = field(default_factory=Counter[str])
    left: Counter[str] = field(default_factory=Counter[str])

    def summary(self) -> dict[str, Any]:
        """Return the counts as plain JSON."""
        counts = {k: v for k, v in vars(self).items() if k not in ("held", "left")}
        return counts | {"estimates_held": dict(self.held), "estimates_left": dict(self.left.most_common(10))}


@dataclass(frozen=True)
class _Record:
    """A trial's latest stored registry record, read and checked against its hash."""

    document_id: UUID
    nct_id: str
    company_id: UUID
    text: str
    study: dict[str, Any]


@dataclass(frozen=True)
class _Live:
    row: Catalyst
    last_learned: datetime
    window_document: UUID  # the registry record behind its current window


def _latest_documents(session: Session) -> list[Any]:
    """Return the latest stored registry record of each trial: by last-update date, then publication."""
    document = TABLES[SourceDocument]
    rows = session.execute(
        select(
            document.c.id,
            document.c.source_key,
            document.c.snapshot_key,
            document.c.sha256,
            document.c.content_type,
            document.c.published_at,
        ).where(document.c.source_key.startswith("ctgov:"))
    ).all()
    latest: dict[str, Any] = {}
    for row in rows:
        parts = row.source_key.split(":")
        if len(parts) != 3:
            continue
        order = (parts[2], row.published_at, row.source_key)
        if parts[1] not in latest or order > latest[parts[1]][0]:
            latest[parts[1]] = (order, row)
    return [row for _, row in sorted(latest.values(), key=lambda item: item[1].source_key)]


def _sponsors(session: Session, ids: list[UUID]) -> dict[UUID, set[UUID]]:
    link = TABLES[CompanyDocument]
    found: defaultdict[UUID, set[UUID]] = defaultdict(set)
    query = select(link.c.source_document_id, link.c.company_id).where(
        link.c.source_document_id.in_(ids), link.c.matched_by == DocumentMatch.CTGOV_LEAD_SPONSOR
    )
    for row in session.execute(query):
        found[row.source_document_id].add(row.company_id)
    return found


def _read(store: SnapshotStore, row: Any, company_id: UUID) -> _Record | None:
    """Read the stored copy, refusing one whose bytes or key do not match its recorded hash."""
    try:
        content = store.get(row.snapshot_key)
    except (SnapshotStoreError, httpx.HTTPError, OSError, ValueError):
        return None
    if row.snapshot_key != snapshot_key(row.sha256) or hashlib.sha256(content).hexdigest() != row.sha256:
        return None
    try:
        study = json.loads(content)
        text = document_text(content, row.content_type)
    except (ValueError, UnicodeDecodeError):
        return None
    nct_id = row.source_key.split(":")[1]
    if not isinstance(study, dict) or at_path(cast(dict[str, Any], study), ID_PATH) != nct_id:
        return None
    return _Record(row.id, nct_id, company_id, text, cast(dict[str, Any], study))


def _facts(study: Mapping[str, Any]) -> RegistryFacts:
    status = at_path(study, STATUS_PATH)
    phases = at_path(study, PHASES_PATH)
    completion = at_path(study, COMPLETION_PATH)
    return RegistryFacts(
        status=status if isinstance(status, str) else None,
        phases=tuple(p for p in cast(list[Any], phases) if isinstance(p, str))
        if isinstance(phases, list)
        else (),
        completion=cast(dict[str, Any], completion) if isinstance(completion, dict) else None,
    )


def _estimates(session: Session) -> dict[UUID, _Live]:
    """Each trial's estimate entry, with its newest change time (one per trial)."""
    catalyst, link, change = TABLES[Catalyst], TABLES[CatalystTrial], TABLES[CatalystChange]
    newest = select(change.c.catalyst_id, func.max(change.c.learned_at).label("at"))
    learned: dict[UUID, datetime] = {
        row.catalyst_id: row.at for row in session.execute(newest.group_by(change.c.catalyst_id))
    }
    claim = TABLES[Claim]
    windows = (
        select(change.c.catalyst_id, claim.c.source_document_id)
        .join(claim, claim.c.id == change.c.claim_id)
        .where(change.c.field == CatalystField.WINDOW_START)
        .order_by(change.c.learned_at, change.c.id)
    )
    window_document = {row.catalyst_id: row.source_document_id for row in session.execute(windows)}
    query = (
        select(catalyst, link.c.trial_id)
        .join(link, link.c.catalyst_id == catalyst.c.id)
        .where(catalyst.c.catalyst_type == CatalystType.PRIMARY_COMPLETION)
    )
    found: dict[UUID, _Live] = {}
    for row in session.execute(query):
        live = Catalyst.model_validate(row, from_attributes=True)
        found[row.trial_id] = _Live(live, learned[live.id], window_document[live.id])
    return found


def _company_dates(session: Session) -> dict[tuple[UUID, UUID], UUID]:
    """For each (trial, company), the claim behind the newest open company or regulator date for it."""
    catalyst, link, change = TABLES[Catalyst], TABLES[CatalystTrial], TABLES[CatalystChange]
    query = (
        select(link.c.trial_id, catalyst.c.company_id, change.c.claim_id)
        .join(catalyst, catalyst.c.id == link.c.catalyst_id)
        .join(change, change.c.catalyst_id == catalyst.c.id)
        .where(
            catalyst.c.catalyst_type != CatalystType.PRIMARY_COMPLETION,
            catalyst.c.status.in_(_OPEN),
            catalyst.c.date_confidence.in_(_COMPANY_DATES),
            change.c.field == CatalystField.WINDOW_START,
        )
        .order_by(change.c.learned_at, change.c.id)
    )
    return {(row.trial_id, row.company_id): row.claim_id for row in session.execute(query)}  # newest wins


def _claim(session: Session, record: _Record, trial: Any, path: Sequence[str], at: datetime) -> UUID | None:
    """Store the registry-field claim for the field at `path`, checked first; return its id."""
    found = field_quote(record.text, record.study, path)
    if found is None:
        return None
    claim = Claim(
        source_document_id=record.document_id,
        subject_type=ClaimSubject.TRIAL,
        subject_id=trial.id,
        field=claim_field(path),
        value={
            "path": list(path),
            "value": found.value,
            "nct_id": record.nct_id,
            "text_version": TEXT_VERSION,
        },
        quote=found.quote,
        quote_start=found.start,
        quote_end=found.end,
        agent=AGENT,
        prompt_id=PROMPT[0],
        prompt_version=PROMPT[1],
        provider=PROVIDER,
        model=MODEL,
        extracted_at=at,
    )
    checks = (gate.match_quote(claim, record.text), field_check(claim, record.text, record.study))
    if any(result.outcome is not CheckOutcome.PASS for result in checks):
        return None
    table = TABLES[Claim]
    key = (
        "source_document_id",
        "field",
        "quote_start",
        "quote_end",
        "agent",
        "prompt_id",
        "prompt_version",
        "model",
    )
    session.execute(
        insert(table).values(**claim.model_dump()).on_conflict_do_nothing(index_elements=list(key))
    )
    values = claim.model_dump()
    stored = session.execute(select(table.c.id).where(*(table.c[k] == values[k] for k in key))).scalar_one()
    for result in checks:
        store_check(session, stored, result, at)
    return stored


def _change(
    catalyst_id: UUID, name: CatalystField, old: JsonValue, new: JsonValue, claim: UUID, at: datetime
) -> dict[str, Any]:
    return CatalystChange(
        catalyst_id=catalyst_id, field=name, old_value=old, new_value=new, claim_id=claim, learned_at=at
    ).model_dump()


def _create(session: Session, record: _Record, trial: Any, at: datetime) -> bool:
    claim = _claim(session, record, trial, COMPLETION_PATH, at)
    window = _estimated(_facts(record.study))
    if claim is None or window is None:
        return False
    start, end, precision = window
    row = Catalyst(
        catalyst_type=CatalystType.PRIMARY_COMPLETION,
        company_id=record.company_id,
        title=compose_title(CatalystType.PRIMARY_COMPLETION, [], [trial.study_name or trial.nct_id]),
        window_start=start,
        window_end=end,
        window_precision=precision,
        window_raw_text=str((_facts(record.study).completion or {})["date"]),
        date_confidence=DateConfidence.REGISTRY_ESTIMATE,
        status=CatalystStatus.UPCOMING,
    )
    session.execute(insert(TABLES[Catalyst]).values(**row.model_dump()))
    values = row.model_dump(mode="json")
    changes = [
        _change(row.id, name, None, values[name.value], claim, at)
        for name in CatalystField
        if name is not CatalystField.DRUGS and values[name.value] is not None
    ]
    session.execute(insert(TABLES[CatalystChange]).values(changes))
    link = CatalystTrial(catalyst_id=row.id, trial_id=trial.id, claim_id=claim)
    session.execute(insert(TABLES[CatalystTrial]).values(**link.model_dump()))
    log_event(_log, "estimate_created", catalyst_id=str(row.id), nct_id=record.nct_id)
    return True


def _update(
    session: Session, live: _Live, updated: Mapping[CatalystField, Any], claim: UUID, at: datetime
) -> None:
    """Change the entry's fields, each with a change row citing `claim`, after its newest change."""
    at = max(at, live.last_learned + timedelta(microseconds=1))
    old = live.row.model_dump(mode="json")
    new = live.row.model_copy(update={name.value: value for name, value in updated.items()}).model_dump(
        mode="json"
    )
    changed = [name for name in updated if old[name.value] != new[name.value]]
    table = TABLES[Catalyst]
    session.execute(
        update(table)
        .where(table.c.id == live.row.id)
        .values(**{name.value: updated[name] for name in changed})
    )
    rows = [_change(live.row.id, name, old[name.value], new[name.value], claim, at) for name in changed]
    session.execute(insert(TABLES[CatalystChange]).values(rows))


def _move(session: Session, record: _Record, trial: Any, live: _Live, at: datetime) -> bool:
    claim = _claim(session, record, trial, COMPLETION_PATH, at)
    window = _estimated(_facts(record.study))
    if claim is None or window is None:
        return False
    start, end, precision = window
    raw = str((_facts(record.study).completion or {})["date"])
    fields = {
        CatalystField.WINDOW_START: start,
        CatalystField.WINDOW_END: end,
        CatalystField.WINDOW_PRECISION: precision,
        CatalystField.WINDOW_RAW_TEXT: raw,
    }
    _update(session, live, fields, claim, at)
    log_event(_log, "estimate_moved", catalyst_id=str(live.row.id), nct_id=record.nct_id)
    return True


def _resolve(session: Session, record: _Record, trial: Any, live: _Live, at: datetime) -> bool:
    actual = (_facts(record.study).completion or {}).get("type") == "ACTUAL"
    claim = _claim(session, record, trial, COMPLETION_PATH if actual else STATUS_PATH, at)
    if claim is None:
        return False
    _update(session, live, {CatalystField.STATUS: CatalystStatus.RESOLVED}, claim, at)
    log_event(_log, "estimate_resolved", catalyst_id=str(live.row.id), nct_id=record.nct_id)
    return True


@dataclass
class _Run:
    session: Session
    store: SnapshotStore
    at: datetime
    report: EstimateReport
    trials: dict[str, Any]
    estimates: dict[UUID, _Live]
    company_dates: dict[tuple[UUID, UUID], UUID]


def _unchanged(trial: Any, live: _Live | None, document_id: UUID, company_dated: bool) -> str | None:
    """Say why the trial's step is already known without reading its copy, or None to read it.

    The trial table's phase comes from the same latest stored record (ADR 0039), so a trial it
    gives as earlier than phase 2 gets nothing; the copy's own phases decide every other trial.
    """
    if live is None:
        if company_dated:
            return COMPANY_DATED
        return None if trial.phase in _PHASE_2_OR_LATER_ROWS else "the trial is not phase 2 or later"
    if live.row.status not in _OPEN:
        return "the estimate entry was already replaced or resolved"
    if not company_dated and live.window_document == document_id:
        return "the estimate is unchanged"
    return None


def _one(run: _Run, row: Any, company_id: UUID) -> None:
    """Read one trial's latest record and take the step it calls for."""
    report, today = run.report, run.at.astimezone(NEW_YORK).date()
    trial = run.trials.get(row.source_key.split(":")[1])
    if trial is None:
        report.left["the trial is not in the trial table"] += 1
        return
    live = run.estimates.get(trial.id)
    company_claim = run.company_dates.get((trial.id, company_id))
    known = _unchanged(trial, live, row.id, company_claim is not None)
    if known is not None:
        report.left[known] += 1  # nothing the record says could change it: no copy is read
        return
    record = _read(run.store, row, company_id)
    if record is None:
        report.copies_unreadable += 1
        log_event(_log, "estimate_copy_unreadable", document_id=str(row.id))
        return
    report.trials_read += 1
    estimate = Estimate(live.row.status, (live.row.window_start, live.row.window_end)) if live else None
    step, reason = next_step(_facts(record.study), estimate, company_claim is not None, today)
    done = False
    if step is Step.CREATE:
        done = _create(run.session, record, trial, run.at)
        report.created += done
    elif step is Step.MOVE and live is not None:
        done = _move(run.session, record, trial, live, run.at)
        report.moved += done
    elif step is Step.REPLACE and live is not None and company_claim is not None:
        _update(run.session, live, {CatalystField.STATUS: CatalystStatus.REPLACED}, company_claim, run.at)
        log_event(_log, "estimate_replaced", catalyst_id=str(live.row.id), claim_id=str(company_claim))
        report.replaced += 1
        done = True
    elif step is Step.RESOLVE and live is not None:
        done = _resolve(run.session, record, trial, live, run.at)
        report.resolved += done
    elif step is Step.HOLD:
        report.held[reason] += 1
        log_event(_log, "estimate_held", nct_id=record.nct_id, reason=reason)
        return
    if not done:
        report.left[reason if step is Step.NONE else "the field is not written once in the stored copy"] += 1


def publish_estimates(
    session_factory: sessionmaker[Session], store: SnapshotStore, now: Callable[[], datetime]
) -> EstimateReport:
    """Write, move, replace, or resolve each covered trial's estimate entry, in one transaction."""
    report = EstimateReport()
    with session_factory.begin() as session:
        session.execute(select(func.pg_advisory_xact_lock(LOCK)))
        documents = _latest_documents(session)
        sponsors = _sponsors(session, [row.id for row in documents])
        trial = TABLES[Trial]
        run = _Run(
            session=session,
            store=store,
            at=now(),
            report=report,
            trials={
                row.nct_id: row
                for row in session.execute(
                    select(trial.c.id, trial.c.nct_id, trial.c.study_name, trial.c.phase)
                )
            },
            estimates=_estimates(session),
            company_dates=_company_dates(session),
        )
        for row in documents:
            companies = sponsors.get(row.id, set())
            if len(companies) != 1:
                report.left["the record is not linked to exactly one covered company"] += 1
                continue
            _one(run, row, next(iter(companies)))
    log_event(_log, "estimates_published", **report.summary())
    return report
