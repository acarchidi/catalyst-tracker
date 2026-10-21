"""The extraction job: run the extractor over stored press releases and keep the checked claims.

Plain code around the agent: it reads stored copies, calls the agent, and stores claims (never
catalysts; only the publish gate will write those). Safe to re-run: a document that already
has claims from this prompt version is skipped, and a claim is stored once (ADR 0013).

A read that makes no claim is kept as a reading, so a re-run skips it too until the prompt version
changes. Three kinds of copy never reach the model, each kept as a reading with its reason: one
that is not a web page or plain text (a PDF copy, done for good), and one with under `MIN_WORDS`
readable words or over the extractor's input limit. Those two are checked again each run, without
a model call and after the new documents, the longest-unchecked first, so a fixed text reader or a
higher limit reads them; each is counted only when first found with that reason (ADR 0038).
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
from sqlalchemy import and_, exists, func, or_, select, update
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
from catalyst.extract.text import document_text, read_page
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
# Skips that a later reader or limit can undo, so the document is checked again each run.
RECHECKED = (ReadingSkip.NO_READABLE_TEXT, ReadingSkip.TOO_LARGE)

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
    differs: bool = False  # its visible text is not the strict reader's: every claim waits for a person
    skipped_before: bool = False  # a re-check that found the same skip as an earlier run


@dataclass
class ExtractionReport:
    """The job's results, document by document, and why it stopped early if it did."""

    documents: list[DocumentResult] = field(default_factory=list[DocumentResult])
    skipped: int = 0
    stopped: str | None = None

    def summary(self) -> dict[str, Any]:
        """Return the counts the owner reads in the job summary."""
        catalysts = [c for d in self.documents for c in d.catalysts]
        skips = [d.skipped for d in self.documents if not d.skipped_before]
        return {
            "documents_read": sum(d.error is None and d.skipped is None for d in self.documents),
            "documents_failed": sum(d.error is not None for d in self.documents),
            "documents_skipped_already_done": self.skipped,
            "documents_skipped_not_text": skips.count(ReadingSkip.NOT_TEXT),
            "documents_skipped_no_readable_text": skips.count(ReadingSkip.NO_READABLE_TEXT),
            "documents_skipped_too_large": skips.count(ReadingSkip.TOO_LARGE),
            "documents_rechecked_still_skipped": sum(d.skipped_before for d in self.documents),
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
    """Return the documents to work on, and how many are done.

    First the newest documents not yet extracted with this prompt and never skipped, up to `limit`;
    then, up to `limit` more, those whose skip a fixed reader or a higher limit may undo, so those
    re-checks never take the place of a new document. Re-checks go longest-unchecked first: each one
    moves its skip's `read_at` to that run, so more than `limit` of them all come round in turn
    (review of PR #79).
    """
    documents, claims, readings = TABLES[SourceDocument], TABLES[Claim], TABLES[DocumentReading]
    links, companies = TABLES[CompanyDocument], TABLES[Company]
    claimed = exists().where(
        claims.c.source_document_id == documents.c.id,
        claims.c.agent == ROLE,
        claims.c.prompt_id == EXTRACTOR_PROMPT[0],
        claims.c.prompt_version == EXTRACTOR_PROMPT[1],
    )

    def reading(*where: ColumnElement[bool]) -> ColumnElement[bool]:
        return exists().where(
            readings.c.source_document_id == documents.c.id,
            readings.c.agent == ROLE,
            readings.c.prompt_id == EXTRACTOR_PROMPT[0],
            readings.c.prompt_version == EXTRACTOR_PROMPT[1],
            *where,
        )

    undoable = [skip.value for skip in RECHECKED]
    recheck = reading(readings.c.skipped.in_(undoable))
    last_checked = (
        select(func.max(readings.c.read_at))
        .where(
            readings.c.source_document_id == documents.c.id,
            readings.c.agent == ROLE,
            readings.c.prompt_id == EXTRACTOR_PROMPT[0],
            readings.c.prompt_version == EXTRACTOR_PROMPT[1],
            readings.c.skipped.in_(undoable),
        )
        .scalar_subquery()
    )
    done = or_(claimed, reading(readings.c.skipped.is_(None) | readings.c.skipped.not_in(undoable)))
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
    newest = documents.c.published_at.desc()
    fresh = session.execute(query.where(~done, ~recheck).order_by(newest).limit(limit)).all()
    longest_unchecked = (last_checked.asc(), newest, documents.c.id)
    again = session.execute(query.where(and_(~done, recheck)).order_by(*longest_unchecked).limit(limit)).all()
    return [*fresh, *again], skipped


def _shown(catalyst: ExtractedCatalyst) -> dict[str, Any]:
    # The job's report spells problems out for people; stored claims keep the short codes.
    return proposal_value(catalyst) | {
        "quote": catalyst.quote.quote if catalyst.quote else None,
        "problems": [problem.value for problem in catalyst.problems],
    }


def _store(
    session_factory: sessionmaker[Session], claims: list[Claim], reading: DocumentReading | None = None
) -> int:
    """Store the claims, or the model's reading of a document that gave none, in one transaction.

    The model's reading replaces an earlier skip of the same document, so it counts as done.
    """
    table = TABLES[Claim]
    with session_factory.begin() as session:
        if reading is not None:
            row = insert(TABLES[DocumentReading]).values(**reading.model_dump())
            replaced = {"skipped": None, "catalysts": row.excluded.catalysts, "read_at": row.excluded.read_at}
            session.execute(row.on_conflict_do_update(index_elements=_READING_KEY, set_=replaced))
        if not claims:
            return 0
        result = session.execute(
            insert(table)
            .values([c.model_dump() for c in claims])
            .on_conflict_do_nothing()
            .returning(table.c.id)
        )
        return len(result.all())


_READING_KEY = list(DocumentReading.__unique__[0])


def _keep_skip(session_factory: sessionmaker[Session], reading: DocumentReading) -> bool:
    """Store a skip; return whether it is new, so a re-check is counted and alerted only once.

    New means new for this document and reason, whatever the model: the row's key includes the
    model, so a model change would otherwise count and alert the same copy again (review of PR
    #79). A skip already on file for the same reason only moves its `read_at` to this run, which
    puts it last in the next re-check batch. A different reason replaces this model's skip and
    counts as new. A model's reading is never replaced by a skip.
    """
    table = TABLES[DocumentReading]
    same_document = (
        table.c.source_document_id == reading.source_document_id,
        table.c.agent == reading.agent,
        table.c.prompt_id == reading.prompt_id,
        table.c.prompt_version == reading.prompt_version,
    )
    with session_factory.begin() as session:
        seen = update(table).where(*same_document, table.c.skipped == reading.skipped)
        if session.execute(seen.values(read_at=reading.read_at).returning(table.c.id)).first() is not None:
            return False
        row = insert(table).values(**reading.model_dump())
        stored = row.on_conflict_do_update(
            index_elements=_READING_KEY,
            set_={"skipped": row.excluded.skipped, "read_at": row.excluded.read_at},
            where=table.c.skipped.is_not(None),
        )
        return session.execute(stored.returning(table.c.id)).first() is not None


def _reading(document_id: Any, model: str, at: datetime, catalysts: int = 0) -> DocumentReading:
    """Return the extractor's reading of a document that made no claim."""
    return DocumentReading(
        source_document_id=UUID(str(document_id)),
        agent=ROLE,
        prompt_id=EXTRACTOR_PROMPT[0],
        prompt_version=EXTRACTOR_PROMPT[1],
        model=model,
        catalysts=catalysts,
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

    def __call__(self, reason: ReadingSkip) -> bool:
        """Record the skip; return whether this run found it first."""
        self.result.skipped = reason
        new = _keep_skip(self.session_factory, self.reading.model_copy(update={"skipped": reason}))
        self.result.skipped_before = not new
        log_event(_log, "document_skipped", source_key=self.result.source_key, reason=reason.value, new=new)
        return new


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
            visible, differs, hidden = read_page(content, row.content_type)  # one reading of the page
            result.differs = differs
            # Words are counted in the visible text, not `document_text`: the model is shown only
            # the visible text and every quote must be in it, so hidden words can never give a
            # claim. A copy that is short only because the reader drops too much (`read_page`, ADR
            # 0044) is checked again each run, so a fixed reader reaches it without a data edit
            # (ADR 0038). When that reading differs from main's strict one, every claim waits.
            if len(visible.split()) < MIN_WORDS:
                skip(ReadingSkip.NO_READABLE_TEXT)
                if len(text.split()) >= MIN_WORDS:  # the words are there, but the page hides them
                    log_event(
                        _log,
                        "document_hides_its_words",
                        source_key=row.source_key,
                        words=len(text.split()),
                        hidden_by=hidden,
                    )
                continue
            extraction = extract_catalysts(client, text, published, visible=visible, differs=differs)
        except InputTooLargeError as error:
            if skip(ReadingSkip.TOO_LARGE):  # an alert only the first time; re-checks just log the skip
                log_alert(
                    _log,
                    "document_too_large",
                    source_key=row.source_key,
                    estimated_tokens=error.estimated_tokens,
                    limit=error.limit,
                )
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
        claims = catalyst_claims(extraction, UUID(str(row.id)), now())
        # A read that makes no claim (nothing found, or every quote lost) is kept, so the next run
        # does not pay to read it again (Opus review of PR #79, O1).
        found = len(extraction.catalysts)
        no_claim = None if claims else _reading(row.id, extraction.model, now(), catalysts=found)
        result.claims_stored = _store(session_factory, claims, no_claim)
        log_event(_log, "document_extracted", source_key=row.source_key, catalysts=len(result.catalysts))
    # Counts only, never page text: copies whose claims all wait because the readings differ.
    copies = sum(document.differs for document in report.documents)
    log_event(_log, "copies_read_differently", copies=copies, documents=len(report.documents))
    return report
