"""The extraction job: run the extractor over stored press releases and keep the checked claims.

Plain code around the agent: it reads stored copies, calls the agent, and stores claims (never
catalysts; only the publish gate will write those). Safe to re-run: a document that already
has claims from this prompt version is skipped, and a claim is stored once (ADR 0013).

A read that finds nothing is kept as a reading, so a re-run skips it too until the prompt version
changes. Three kinds of copy never reach the model, each kept as a reading with its reason: one
that is not a web page or plain text (a PDF copy), one with under `MIN_WORDS` readable words, and
one over the extractor's input limit (ADR 0038).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from catalyst.db.tables import TABLES
from catalyst.extract.agent import (
    EXTRACTOR_PROMPT,
    ROLE,
    ExtractedCatalyst,
    catalyst_claims,
    extract_catalysts,
    proposal_value,
)
from catalyst.extract.text import document_text, visible_text
from catalyst.llm.client import InputTooLargeError, ModelClient, ModelError, SpendCapReachedError
from catalyst.logs import log_alert, log_event
from catalyst.schemas import Claim, Company, CompanyDocument, DocumentReading, ReadingSkip, SourceDocument
from catalyst.snapshots import SnapshotStore, SnapshotStoreError

NEW_YORK = ZoneInfo("America/New_York")
PRESS_RELEASE_TYPES = ("8-K EX-99.1",)
# Only these copies go to the model; a PDF read as text is raw bytes (ADR 0038).
TEXT_TYPES = frozenset({"text/html", "text/plain"})
# Fewer readable words than any real release: the shortest that ever gave a catalyst had about 1,200.
MIN_WORDS = 50

_log = logging.getLogger(__name__)


@dataclass
class DocumentResult:
    """What the job did with one document."""

    source_key: str
    url: str
    company: str | None
    published: date
    catalysts: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    claims_stored: int = 0
    error: str | None = None
    skipped: ReadingSkip | None = None  # why no model read it, if none did


@dataclass
class ExtractionReport:
    """The job's results, document by document, and why it stopped early if it did."""

    documents: list[DocumentResult] = field(default_factory=list[DocumentResult])
    skipped: int = 0
    stopped: str | None = None

    def summary(self) -> dict[str, Any]:
        """Return the counts the owner reads in the job summary."""
        catalysts = [c for d in self.documents for c in d.catalysts]
        skips = [d.skipped for d in self.documents]
        return {
            "documents_read": sum(d.error is None and d.skipped is None for d in self.documents),
            "documents_failed": sum(d.error is not None for d in self.documents),
            "documents_skipped_already_done": self.skipped,
            "documents_skipped_not_text": skips.count(ReadingSkip.NOT_TEXT),
            "documents_skipped_no_readable_text": skips.count(ReadingSkip.NO_READABLE_TEXT),
            "documents_skipped_too_large": skips.count(ReadingSkip.TOO_LARGE),
            "catalysts_proposed": len(catalysts),
            "catalysts_passing_every_check": sum(not c["problems"] for c in catalysts),
            "claims_stored": sum(d.claims_stored for d in self.documents),
            "stopped_early": self.stopped,
        }


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _candidates(
    session: Session, document_types: Sequence[str], since: date | None, limit: int
) -> tuple[list[Any], int]:
    """Return the newest matching documents not yet extracted with this prompt, and how many were done."""
    documents, claims, readings = TABLES[SourceDocument], TABLES[Claim], TABLES[DocumentReading]
    links, companies = TABLES[CompanyDocument], TABLES[Company]
    claimed = exists().where(
        claims.c.source_document_id == documents.c.id,
        claims.c.agent == ROLE,
        claims.c.prompt_id == EXTRACTOR_PROMPT[0],
        claims.c.prompt_version == EXTRACTOR_PROMPT[1],
    )
    read = exists().where(
        readings.c.source_document_id == documents.c.id,
        readings.c.agent == ROLE,
        readings.c.prompt_id == EXTRACTOR_PROMPT[0],
        readings.c.prompt_version == EXTRACTOR_PROMPT[1],
    )
    done = or_(claimed, read)
    company = (
        select(companies.c.name)
        .join(links, links.c.company_id == companies.c.id)
        .where(links.c.source_document_id == documents.c.id)
        .order_by(companies.c.name)
        .limit(1)
        .scalar_subquery()
    )
    chosen: list[ColumnElement[bool]] = [documents.c.document_type.in_(document_types)]
    if since is not None:
        chosen.append(documents.c.published_at >= datetime.combine(since, datetime.min.time(), NEW_YORK))
    query = select(documents, company.label("company")).where(*chosen)
    finished = select(func.count()).select_from(documents).where(*chosen, done)
    skipped = session.execute(finished).scalar_one()
    rows = session.execute(query.where(~done).order_by(documents.c.published_at.desc()).limit(limit)).all()
    return list(rows), skipped


def _shown(catalyst: ExtractedCatalyst) -> dict[str, Any]:
    # The job's report spells problems out for people; stored claims keep the short codes.
    return proposal_value(catalyst) | {
        "quote": catalyst.quote.quote if catalyst.quote else None,
        "problems": [problem.value for problem in catalyst.problems],
    }


def _store(
    session_factory: sessionmaker[Session], claims: list[Claim], reading: DocumentReading | None = None
) -> int:
    """Store the claims, or the reading of a document that gave none, in one transaction."""
    table = TABLES[Claim]
    with session_factory.begin() as session:
        if reading is not None:
            row = insert(TABLES[DocumentReading]).values(**reading.model_dump())
            session.execute(row.on_conflict_do_nothing())
        if not claims:
            return 0
        result = session.execute(
            insert(table)
            .values([c.model_dump() for c in claims])
            .on_conflict_do_nothing()
            .returning(table.c.id)
        )
        return len(result.all())


def _reading(document_id: Any, model: str, at: datetime) -> DocumentReading:
    """Return the extractor's reading of a document that gave no catalysts."""
    return DocumentReading(
        source_document_id=UUID(str(document_id)),
        agent=ROLE,
        prompt_id=EXTRACTOR_PROMPT[0],
        prompt_version=EXTRACTOR_PROMPT[1],
        model=model,
        catalysts=0,
        read_at=at,
    )


def _is_text(content_type: str) -> bool:
    return content_type.split(";")[0].strip().lower() in TEXT_TYPES


@dataclass(frozen=True)
class _Skip:
    """Keep a document no model read as a reading with its reason, and say so in the report."""

    session_factory: sessionmaker[Session]
    result: DocumentResult
    reading: DocumentReading

    def __call__(self, reason: ReadingSkip) -> None:
        self.result.skipped = reason
        _store(self.session_factory, [], self.reading.model_copy(update={"skipped": reason}))
        log_event(_log, "document_skipped", source_key=self.result.source_key, reason=reason.value)


def extract_documents(
    session_factory: sessionmaker[Session],
    store: SnapshotStore,
    client: ModelClient,
    *,
    limit: int,
    since: date | None = None,
    document_types: Sequence[str] = PRESS_RELEASE_TYPES,
    now: Callable[[], datetime] = _utc_now,
) -> ExtractionReport:
    """Extract catalysts from up to `limit` stored documents, newest first, and store the claims."""
    with session_factory() as session:
        rows, skipped = _candidates(session, document_types, since, limit)
    report = ExtractionReport(skipped=skipped)
    for row in rows:
        published = row.published_at.astimezone(NEW_YORK).date()
        result = DocumentResult(row.source_key, row.url, row.company, published)
        report.documents.append(result)
        skip = _Skip(session_factory, result, _reading(row.id, client.model_for(ROLE), now()))
        if not _is_text(row.content_type):
            skip(ReadingSkip.NOT_TEXT)
            continue
        try:
            content = store.get(row.snapshot_key)
            text = document_text(content, row.content_type)
            visible = visible_text(content, row.content_type)
            if len(visible.split()) < MIN_WORDS:
                skip(ReadingSkip.NO_READABLE_TEXT)
                continue
            extraction = extract_catalysts(client, text, published, visible=visible)
        except InputTooLargeError as error:
            log_alert(
                _log,
                "document_too_large",
                source_key=row.source_key,
                estimated_tokens=error.estimated_tokens,
                limit=error.limit,
            )
            skip(ReadingSkip.TOO_LARGE)
            continue
        except SpendCapReachedError as error:
            report.documents.pop()
            report.stopped = str(error)
            break
        except (ModelError, SnapshotStoreError, httpx.HTTPError) as error:
            result.error = f"{type(error).__name__}: {error}"
            continue
        if extraction.instructions:
            # A source that tries to steer the model: every claim from it waits for a person.
            log_alert(
                _log, "source_instructs_models", source_key=row.source_key, found=extraction.instructions[0]
            )
        result.catalysts = [_shown(c) for c in extraction.catalysts]
        # A read that found nothing is kept, so the next run does not pay to read it again.
        found_none = None if extraction.catalysts else _reading(row.id, extraction.model, now())
        claims = catalyst_claims(extraction, UUID(str(row.id)), now())
        result.claims_stored = _store(session_factory, claims, found_none)
        log_event(_log, "document_extracted", source_key=row.source_key, catalysts=len(result.catalysts))
    return report
