"""Closed deals: a stored document saying a deal was completed closes its open entry (ADR 0080).

The extractor reads forward-looking statements only, so a "completed the acquisition" release
never makes a claim that updates a merger or acquisition entry; its "expected to close" window
stayed on the calendar after the deal had closed. This plain code, run by the writer, closes it.

For each open `merger_acquisition` catalyst it reads the company's own stored documents published
after the source of the catalyst's current window:
- an 8-K that reports Item 2.01 (Completion of Acquisition or Disposition of Assets) or Item 5.01
  (Changes in Control of Registrant), and
- the company's press releases filed as 8-K Exhibit 99,
and looks for one visible sentence that says the deal happened ("completed", "closed",
"consummated", "became effective"), not that it will, and that names the counterparty or asset the
catalyst's own claims name. The company's own names do not count, and a catalyst whose claims name
no counterparty is never closed: when unsure, nothing happens.

A match becomes a claim of its own (agent `deal_closer`, the sentence as its verbatim quote, placed
in the stored copy's text), and a `status` change from the open status to `resolved` citing it,
later than the catalyst's last change. It never creates or moves a catalyst and never publishes
anything new; a closed catalyst is open no more, so a re-run finds nothing to do.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalyst.db.tables import TABLES
from catalyst.extract.agent import ROLE
from catalyst.extract.injection import invisible_characters
from catalyst.extract.run import TEXT_TYPES
from catalyst.extract.strict_text import visible_text
from catalyst.extract.text import TEXT_VERSION, document_text, find_quote
from catalyst.logs import log_event
from catalyst.publish.live import DEAL_CLOSER, LiveCatalyst, claim_value, load_live
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CatalystField,
    CatalystStatus,
    CatalystType,
    Claim,
    ClaimSubject,
    Company,
    CompanyDocument,
    CompanyName,
    CompanyTicker,
    SourceDocument,
)
from catalyst.schemas.entities import MAX_QUOTE_CHARS
from catalyst.snapshots import SnapshotStore, SnapshotStoreError, snapshot_key

__all__ = [
    "CLOSED_EVENT",
    "DEAL_CLOSER",
    "close_deals",
    "completion_sentences",
    "counterparties",
    "names_in",
    "reports_completion_item",
]

RULE_VERSION = "1.0.0"
PROMPT_ID = "closed_deals"  # no prompt: plain code; the field names the rule set and its version
PROVIDER = MODEL = "plain code"
FIELD_PREFIX = "deal_closed:"
CLOSED_EVENT = "the deal already closed; an older release cannot reopen it"
MAIN_8K = "8-K"
RELEASES = "8-K EX-99%"  # every Exhibit 99 of an 8-K: the company's own releases filed with the SEC
OPEN = (CatalystStatus.UPCOMING.value, CatalystStatus.SLIPPED.value)

_log = logging.getLogger(__name__)

# --- The completion sentence ---

_DEAL = r"(?:acquisition|merger|transaction|business\s+combination|purchase|sale|divestiture|tender\s+offer)"
_ARTICLE = r"(?:(?:its|the|their|our|this)\s+)?"
_ANNOUNCED = r"(?:(?:previously[\s-]+(?:announced|disclosed|reported)|pending|proposed)\s+)?"
# Active: "Viatris completed its previously announced acquisition of Pacira", "Completes Acquisition".
_ACTIVE = re.compile(
    rf"\b(?:has\s+|have\s+|had\s+)?(?:successfully\s+)?(?:completed|completes|closed|closes|consummated|consummates|"
    rf"finalized|finalizes)\s+{_ARTICLE}{_ANNOUNCED}{_DEAL}\b",
    re.IGNORECASE,
)
# Passive: "The acquisition of Mentari was consummated", "the Merger became effective".
_PASSIVE = re.compile(
    rf"\b{_DEAL}\b[^.;:]{{0,120}}?\b(?:was|were|has\s+been|have\s+been|had\s+been|is\s+now|became)\s+"
    rf"(?:successfully\s+|officially\s+)?(?:completed|closed|consummated|effective)\b",
    re.IGNORECASE,
)
# Noun: "announced the completion of its acquisition of Mentari".
_NOUN = re.compile(
    rf"\b(?:announced|announces|announce|confirmed|confirms)\s+(?:that\s+)?(?:the\s+)?(?:successful\s+)?"
    rf"(?:completion|closing|consummation)\s+of\s+{_ARTICLE}{_ANNOUNCED}{_DEAL}\b",
    re.IGNORECASE,
)
# A word before the statement that makes it a plan, a condition or a denial, not a fact.
_NOT_YET = re.compile(
    r"\b(?:expect\w*|anticipat\w*|will|would|shall|(?-i:may)|might|could|should|intend\w*|plan\w*|to\s+be|"
    r"upon|once|if|until|unless|subject\s+to|prior\s+to|before|following|after|pending|not|no|"
    r"seek\w*|aim\w*|agree\w*|propos\w*|when|whether)\b",
    re.IGNORECASE,
)
_ABBREVIATION_WORDS = (
    "inc corp co ltd plc llc lp fla calif mass n.y u.s u.k jan feb mar apr jun jul aug sep sept oct nov dec "
    "no nos mr ms mrs dr st vs e.g i.e approx"
)
_ABBREVIATIONS = frozenset(_ABBREVIATION_WORDS.split())
_END = re.compile(r"[.!?](?=\s+[A-Z])")
_ITEM = re.compile(
    r"\bItem\s+(2\.01|5\.01)\b\.?\s+(?:Completion\s+of\s+Acquisition|Changes?\s+in\s+Control)", re.IGNORECASE
)


@dataclass(frozen=True)
class Completion:
    """A sentence that says a deal happened, with where it stands in the text it was found in."""

    start: int
    end: int
    sentence: str


def _sentences(text: str) -> Iterable[tuple[int, int]]:
    """Yield each sentence's span: lines first, then sentence ends that are not after an abbreviation."""
    for line in re.finditer(r"[^\n]+", text):
        start = line.start()
        for end in _END.finditer(line.group(0)):
            words = line.group(0)[: end.start()].split()
            last = words[-1].strip("(),\"'\u201c\u201d\u2018\u2019").casefold() if words else ""
            if last in _ABBREVIATIONS or (len(last) == 1 and last.isalpha()):
                continue
            yield start, line.start() + end.end()
            start = line.start() + end.end()
        yield start, line.end()


def _states_completion(sentence: str) -> bool:
    """Say whether the sentence states, as a fact, that a deal happened.

    Nothing before the statement, and for the passive nothing between the deal and its verb, may
    make it a plan, a condition or a denial ("expected to", "will", "if", "upon", "not").
    """
    for pattern in (_ACTIVE, _PASSIVE, _NOUN):
        for found in pattern.finditer(sentence):
            inner = (
                re.sub(rf"^{_DEAL}", "", found.group(0), flags=re.IGNORECASE) if pattern is _PASSIVE else ""
            )
            if not _NOT_YET.search(sentence[: found.start()]) and not _NOT_YET.search(inner):
                return True
    return False


def completion_sentences(text: str) -> list[Completion]:
    """Return every sentence of `text` that says a deal was completed, closed or consummated."""
    found: list[Completion] = []
    for start, end in _sentences(text):
        raw = text[start:end]
        sentence = raw.strip()
        if not sentence or len(sentence) > MAX_QUOTE_CHARS or not _states_completion(sentence):
            continue
        offset = start + raw.index(sentence)
        found.append(Completion(offset, offset + len(sentence), sentence))
    return found


def reports_completion_item(text: str) -> str | None:
    """Return "2.01" or "5.01" when an 8-K's text holds that item's heading, else None."""
    found = _ITEM.search(text)
    return found.group(1) if found else None


# --- Names ---

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9&]*(?:[-\u2013][A-Za-z0-9]+)*")
# Capitalised words that name no party: deal words, defined terms, dates, places of business, forms.
_COMMON_WORDS = """
    a an and the of for to in on at by with from as or its it our we us their this that these those
    company companies inc incorporated corp corporation co ltd limited plc llc lp ag sa nv bv gmbh holdings
    holding group therapeutics pharmaceuticals pharmaceutical pharma biosciences bioscience bio
    biotherapeutics biopharma biopharmaceuticals sciences science medical health healthcare labs
    laboratories technologies
    merger mergers acquisition acquisitions acquire acquired acquires transaction transactions agreement
    agreements plan closing close closed completion complete completed completes consummation consummated
    tender offer offers purchase purchaser parent buyer seller sub subsidiary effective time date deal sale
    asset assets rights right global commercial license licence program programs shareholders
    stockholders shareholder stockholder board directors director vote meeting special annual
    expected expects expect anticipated anticipates today announced announces announce previously pending
    proposed definitive item items form exhibit current report press release about forward looking
    statements securities exchange act commission nasdaq nyse global select market stock common shares share
    cash per million billion dollar dollars fda sec ema u s us usa first second third fourth quarter half
    year end mid early late january february march april may june july august september october november
    december jan feb mar apr jun jul aug sep sept oct nov dec monday tuesday wednesday thursday friday
    new york time eastern nasal spray injection tablets tablet oral solution capsules phase trial
"""
_COMMON = frozenset(_COMMON_WORDS.split())


def names_in(text: str) -> frozenset[str]:
    """Return the distinctive names in `text`, case-folded: capitalised words or codes like ST-920."""
    names: set[str] = set()
    for found in _NAME.finditer(text):
        word = found.group(0)
        coded = any(ch.isdigit() for ch in word) and any(ch.isalpha() for ch in word)
        if not (word[0].isupper() or coded) or (len(word) < 3 and not coded):
            continue
        folded = word.casefold()
        if folded not in _COMMON:
            names.add(folded)
    return frozenset(names)


def counterparties(texts: Iterable[str], own: Iterable[str]) -> frozenset[str]:
    """Return the names the claims give that are not the company's own: its counterparty or asset."""
    mine: set[str] = set()
    for name in own:
        mine |= names_in(name)
    found: set[str] = set()
    for text in texts:
        found |= names_in(text)
    return frozenset(found - mine)


# --- The job ---


@dataclass(frozen=True)
class _Deal:
    live: LiveCatalyst
    names: frozenset[str]


@dataclass(frozen=True)
class _Found:
    completion: Completion  # placed in the stored copy's text, as every claim's quote is
    item: str | None
    names: frozenset[str]


def _claim_texts(session: Session, ids: Sequence[UUID]) -> dict[UUID, list[str]]:
    """Every extractor claim's quote and proposed title behind each catalyst's history."""
    change, claim = TABLES[CatalystChange], TABLES[Claim]
    query = (
        select(change.c.catalyst_id, claim.c.quote, claim.c.value)
        .join(claim, claim.c.id == change.c.claim_id)
        .where(change.c.catalyst_id.in_(ids), claim.c.agent == ROLE)
        .distinct()
    )
    texts: defaultdict[UUID, list[str]] = defaultdict(list)
    for row in session.execute(query):
        title = claim_value(row.value).get("title")
        texts[row.catalyst_id] += [row.quote, *([title] if isinstance(title, str) else [])]
    return texts


def _own_names(session: Session, companies: Sequence[UUID]) -> dict[UUID, list[str]]:
    company, names, tickers = TABLES[Company], TABLES[CompanyName], TABLES[CompanyTicker]
    own: defaultdict[UUID, list[str]] = defaultdict(list)
    for row in session.execute(
        select(company.c.id, company.c.name, company.c.display_name).where(company.c.id.in_(companies))
    ):
        own[row.id] += [row.name, *([row.display_name] if row.display_name else [])]
    for row in session.execute(
        select(names.c.company_id, names.c.name).where(names.c.company_id.in_(companies))
    ):
        own[row.company_id].append(row.name)
    for row in session.execute(
        select(tickers.c.company_id, tickers.c.ticker).where(tickers.c.company_id.in_(companies))
    ):
        own[row.company_id].append(row.ticker)
    return own


def _documents(session: Session, earliest: dict[UUID, datetime]) -> list[Any]:
    """Return the companies' stored 8-Ks and 8-K releases published after their earliest deal's source."""
    link, document = TABLES[CompanyDocument], TABLES[SourceDocument]
    after = or_(*(and_(link.c.company_id == c, document.c.published_at > at) for c, at in earliest.items()))
    query = (
        select(
            link.c.company_id,
            document.c.id,
            document.c.document_type,
            document.c.published_at,
            document.c.snapshot_key,
            document.c.sha256,
            document.c.content_type,
        )
        .join(document, document.c.id == link.c.source_document_id)
        .where(
            after,
            or_(document.c.document_type == MAIN_8K, document.c.document_type.like(RELEASES)),
            document.c.copy_deleted_at.is_(None),
        )
        .order_by(document.c.published_at, document.c.id)
    )
    return list(session.execute(query))


def _read(store: SnapshotStore, row: Any) -> list[_Found]:
    """Return each completion sentence the document shows a reader, placed in its stored text."""
    if row.content_type.split(";")[0].strip().lower() not in TEXT_TYPES or row.snapshot_key != snapshot_key(
        row.sha256
    ):
        return []
    try:
        content = store.get(row.snapshot_key)
        visible, text = visible_text(content, row.content_type), document_text(content, row.content_type)
    except (SnapshotStoreError, ValueError):
        return []  # an unreadable copy closes nothing; the snapshot checks report it
    item = reports_completion_item(visible)
    if row.document_type == MAIN_8K and item is None:
        return []
    found: list[_Found] = []
    for completion in completion_sentences(visible):
        span = find_quote(text, completion.sentence)
        if span is None or invisible_characters(span.quote):
            continue
        placed = Completion(span.start, span.end, span.quote)
        found.append(_Found(placed, item if row.document_type == MAIN_8K else None, names_in(span.quote)))
    return found


def _close(session: Session, deal: _Deal, row: Any, found: _Found, at: datetime) -> None:
    live = deal.live.row
    matched = sorted(found.names & deal.names)
    value: dict[str, JsonValue] = {
        "status": CatalystStatus.RESOLVED.value,
        "event": "deal_closed",
        "names": list[JsonValue](matched),
        "item": found.item,
        "document_type": str(row.document_type),
        "text_version": TEXT_VERSION,
        "rule_version": RULE_VERSION,
    }
    claim = Claim(
        source_document_id=row.id,
        subject_type=ClaimSubject.CATALYST,
        subject_id=live.id,
        field=f"{FIELD_PREFIX}{live.id}",
        value=value,
        quote=found.completion.sentence,
        quote_start=found.completion.start,
        quote_end=found.completion.end,
        agent=DEAL_CLOSER,
        prompt_id=PROMPT_ID,
        prompt_version=RULE_VERSION,
        provider=PROVIDER,
        model=MODEL,
        extracted_at=at,
    )
    table = TABLES[Claim]
    session.execute(insert(table).values(**claim.model_dump()).on_conflict_do_nothing())
    claim_id = session.execute(
        select(table.c.id).where(
            table.c.source_document_id == claim.source_document_id,
            table.c.field == claim.field,
            table.c.quote_start == claim.quote_start,
            table.c.quote_end == claim.quote_end,
            table.c.agent == claim.agent,
            table.c.prompt_id == claim.prompt_id,
            table.c.prompt_version == claim.prompt_version,
            table.c.model == claim.model,
        )
    ).scalar_one()
    change = CatalystChange(
        catalyst_id=live.id,
        field=CatalystField.STATUS,
        old_value=live.status.value,
        new_value=CatalystStatus.RESOLVED.value,
        claim_id=claim_id,
        learned_at=max(at, deal.live.last_learned + timedelta(microseconds=1)),
    )
    session.execute(
        update(TABLES[Catalyst])
        .where(TABLES[Catalyst].c.id == live.id, TABLES[Catalyst].c.status.in_(OPEN))
        .values(status=CatalystStatus.RESOLVED.value)
    )
    session.execute(insert(TABLES[CatalystChange]).values(**change.model_dump()))
    log_event(
        _log, "deal_closed", catalyst_id=str(live.id), claim_id=str(claim_id), source_document_id=str(row.id)
    )


def close_deals(session: Session, store: SnapshotStore, at: datetime) -> int:
    """Close every open merger or acquisition catalyst a later stored document says was completed.

    Returns how many were closed now. Runs inside the writer's transaction and lock.
    """
    live = load_live(session)
    open_deals = [
        c
        for c in live.catalysts
        if c.key.catalyst_type == CatalystType.MERGER_ACQUISITION.value and c.row.status.value in OPEN
    ]
    if not open_deals:
        return 0
    ids = [c.row.id for c in open_deals]
    texts = _claim_texts(session, ids)
    own = _own_names(session, list({c.key.company_id for c in open_deals}))
    deals: defaultdict[UUID, list[_Deal]] = defaultdict(list)
    for c in open_deals:
        names = counterparties(texts[c.row.id], own[c.key.company_id])
        if names:  # a claim that names no counterparty or asset is never closed: unsure
            deals[c.key.company_id].append(_Deal(c, names))
    if not deals:
        return 0
    earliest = {company: min(d.live.source.published_at for d in group) for company, group in deals.items()}
    closed: set[UUID] = set()
    read: dict[UUID, list[_Found]] = {}
    for row in _documents(session, earliest):
        waiting = [
            d
            for d in deals[row.company_id]
            if d.live.row.id not in closed and row.published_at > d.live.source.published_at
        ]
        if not waiting:
            continue
        if row.id not in read:
            read[row.id] = _read(store, row)
        for deal in waiting:
            match = next((f for f in read[row.id] if f.names & deal.names), None)
            if match is not None:
                _close(session, deal, row, match, at)
                closed.add(deal.live.row.id)
    return len(closed)
