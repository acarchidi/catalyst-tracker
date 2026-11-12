"""The indication job: both readers on each published catalyst that has no indication yet (ADR 0053).

For each published catalyst with one drug tested alone and no indication, it reads the source
claim that states the event, offers the readers the stored copy's visible sentences that name
the drug, and keeps what they find as a claim: the sentence is the quote, the MeSH disease and
the sentence's own words are the value. It never writes a catalyst; the writer does (ADR 0018,
part 5b). Every try is kept in `indication_attempt`, found or refused, so a re-run never pays to
ask again about the same catalyst, claim, prompts, and models.

It runs only when `indication_gate_problems` finds nothing: scores for exactly these prompts,
models, and label sets that meet their floors. The readers have no tools and no database
access; plain code reads and writes (spec: Agent architecture). A role at its daily cap stops
the job (ADR 0051).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.api.read import published, read_session
from catalyst.api.views import CatalystDetail, SourceView
from catalyst.collectors.sources.mesh import stored_index
from catalyst.db.tables import TABLES
from catalyst.extract.agent import ROLE
from catalyst.extract.disease import (
    CHECKER_PROMPT,
    READER_PROMPT,
    Outcome,
    find_indication,
    indication_gate_problems,
)
from catalyst.extract.indication import Found, IndicationMiss
from catalyst.extract.text import TEXT_VERSION, document_text, read_page
from catalyst.llm.client import InputTooLargeError, ModelClient, ModelError, SpendCapReachedError
from catalyst.llm.config import load_config
from catalyst.logs import log_event
from catalyst.mesh import MeshIndex
from catalyst.publish.live import claim_value
from catalyst.schemas import (
    CheckOutcome,
    Claim,
    ClaimCheck,
    ClaimSubject,
    DrugRole,
    IndicationAttempt,
    IndicationResult,
    SourceDocument,
)
from catalyst.snapshots import SnapshotStore, SnapshotStoreError
from catalyst.verify.second import VERIFIER_ROLE

_log = logging.getLogger(__name__)

FIELD_PREFIX = "indication:"  # then the catalyst's id, so two catalysts never share one claim row
CONFIRMED_CHECK = "indication_confirmed"  # the second reader's verdict, kept beside the claim
# A later try kept the same sentence with another disease: the stored claim's value stays as it
# was, so the try is refused rather than pointed at a claim stating something else (review of PR 127).
REDRAWN = "an earlier run kept another disease for this sentence"
EVENT_FIELD = "catalyst_type"  # the source that states the event is the one that gives its type


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class IndicationReport:
    """What one run did: how many catalysts it looked at, found, refused, and why it stopped."""

    blocked: list[str] = field(default_factory=list[str])
    considered: int = 0
    already_tried: int = 0
    found: int = 0
    refused: dict[str, int] = field(default_factory=dict[str, int])
    failed: int = 0
    stopped: str | None = None

    def summary(self) -> dict[str, Any]:
        """Return the counts for the job's JSON report; no source text."""
        return {
            "blocked": self.blocked,
            "considered": self.considered,
            "already_tried": self.already_tried,
            "found": self.found,
            "refused": dict(sorted(self.refused.items())),
            "failed": self.failed,
            "stopped": self.stopped,
        }


@dataclass(frozen=True)
class _Task:
    """One catalyst to ask about, with the source claim and copy the readers read."""

    catalyst_id: UUID
    claim_id: UUID
    quote: str
    drug: str
    document_id: UUID
    snapshot_key: str
    content_type: str


def _event_source(detail: CatalystDetail) -> SourceView | None:
    return next((s for s in detail.sources if EVENT_FIELD in s.fields), None)


def _one_drug_alone(detail: CatalystDetail) -> bool:
    return len(detail.drugs) == 1 and detail.drugs[0].role is DrugRole.ALONE


def _tried(session: Session, reader_model: str, checker_model: str) -> set[tuple[UUID, UUID]]:
    table = TABLES[IndicationAttempt]
    query = select(table.c.catalyst_id, table.c.claim_id).where(
        table.c.reader_prompt_version == READER_PROMPT[1],
        table.c.checker_prompt_version == CHECKER_PROMPT[1],
        table.c.reader_model == reader_model,
        table.c.checker_model == checker_model,
    )
    return {(row.catalyst_id, row.claim_id) for row in session.execute(query)}


def _sources(session: Session, claim_ids: Sequence[UUID]) -> dict[UUID, Any]:
    claim, document = TABLES[Claim], TABLES[SourceDocument]
    query = (
        select(
            claim.c.id,
            claim.c.value,
            document.c.id.label("document_id"),
            document.c.snapshot_key,
            document.c.content_type,
        )
        .join(document, document.c.id == claim.c.source_document_id)
        .where(claim.c.id.in_(claim_ids))
    )
    return {row.id: row for row in session.execute(query)}


def _drug(value: object) -> str | None:
    """Return the drug as the release writes it, from the event claim's value."""
    drug = claim_value(value).get("drug")
    return drug if isinstance(drug, str) and drug.strip() else None


def claim_field(catalyst_id: UUID) -> str:
    """Return the field of a catalyst's indication claim: one per catalyst, as event claims are."""
    return f"{FIELD_PREFIX}{catalyst_id}"


def _attempt(
    task: _Task,
    outcome: IndicationResult,
    reason: str | None,
    claim_id: UUID | None,
    models: tuple[str, str],
    at: datetime,
) -> IndicationAttempt:
    return IndicationAttempt(
        catalyst_id=task.catalyst_id,
        claim_id=task.claim_id,
        outcome=outcome,
        reason=reason,
        indication_claim_id=claim_id,
        reader_prompt_version=READER_PROMPT[1],
        checker_prompt_version=CHECKER_PROMPT[1],
        reader_model=models[0],
        checker_model=models[1],
        attempted_at=at,
    )


def indication_claim(
    task: _Task, found: Found, provider: str, models: tuple[str, str], at: datetime
) -> Claim:
    """Return the claim that holds a found indication: its sentence is the quote (spec: Provenance)."""
    value: dict[str, Any] = {
        "mesh_id": found.descriptor.ui,
        "mesh_name": found.descriptor.name,
        "words": found.words,  # what is printed: the sentence's own words, never the MeSH name
        "drug": task.drug,
        "text_version": TEXT_VERSION,  # the offsets hold only for this text version (gate.match_quote)
        "checked_by": {
            "prompt_id": CHECKER_PROMPT[0],
            "prompt_version": CHECKER_PROMPT[1],
            "model": models[1],
        },
    }
    return Claim(
        source_document_id=task.document_id,
        subject_type=ClaimSubject.CATALYST,
        subject_id=task.catalyst_id,
        field=claim_field(task.catalyst_id),
        value=value,
        quote=found.sentence.quote,
        quote_start=found.sentence.start,
        quote_end=found.sentence.end,
        agent=ROLE,
        prompt_id=READER_PROMPT[0],
        prompt_version=READER_PROMPT[1],
        provider=provider,
        model=models[0],
        extracted_at=at,
    )


def found_mesh(claim: Claim) -> object:
    """Return the MeSH identifier a kept indication claim states."""
    return claim_value(claim.value).get("mesh_id")


def _store(
    session: Session, task: _Task, outcome: Outcome, provider: str, models: tuple[str, str], at: datetime
) -> str | None:
    """Keep one try, and for a found indication its claim and the second reader's pass, together.

    Return why no indication stands, or None when one does.
    """
    if not isinstance(outcome.result, Found):
        reason = str(outcome.result)
        row = _attempt(task, IndicationResult.REFUSED, reason, None, models, at)
        session.execute(insert(TABLES[IndicationAttempt]).values(**row.model_dump()).on_conflict_do_nothing())
        return reason
    claim = indication_claim(task, outcome.result, provider, models, at)
    table = TABLES[Claim]
    inserted = session.execute(
        insert(table).values(**claim.model_dump()).on_conflict_do_nothing().returning(table.c.id)
    ).scalar_one_or_none()
    claim_id: UUID
    if inserted is not None:
        claim_id = inserted
    else:  # the same sentence was kept by an earlier run: point at that claim
        key = TABLES[Claim].c
        earlier = session.execute(
            select(key.id, key.value).where(
                key.source_document_id == claim.source_document_id,
                key.field == claim.field,
                key.quote_start == claim.quote_start,
                key.quote_end == claim.quote_end,
                key.prompt_version == claim.prompt_version,
                key.model == claim.model,
                key.agent == claim.agent,
                key.prompt_id == claim.prompt_id,
            )
        ).one()
        if claim_value(earlier.value).get("mesh_id") != found_mesh(claim):
            row = _attempt(task, IndicationResult.REFUSED, REDRAWN, None, models, at)
            session.execute(
                insert(TABLES[IndicationAttempt]).values(**row.model_dump()).on_conflict_do_nothing()
            )
            return REDRAWN
        claim_id = earlier.id
    check = ClaimCheck(
        claim_id=claim_id, check_name=CONFIRMED_CHECK, outcome=CheckOutcome.PASS, checked_at=at
    )
    session.execute(insert(TABLES[ClaimCheck]).values(**check.model_dump()).on_conflict_do_nothing())
    row = _attempt(task, IndicationResult.FOUND, None, claim_id, models, at)
    session.execute(insert(TABLES[IndicationAttempt]).values(**row.model_dump()).on_conflict_do_nothing())
    return None


def _tasks(session: Session, report: IndicationReport, models: tuple[str, str]) -> list[_Task]:
    details = [d for d in published(session) if d.indication is None]
    tried = _tried(session, *models)
    picked: list[tuple[CatalystDetail, SourceView]] = []
    for detail in details:
        source = _event_source(detail)
        if source is None or not _one_drug_alone(detail):
            continue
        if (detail.id, source.claim_id) in tried:
            report.already_tried += 1
            continue
        picked.append((detail, source))
    rows = _sources(session, [s.claim_id for _, s in picked])
    tasks: list[_Task] = []
    for detail, source in picked:
        row = rows.get(source.claim_id)
        drug = _drug(row.value) if row is not None else None
        if row is None or drug is None:
            continue
        tasks.append(
            _Task(
                detail.id,
                source.claim_id,
                source.quote,
                drug,
                row.document_id,
                row.snapshot_key,
                row.content_type,
            )
        )
    return tasks


def indicate_catalysts(
    session_factory: sessionmaker[Session],
    store: SnapshotStore,
    client: ModelClient,
    *,
    limit: int,
    index: MeshIndex | None = None,
    now: Callable[[], datetime] = _utc_now,
    gate: Callable[[], list[str]] = indication_gate_problems,
) -> IndicationReport:
    """Ask both readers about up to `limit` published catalysts without an indication; keep each try."""
    report = IndicationReport(blocked=gate())
    if report.blocked:
        return report  # no model is called without scores that meet their floors
    if index is None:
        with session_factory() as session:
            index = stored_index(session, store)
        if index is None:
            report.blocked = ["no MeSH descriptor file is stored yet; run `catalyst collect --sources mesh`"]
            return report
    models = (client.model_for(ROLE), client.model_for(VERIFIER_ROLE))
    provider = load_config().roles[ROLE].provider
    with read_session(session_factory) as session:
        tasks = _tasks(session, report, models)
    for task in tasks[:limit]:
        report.considered += 1
        try:
            content = store.get(task.snapshot_key)
            text = document_text(content, task.content_type)
            page = read_page(content, task.content_type)
            if page.differs:
                outcome = Outcome(IndicationMiss.COPY_DIFFERS)  # such a copy's claims go to a person
            else:
                outcome = find_indication(client, text, page.visible, task.drug, task.quote, index)
        except SpendCapReachedError as error:
            report.considered -= 1
            report.stopped = str(error)
            break
        except (InputTooLargeError, ModelError, SnapshotStoreError, httpx.HTTPError) as error:
            report.failed += 1
            log_event(
                _log, "indication_failed", catalyst_id=str(task.catalyst_id), error=type(error).__name__
            )
            continue
        with session_factory.begin() as session:
            reason = _store(session, task, outcome, provider, models, now())
        if reason is None:
            report.found += 1
        else:
            report.refused[reason] = report.refused.get(reason, 0) + 1
    log_event(_log, "indications_read", **{k: v for k, v in report.summary().items() if k != "refused"})
    return report
