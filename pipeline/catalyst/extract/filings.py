"""Reading stored 10-Q and 10-K filings and 8-K exhibits other than 99.1, part by part (ADR 0066).

The collectors store these, but until now nothing read them. Each is read by the same extractor,
with the same prompt, and its proposals become the same claims, with the same checks and the same
publish gate, as a press release's. Only the reading differs: a long filing is read in parts
(`catalyst.extract.sections`), each part a call of its own, under the extractor's input limit.

- Each part read is kept in `document_part_reading` with its claims, in one transaction, so a run
  that stops (time, cap, an error) goes on with the next part, and no part is paid for twice.
- Once every part is read, `source_document.fully_read_at` is set in the same transaction as the
  last part's claims. A fully read document is never read again, whatever the prompt version.
- A copy no model can read (a PDF) or with almost no words is kept as a reading with its reason,
  as for press releases (ADR 0038).
- Spending: within each role's usual daily cap (`daily_cap_usd`). Only on a raised-cap day with
  a `filings_share` in config/models.toml, and only while the promotional credit still pays for
  the day (`credit_holds`), the line is that share of the raised cap instead, counted on the role's
  whole spend that day, so the press-release backfill keeps the rest (owner, in writing,
  2026-11-25; ADR 0081). The client's own caps apply to every call as well.
- Order: started filings first, then the newest filing date, 10-Q before 10-K before exhibits on
  the same date (ADR 0081).

Each reading is the strict visible text (`strict_text.visible_text`): its result never depends on
a time limit, so the parts of a document come out the same on every run.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import and_, case, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from catalyst.db.tables import TABLES
from catalyst.extract.agent import EXTRACTOR_PROMPT, ROLE, catalyst_claims, extract_catalysts
from catalyst.extract.run import (
    MIN_WORDS,
    NEW_YORK,
    READING_KEY,
    RECHECKED,
    TIME_LIMIT_STOP,
    DocumentResult,
    SkipRecorder,
    extractor_reading,
    is_text,
)
from catalyst.extract.sections import PART_CHARS, Part, document_parts
from catalyst.extract.strict_text import visible_text as strict_visible_text
from catalyst.extract.text import document_text, find_quote, visible_text
from catalyst.extract.xbrl_header import without_header
from catalyst.llm.client import (
    CHARS_PER_TOKEN_ESTIMATE,
    InputTooLargeError,
    ModelClient,
    ModelError,
    SpendCapReachedError,
)
from catalyst.llm.config import ModelConfig
from catalyst.llm.ledger import SpendLedger
from catalyst.llm.pricing import TokenUsage, cost_usd
from catalyst.logs import log_alert, log_event
from catalyst.schemas import Claim, DocumentPartReading, DocumentReading, ReadingSkip, SourceDocument
from catalyst.snapshots import SnapshotStore, SnapshotStoreError

FILING_FORMS = ("10-Q", "10-K")
PRESS_RELEASE = "8-K EX-99.1"  # read whole by `extract_documents`, never here
EXHIBITS = "8-K EX-99%"  # every other Exhibit 99 of an 8-K the collector keeps
# Room for the prompt and the publication date ahead of a part, in characters (the extractor's
# prompt is about 7,000).
PROMPT_CHARS = 8_000
USUAL_CAP_STOP = "the usual daily cap is reached; filings wait for the next run (ADR 0066)"
SHARE_STOP = "the filings' share of today's raised cap is reached; the rest is the backfill's (ADR 0081)"
# On a raised-cap day, which kind of filing goes first among those filed on the same date.
TYPE_ORDER = {"10-Q": 0, "10-K": 1}

_log = logging.getLogger(__name__)


def is_filing(document_type: str) -> bool:
    """Say whether this reader reads the document type: a 10-Q, a 10-K, or an 8-K Exhibit 99 but 99.1."""
    exhibit = document_type.startswith("8-K EX-99") and document_type != PRESS_RELEASE
    return document_type in FILING_FORMS or exhibit


def filing_documents() -> ColumnElement[bool]:
    """Select the documents this reader reads, as `is_filing` says in Python."""
    kind = TABLES[SourceDocument].c.document_type
    return or_(kind.in_(FILING_FORMS), and_(kind.like(EXHIBITS), kind != PRESS_RELEASE))


@dataclass(frozen=True)
class FilingView:
    """One reading of a stored filing: its text, what a reader sees, and the parts to read."""

    text: str  # `document_text`: where quotes are placed
    visible: str  # what a reader sees; every quote must be in it
    hide_text: str  # `text` without the inline XBRL header, for the hiding check
    parts: tuple[Part, ...]

    def part_holding(self, quote: str) -> Part | None:
        """Return the first part that holds `quote`, spacing aside, or None."""
        return next((part for part in self.parts if find_quote(part.text, quote) is not None), None)


def filing_view(content: bytes, content_type: str, document_type: str, limit: int = PART_CHARS) -> FilingView:
    """Read a stored filing once: its text, its visible text, and its parts."""
    text = document_text(content, content_type)
    visible = strict_visible_text(content, content_type)
    hide_text = document_text(without_header(content), content_type) if "html" in content_type else text
    return FilingView(text, visible, hide_text, tuple(document_parts(visible, document_type, limit)))


def reader_text(content: bytes, content_type: str, document_type: str, quote: str) -> str:
    """Return the text a reader of this document sees around `quote`: the part holding it, for a filing.

    A press release is read whole, as always. A filing is read in parts (ADR 0066), so a checker
    that reads it again (the weekly sample, ADR 0041) is shown the part that holds the claim's
    quote, under its input limit, instead of the whole filing it could never send.
    """
    if not is_filing(document_type):
        return visible_text(content, content_type)
    view = filing_view(content, content_type, document_type)
    part = view.part_holding(quote)
    return part.text if part is not None else view.visible


def part_call_most(config: ModelConfig, role: str) -> Decimal:
    """Return the most one part's call can cost `role`, by the client's own cautious estimate.

    A full part and the prompt at characters / 3 tokens of input, and a full answer of the role's
    `max_tokens`, at the role's model prices in config/models.toml.
    """
    settings = config.roles[role]
    tokens = math.ceil((PART_CHARS + PROMPT_CHARS) / CHARS_PER_TOKEN_ESTIMATE)
    return cost_usd(config.pricing[settings.model], TokenUsage(input=tokens, output=settings.max_tokens))


@dataclass(frozen=True)
class SpendGuard:
    """How far filing reads may spend today (ADR 0066, ADR 0081).

    The line is the role's usual daily cap, except on a day whose raised cap gives filings a share
    and while `credit_holds` (the promotional credit still pays for the day): then it is that share
    of the raised cap. Either way it is compared with the role's whole spend today, releases
    included, so filings never take more than their share of the day.
    """

    config: ModelConfig
    ledger: SpendLedger
    today: date
    credit_holds: bool = False  # the backfill's credit check (`catalyst.extract.backfill.credit_holds`)

    def line(self, role: str) -> Decimal:
        """Return the most `role`'s spend today may reach with a filing part's call."""
        settings = self.config.roles[role]
        share = settings.filings_line(self.today) if self.credit_holds else None
        return share if share is not None else settings.daily_cap_usd

    def on_credit(self) -> bool:
        """Say whether the extractor reads filings on its share of a raised cap today."""
        return self.credit_holds and self.config.roles[ROLE].filings_line(self.today) is not None

    def allows(self, role: str) -> bool:
        """Say whether `role` may start one more part's call today within its line."""
        return self.ledger.spent(role, self.today) + part_call_most(self.config, role) <= self.line(role)

    def stop_reason(self) -> str:
        """Say why filings stopped when the guard refused a part."""
        return SHARE_STOP if self.on_credit() else USUAL_CAP_STOP


@dataclass
class FilingResult(DocumentResult):
    """What the reader did with one filing."""

    document_type: str = ""
    parts: int = 0
    parts_read: int = 0  # read in this run
    fully_read: bool = False  # its last part was read in this run


@dataclass
class FilingReport:
    """The filing reader's results, and why it stopped early if it did."""

    documents: list[FilingResult] = field(default_factory=list[FilingResult])
    parts_already_read: int = 0
    waiting: int | None = None
    stopped: str | None = None
    stopped_for_time: bool = False

    def summary(self) -> dict[str, Any]:
        """Return the counts the owner reads; never page text."""
        skips = [d.skipped for d in self.documents if not d.skipped_before]
        return {
            "filings_started": sum(d.parts_read > 0 for d in self.documents),
            "filings_fully_read": sum(d.fully_read for d in self.documents),
            "filing_parts_read": sum(d.parts_read for d in self.documents),
            "filing_parts_already_read": self.parts_already_read,
            "filings_failed": sum(d.error is not None for d in self.documents),
            "filings_skipped_not_text": skips.count(ReadingSkip.NOT_TEXT),
            "filings_skipped_no_readable_text": skips.count(ReadingSkip.NO_READABLE_TEXT),
            "filings_rechecked_still_skipped": sum(d.skipped_before for d in self.documents),
            "filing_claims_stored": sum(d.claims_stored for d in self.documents),
            "filings_waiting": self.waiting,
            "stopped_early": self.stopped,
        } | ({"stopped_for_time": True} if self.stopped_for_time else {})


def _extractor_rows(table: Any, *where: ColumnElement[bool]) -> ColumnElement[bool]:
    documents = TABLES[SourceDocument]
    return exists().where(
        table.c.source_document_id == documents.c.id,
        table.c.agent == ROLE,
        table.c.prompt_id == EXTRACTOR_PROMPT[0],
        table.c.prompt_version == EXTRACTOR_PROMPT[1],
        *where,
    )


def _unread(since: date | None) -> list[ColumnElement[bool]]:
    """Select the filings not fully read, whose copy is kept and is not one no model can read."""
    documents, readings = TABLES[SourceDocument], TABLES[DocumentReading]
    chosen = [
        filing_documents(),
        documents.c.fully_read_at.is_(None),
        documents.c.copy_deleted_at.is_(None),
        ~_extractor_rows(readings, readings.c.skipped == ReadingSkip.NOT_TEXT.value),
    ]
    if since is not None:
        chosen.append(documents.c.published_at >= datetime.combine(since, datetime.min.time(), NEW_YORK))
    return chosen


def _recheck() -> ColumnElement[bool]:
    readings = TABLES[DocumentReading]
    return _extractor_rows(readings, readings.c.skipped.in_([skip.value for skip in RECHECKED]))


def filings_waiting(session: Session, since: date | None = None) -> int:
    """Count the filings not fully read, leaving out those kept only for a free re-check."""
    query = select(func.count()).select_from(TABLES[SourceDocument]).where(*_unread(since), ~_recheck())
    return session.execute(query).scalar_one()


def _candidates(session: Session, since: date | None, limit: int) -> list[Any]:
    """Return the filings to work on: started ones first, then the newest; then the re-checks.

    Newest is by New York filing date, and on one date a 10-Q goes before a 10-K before an exhibit
    (ADR 0081): a quarterly report says most about the next months for the least money.
    """
    documents, parts = TABLES[SourceDocument], TABLES[DocumentPartReading]
    started = _extractor_rows(parts)
    query = select(documents).where(*_unread(since))
    filed = func.date(func.timezone(NEW_YORK.key, documents.c.published_at))
    kind = case(TYPE_ORDER, value=documents.c.document_type, else_=len(TYPE_ORDER))
    order = (started.desc(), filed.desc(), kind, documents.c.published_at.desc(), documents.c.id)
    fresh = session.execute(query.where(~_recheck()).order_by(*order).limit(limit)).all()
    again = session.execute(query.where(_recheck()).order_by(*order).limit(limit)).all()
    return [*fresh, *again]


def parts_read(session: Session, document_id: UUID, agent: str, prompt: tuple[str, str]) -> set[str]:
    """Return the hashes of the parts of a document that `agent` has read with `prompt`."""
    table = TABLES[DocumentPartReading]
    query = select(table.c.part_sha256).where(
        table.c.source_document_id == document_id,
        table.c.agent == agent,
        table.c.prompt_id == prompt[0],
        table.c.prompt_version == prompt[1],
    )
    return set(session.execute(query).scalars())


def store_part(session: Session, claims: list[Claim], part: DocumentPartReading) -> int:
    """Store one part's claims and its reading together; return how many claims are new."""
    session.execute(insert(TABLES[DocumentPartReading]).values(**part.model_dump()).on_conflict_do_nothing())
    if not claims:
        return 0
    table = TABLES[Claim]
    stored = session.execute(
        insert(table).values([c.model_dump() for c in claims]).on_conflict_do_nothing().returning(table.c.id)
    )
    return len(stored.all())


def _finish(session: Session, document_id: UUID, model: str, at: datetime) -> bool:
    """Mark a document fully read, with the extractor's reading of it; return whether this did it."""
    parts = TABLES[DocumentPartReading]
    proposed = session.execute(
        select(func.coalesce(func.sum(parts.c.catalysts), 0)).where(
            parts.c.source_document_id == document_id,
            parts.c.agent == ROLE,
            parts.c.prompt_id == EXTRACTOR_PROMPT[0],
            parts.c.prompt_version == EXTRACTOR_PROMPT[1],
        )
    ).scalar_one()
    reading = extractor_reading(document_id, model, at, catalysts=int(proposed))
    row = insert(TABLES[DocumentReading]).values(**reading.model_dump())
    replaced = {"skipped": None, "catalysts": row.excluded.catalysts, "read_at": row.excluded.read_at}
    session.execute(row.on_conflict_do_update(index_elements=READING_KEY, set_=replaced))
    documents = TABLES[SourceDocument]
    marked = session.execute(
        update(documents)
        .where(documents.c.id == document_id, documents.c.fully_read_at.is_(None))
        .values(fully_read_at=at)
        .returning(documents.c.id)
    )
    return marked.first() is not None


def part_reading(
    document_id: UUID,
    part: Part,
    *,
    agent: str,
    prompt: tuple[str, str],
    model: str,
    catalysts: int,
    at: datetime,
) -> DocumentPartReading:
    """Return the record of one part read by one reader."""
    return DocumentPartReading(
        source_document_id=document_id,
        agent=agent,
        prompt_id=prompt[0],
        prompt_version=prompt[1],
        model=model,
        section=part.section,
        part=part.index,
        parts=part.count,
        part_start=part.start,
        part_end=part.end,
        part_sha256=part.sha256,
        catalysts=catalysts,
        read_at=at,
    )


class _Stop(Exception):
    """The run stops starting parts: a cap, the spending guard, or the time limit."""


@dataclass
class _Run:
    session_factory: sessionmaker[Session]
    store: SnapshotStore
    client: ModelClient
    now: Callable[[], datetime]
    deadline: datetime | None
    guard: SpendGuard | None
    report: FilingReport
    limit: int = PART_CHARS

    def check_time_and_money(self) -> None:
        if self.deadline is not None and self.now() >= self.deadline:
            self.report.stopped = f"{TIME_LIMIT_STOP}; the rest wait for the next run"
            self.report.stopped_for_time = True
            raise _Stop
        if self.guard is not None and not self.guard.allows(ROLE):
            self.report.stopped = self.guard.stop_reason()
            raise _Stop

    def read(self, row: Any) -> None:
        """Read the parts of one filing not yet read; mark it fully read after its last part."""
        document_id = UUID(str(row.id))
        published = row.published_at.astimezone(NEW_YORK).date()
        result = FilingResult(row.source_key, row.url, None, published, document_type=row.document_type)
        self.report.documents.append(result)
        skip = SkipRecorder(
            self.session_factory, result, extractor_reading(row.id, self.client.model_for(ROLE), self.now())
        )
        if not is_text(row.content_type):
            skip(ReadingSkip.NOT_TEXT)
            return
        try:
            view = filing_view(
                self.store.get(row.snapshot_key), row.content_type, row.document_type, self.limit
            )
        except (SnapshotStoreError, httpx.HTTPError) as error:
            result.error = f"{type(error).__name__}: {error}"
            return
        if len(view.visible.split()) < MIN_WORDS:
            skip(ReadingSkip.NO_READABLE_TEXT)
            return
        with self.session_factory() as session:
            done = parts_read(session, document_id, ROLE, EXTRACTOR_PROMPT)
        todo = [part for part in view.parts if part.sha256 not in done]
        result.parts = len(view.parts)
        self.report.parts_already_read += len(view.parts) - len(todo)
        if not todo:  # every part was read, but the run stopped before marking it
            with self.session_factory.begin() as session:
                result.fully_read = _finish(session, document_id, self.client.model_for(ROLE), self.now())
            return
        for part in todo:
            self.check_time_and_money()
            if not self._read_part(row, document_id, published, view, part, result, last=part is todo[-1]):
                return

    def _read_part(
        self,
        row: Any,
        document_id: UUID,
        published: date,
        view: FilingView,
        part: Part,
        result: FilingResult,
        *,
        last: bool,
    ) -> bool:
        try:
            extraction = extract_catalysts(
                self.client,
                view.text,
                published,
                visible=view.visible,
                shown=part.text,
                hide_text=view.hide_text,
            )
        except SpendCapReachedError as error:
            self.report.stopped = str(error)
            raise _Stop from error
        except (InputTooLargeError, ModelError) as error:
            result.error = f"{type(error).__name__}: {error}"
            return False
        if extraction.instructions:
            log_alert(
                _log, "source_instructs_models", source_key=row.source_key, found=extraction.instructions[0]
            )
        at = self.now()
        claims = catalyst_claims(extraction, document_id, at)
        reading = part_reading(
            document_id,
            part,
            agent=ROLE,
            prompt=EXTRACTOR_PROMPT,
            model=extraction.model,
            catalysts=len(extraction.catalysts),
            at=at,
        )
        with self.session_factory.begin() as session:
            result.claims_stored += store_part(session, claims, reading)
            if last:
                result.fully_read = _finish(session, document_id, extraction.model, at)
        result.parts_read += 1
        log_event(_log, "filing_part_read", source_key=row.source_key, part=part.index, parts=part.count)
        return True


def read_filings(
    session_factory: sessionmaker[Session],
    store: SnapshotStore,
    client: ModelClient,
    *,
    limit: int,
    now: Callable[[], datetime],
    since: date | None = None,
    deadline: datetime | None = None,
    guard: SpendGuard | None = None,
    part_chars: int = PART_CHARS,
) -> FilingReport:
    """Read up to `limit` filings not yet fully read, part by part, and store their claims.

    No part is started after `deadline`, or once `guard` says the day's allowance is spent; the
    next run goes on with the next part.
    """
    with session_factory() as session:
        rows = _candidates(session, since, limit)
    report = FilingReport()
    run = _Run(session_factory, store, client, now, deadline, guard, report, part_chars)
    try:
        for row in rows:
            run.check_time_and_money()  # before fetching the next copy, too
            run.read(row)
    except _Stop:
        pass
    with session_factory() as session:
        report.waiting = filings_waiting(session, since)
    log_event(_log, "filings_read", **{k: v for k, v in report.summary().items() if k != "stopped_early"})
    return report


@dataclass
class SecondFilingReport:
    """What the verifier's second reading of filings did: counts only."""

    documents_read: int = 0
    documents_failed: int = 0
    parts_read: int = 0
    stopped: str | None = None  # a role at its cap: as for press releases
    paused: str | None = None  # the spending guard: a normal end, the next run goes on
    stopped_for_time: bool = False


def _second_candidates(
    session: Session, model: str, agent: str, prompt: tuple[str, str], limit: int
) -> list[Any]:
    """Return fully read filings with extractor claims that the verifier has not read whole yet."""
    documents, claims, readings = TABLES[SourceDocument], TABLES[Claim], TABLES[DocumentReading]
    has_claims = exists().where(
        claims.c.source_document_id == documents.c.id,
        claims.c.agent == ROLE,
        claims.c.field.startswith("catalyst:"),
    )
    done = exists().where(
        readings.c.source_document_id == documents.c.id,
        readings.c.agent == agent,
        readings.c.prompt_id == prompt[0],
        readings.c.prompt_version == prompt[1],
        readings.c.model == model,
    )
    query = (
        select(documents)
        .where(
            filing_documents(),
            documents.c.fully_read_at.is_not(None),
            documents.c.copy_deleted_at.is_(None),
            has_claims,
            ~done,
        )
        .order_by(documents.c.published_at.desc(), documents.c.id)
        .limit(limit)
    )
    return list(session.execute(query).all())


def _claim_quotes(session: Session, document_id: UUID) -> list[str]:
    claims = TABLES[Claim]
    query = select(claims.c.quote).where(
        claims.c.source_document_id == document_id,
        claims.c.agent == ROLE,
        claims.c.field.startswith("catalyst:"),
    )
    return list(session.execute(query).scalars())


@dataclass
class SecondReader:
    """The verifier's second reading of fully read filings: only the parts that hold an extractor claim.

    A part with no claim has nothing to compare, so it is not paid for. The verifier's reading of
    the document is stored once every such part is read, and the usual comparison then runs on
    the document's claims (ADR 0018, part 2; ADR 0066).
    """

    session_factory: sessionmaker[Session]
    store: SnapshotStore
    client: ModelClient
    model: str
    agent: str
    prompt: tuple[str, str]
    now: Callable[[], datetime]
    out_of_time: Callable[[], bool]
    guard: SpendGuard | None = None
    part_chars: int = PART_CHARS
    report: SecondFilingReport = field(default_factory=SecondFilingReport)

    def run(self, limit: int) -> SecondFilingReport:
        """Read up to `limit` filings a second time, part by part; stop at a cap or the time limit."""
        with self.session_factory() as session:
            rows = _second_candidates(session, self.model, self.agent, self.prompt, limit)
        try:
            for row in rows:
                self._document(row)
        except _Stop:
            pass
        return self.report

    def _check(self) -> None:
        if self.out_of_time():
            self.report.stopped_for_time = True
            raise _Stop
        if self.guard is not None and not self.guard.allows(self.agent):
            self.report.paused = self.guard.stop_reason()
            raise _Stop

    def _document(self, row: Any) -> None:
        self._check()
        document_id = UUID(str(row.id))
        published = row.published_at.astimezone(NEW_YORK).date()
        try:
            content = self.store.get(row.snapshot_key)
        except (SnapshotStoreError, httpx.HTTPError) as error:
            self.report.documents_failed += 1
            log_event(_log, "second_reading_failed", source_key=row.source_key, error=type(error).__name__)
            return
        view = filing_view(content, row.content_type, row.document_type, self.part_chars)
        with self.session_factory() as session:
            quotes = _claim_quotes(session, document_id)
            done = parts_read(session, document_id, self.agent, self.prompt)
        needed = {part.index: part for quote in quotes if (part := view.part_holding(quote)) is not None}
        for index in sorted(needed):
            if needed[index].sha256 not in done and not self._part(
                row, document_id, published, view, needed[index]
            ):
                return
        with self.session_factory.begin() as session:
            self._record(session, document_id)
        self.report.documents_read += 1

    def _part(self, row: Any, document_id: UUID, published: date, view: FilingView, part: Part) -> bool:
        """Read one part a second time and store what the verifier found; False when the call failed."""
        self._check()
        try:
            extraction = extract_catalysts(
                self.client,
                view.text,
                published,
                visible=view.visible,
                shown=part.text,
                hide_text=view.hide_text,
                role=self.agent,
                prompt_ref=self.prompt,
            )
        except SpendCapReachedError as error:
            self.report.stopped = str(error)
            raise _Stop from error
        except ModelError as error:
            self.report.documents_failed += 1
            log_event(_log, "second_reading_failed", source_key=row.source_key, error=type(error).__name__)
            return False
        at = self.now()
        claims = catalyst_claims(extraction, document_id, at, agent=self.agent)
        reading = part_reading(
            document_id,
            part,
            agent=self.agent,
            prompt=self.prompt,
            model=extraction.model,
            catalysts=len(extraction.catalysts),
            at=at,
        )
        with self.session_factory.begin() as session:
            store_part(session, claims, reading)
        self.report.parts_read += 1
        return True

    def _record(self, session: Session, document_id: UUID) -> None:
        """Store the verifier's reading of the whole document, once its claim-holding parts are read."""
        parts = TABLES[DocumentPartReading]
        proposed = session.execute(
            select(func.coalesce(func.sum(parts.c.catalysts), 0)).where(
                parts.c.source_document_id == document_id,
                parts.c.agent == self.agent,
                parts.c.prompt_id == self.prompt[0],
                parts.c.prompt_version == self.prompt[1],
            )
        ).scalar_one()
        row = DocumentReading(
            source_document_id=document_id,
            agent=self.agent,
            prompt_id=self.prompt[0],
            prompt_version=self.prompt[1],
            model=self.model,
            catalysts=int(proposed),
            read_at=self.now(),
        )
        session.execute(insert(TABLES[DocumentReading]).values(**row.model_dump()).on_conflict_do_nothing())
