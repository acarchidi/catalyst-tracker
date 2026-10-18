"""The second-reading job: the verifier reads each release once; plain code compares (ADR 0018, part 2).

It runs only when the verifier's prompt has its own scores (the caller passes the gate's
problems). Each release the extractor read is read once per verifier prompt version and model,
and the reading is kept even when it found nothing, so a re-run spends nothing. Every
extracted catalyst on a read release then gets a `second_reading_agrees` row.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import httpx
from sqlalchemy import and_, delete, exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.extract.agent import ROLE, catalyst_claims, extract_catalysts
from catalyst.extract.evaluate import VERIFY_EVALS, gate_problems
from catalyst.extract.run import NEW_YORK
from catalyst.extract.text import document_text, visible_text
from catalyst.llm.client import ModelClient, ModelError, SpendCapReachedError
from catalyst.llm.config import ModelConfig
from catalyst.llm.prompts import load_prompt
from catalyst.logs import log_event
from catalyst.references import load_reference_data
from catalyst.resolve import ReferenceData, Status
from catalyst.schemas import Claim, ClaimCheck, CompanyDocument, DocumentReading, SourceDocument
from catalyst.snapshots import SnapshotStore, SnapshotStoreError
from catalyst.verify.rules import Outcome
from catalyst.verify.run import store_check
from catalyst.verify.second import CHECK, VERIFIER_PROMPT, VERIFIER_ROLE, Reading, compare

_log = logging.getLogger(__name__)


@dataclass
class SecondReadingReport:
    """What one run did."""

    skipped: list[str] = field(default_factory=list[str])
    documents_read: int = 0
    documents_failed: int = 0
    claims_compared: int = 0
    claims_agreeing: int = 0
    stopped: str | None = None

    def summary(self) -> dict[str, Any]:
        """Return the counts as plain JSON."""
        return {
            "skipped": self.skipped,
            "documents_read": self.documents_read,
            "documents_failed": self.documents_failed,
            "claims_compared": self.claims_compared,
            "claims_agreeing": self.claims_agreeing,
            "stopped_early": self.stopped,
        }


def second_reading_problems(config: ModelConfig) -> list[str]:
    """Return why the second reading may not run yet; empty once its scores match (no exemption)."""
    prompt = load_prompt(*VERIFIER_PROMPT)
    return gate_problems(prompt, config.roles[VERIFIER_ROLE], VERIFY_EVALS, allow_unscored=False)


def current_reading(model: str) -> Any:
    """Select the verifier's readings made with its current prompt and with `model`."""
    reading = TABLES[DocumentReading]
    return and_(
        reading.c.agent == VERIFIER_ROLE,
        reading.c.prompt_id == VERIFIER_PROMPT[0],
        reading.c.prompt_version == VERIFIER_PROMPT[1],
        reading.c.model == model,
    )


def _extracted() -> Any:
    claim = TABLES[Claim]
    return and_(claim.c.agent == ROLE, claim.c.field.startswith("catalyst:"))


def _to_read(session: Session, model: str, limit: int) -> list[Any]:
    documents, claim, reading = TABLES[SourceDocument], TABLES[Claim], TABLES[DocumentReading]
    has_claims = exists().where(claim.c.source_document_id == documents.c.id, _extracted())
    done = exists().where(reading.c.source_document_id == documents.c.id, current_reading(model))
    query = select(documents).where(has_claims, ~done).order_by(documents.c.published_at.desc()).limit(limit)
    return list(session.execute(query).all())


def _record(session: Session, document_id: UUID, model: str, claims: list[Claim], at: datetime) -> None:
    """Store the verifier's claims and the reading itself, together."""
    if claims:
        session.execute(
            insert(TABLES[Claim]).values([c.model_dump() for c in claims]).on_conflict_do_nothing()
        )
    row = DocumentReading(
        source_document_id=document_id,
        agent=VERIFIER_ROLE,
        prompt_id=VERIFIER_PROMPT[0],
        prompt_version=VERIFIER_PROMPT[1],
        model=model,
        catalysts=len(claims),
        read_at=at,
    )
    session.execute(insert(TABLES[DocumentReading]).values(**row.model_dump()).on_conflict_do_nothing())


def _value(raw: object) -> dict[str, Any]:
    return cast(dict[str, Any], raw) if isinstance(raw, dict) else {}


def _reading(row: Any) -> Reading:
    value = _value(row.value)
    window = (value.get("window_start"), value.get("window_end"), value.get("window_precision"))
    return Reading(
        row.id, str(value.get("catalyst_type")), value.get("drug"), window, row.quote_start, row.quote_end
    )


def _clean(row: Any) -> bool:
    """Say whether a verifier proposal may vouch: only when its own checks found no problem."""
    return _value(row.value).get("problems") == []


def same_drug_rule(references: ReferenceData, company: UUID | None) -> Callable[[str, str], bool]:
    """Say whether two names are one drug of the document's one company in the reference tables."""

    def same(a: str, b: str) -> bool:
        if company is None:
            return False
        first, second = references.resolve_drug(a, company), references.resolve_drug(b, company)
        return first.status is second.status is Status.RESOLVED and first.id == second.id

    return same


def _clear_stale(session: Session, model: str) -> None:
    """Remove second-reading outcomes that no current reading supports (review of PR #34).

    After the verifier's prompt or model changes, a release keeps no outcome until the new
    reading reads it: the publish gate counts a missing row as not passing.
    """
    check, claim, reading = TABLES[ClaimCheck], TABLES[Claim], TABLES[DocumentReading]
    current = select(reading.c.source_document_id).where(current_reading(model))
    stale = select(claim.c.id).where(_extracted(), claim.c.source_document_id.not_in(current))
    session.execute(delete(check).where(check.c.check_name == CHECK, check.c.claim_id.in_(stale)))


def _compare_all(session: Session, model: str, at: datetime, report: SecondReadingReport) -> None:
    claim, reading, links = TABLES[Claim], TABLES[DocumentReading], TABLES[CompanyDocument]
    read = select(reading.c.source_document_id).where(current_reading(model))
    columns = (
        claim.c.id,
        claim.c.source_document_id,
        claim.c.agent,
        claim.c.value,
        claim.c.quote_start,
        claim.c.quote_end,
    )
    # A verifier claim counts only if this prompt and model made it. ModelClient refuses an answer
    # from any other model, so a claim's model is always the one its reading asked for.
    verifier = and_(
        claim.c.agent == VERIFIER_ROLE,
        claim.c.prompt_id == VERIFIER_PROMPT[0],
        claim.c.prompt_version == VERIFIER_PROMPT[1],
        claim.c.model == model,
    )
    rows = session.execute(
        select(*columns, claim.c.prompt_id, claim.c.prompt_version, claim.c.model).where(
            claim.c.source_document_id.in_(read), _extracted() | verifier
        )
    ).all()
    # Each extractor version's claims are compared on their own, so an older copy of an event
    # never competes with the current one for the same verifier claim (review of PR #34).
    first: defaultdict[tuple[UUID, str, str, str], list[Reading]] = defaultdict(list)
    second: defaultdict[UUID, list[Reading]] = defaultdict(list)
    for row in rows:
        if row.agent == ROLE:
            first[(row.source_document_id, row.prompt_id, row.prompt_version, row.model)].append(
                _reading(row)
            )
        elif _clean(row):
            second[row.source_document_id].append(_reading(row))
    companies: defaultdict[UUID, set[UUID]] = defaultdict(set)
    for row in session.execute(select(links.c.source_document_id, links.c.company_id)):
        companies[row.source_document_id].add(row.company_id)
    references = load_reference_data(session)
    for (document, *_), readings in first.items():
        owners = companies.get(document, set())
        company = next(iter(owners)) if len(owners) == 1 else None
        for claim_id, result in compare(
            readings, second[document], same_drug_rule(references, company)
        ).items():
            store_check(session, claim_id, result, at)
            report.claims_compared += 1
            report.claims_agreeing += result.outcome is Outcome.PASS


def run_second_reading(
    session_factory: sessionmaker[Session],
    store: SnapshotStore,
    client: ModelClient,
    config: ModelConfig,
    *,
    limit: int,
    now: Callable[[], datetime],
    gate: Callable[[ModelConfig], list[str]] = second_reading_problems,
) -> SecondReadingReport:
    """Read up to `limit` extracted releases a second time, then compare every read release.

    The verifier's model and gate come from `config`, the same config `client` calls with. When
    the gate reports a problem, nothing is read, and outcomes from older readings are removed.
    """
    model = config.roles[VERIFIER_ROLE].model
    problems = gate(config)
    report = SecondReadingReport(skipped=problems)
    with session_factory.begin() as session:
        _clear_stale(session, model)
    if problems:
        log_event(_log, "second_reading_skipped", problems=problems)
        return report
    with session_factory() as session:
        documents = _to_read(session, model, limit)
    for row in documents:
        published = row.published_at.astimezone(NEW_YORK).date()
        try:
            content = store.get(row.snapshot_key)
            reading = extract_catalysts(
                client,
                document_text(content, row.content_type),
                published,
                visible=visible_text(content, row.content_type),
                role=VERIFIER_ROLE,
                prompt_ref=VERIFIER_PROMPT,
            )
        except SpendCapReachedError as error:
            report.stopped = str(error)
            break
        except (ModelError, SnapshotStoreError, httpx.HTTPError) as error:
            report.documents_failed += 1
            log_event(_log, "second_reading_failed", source_key=row.source_key, error=type(error).__name__)
            continue
        at = now()
        claims = catalyst_claims(reading, UUID(str(row.id)), at, agent=VERIFIER_ROLE)
        with session_factory.begin() as session:
            _record(session, UUID(str(row.id)), model, claims, at)
        report.documents_read += 1
    with session_factory.begin() as session:
        _compare_all(session, model, now(), report)
    log_event(_log, "second_reading_done", **report.summary())
    return report
