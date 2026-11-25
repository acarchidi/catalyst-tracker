"""Two open entries that are one event: keep one, mark the other merged (ADR 0082).

The writer places a claim on an open entry only when the company, type and matched drugs are the
same (`catalyst.publish.live.place`). One drug matched to two drug records ("pegozafermin" and
"pegozafermin (BIO89-100)"), or one conference talk from two releases, therefore made two entries
of one event, and the weekly issue listed both.

Plain code, no model calls, inside the writer's lock and transaction, after it has placed the
night's claims. Two open entries are one event only when every test holds:
- the same company and the same type;
- the same drug, as the second reading compares names (ADR 0062): "NAME (OTHER)" is one drug and
  "X with Y" is never X; both must name one, so two entries without a drug (most deals) are never
  merged; drug links on both sides must say the same (alone, or in combination);
- trials that are the same, or one side names none;
- indications that are the same, or one side names none, both as stated and as matched;
- windows that overlap;
- for a conference presentation, the same meeting name read from their quotes, or the very same
  dates when either names none; two different meetings are two events.

A group is merged only when every entry in it is the same event as every other, and one source
outranks the rest: a higher tier, then a later publication (spec: Which claim wins); that entry is
kept. When unsure, nothing happens. The kept entry's values never change, nothing new is published,
and no date moves. Each other entry gets status `merged`, with a status change in its history
citing a plain-code claim that quotes the kept entry's own source, and its claims are linked to the
kept entry as extra sources. Every row of the merged entry stays, so the merge can be undone with
one status change. Safe to re-run: a merged entry is open no more.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalyst.db.tables import TABLES
from catalyst.extract.agent import ROLE
from catalyst.logs import log_event
from catalyst.publish.live import DEDUPLICATOR, WINDOW_FIELDS, LiveCatalyst, Rank, claim_value, load_live
from catalyst.references import load_reference_data
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CatalystDrug,
    CatalystField,
    CatalystMergedSource,
    CatalystStatus,
    CatalystType,
    Claim,
    ClaimSubject,
    SourceTier,
)
from catalyst.verify.second import SameDrug, drug_names_agree
from catalyst.verify.second_run import same_drug_rule

__all__ = ["DEDUPLICATOR", "Entry", "meeting_names", "merge_duplicates", "merge_plan", "same_event"]

RULE_VERSION = "1.0.0"
PROMPT_ID = "merge_duplicates"  # no prompt: plain code; the field names the rule and its version
PROVIDER = MODEL = "plain code"
FIELD_PREFIX = "merged_into:"
OPEN = (CatalystStatus.UPCOMING, CatalystStatus.SLIPPED)
_PLAIN_CODE = (DEDUPLICATOR, "deal_closer")  # claims of these roles quote evidence, not the event

_log = logging.getLogger(__name__)

# --- Meeting names, read from a quote ("presented at the AASLD Liver Meeting") ---

_MEETING_WORDS = frozenset(
    {
        "meeting",
        "meetings",
        "congress",
        "conference",
        "symposium",
        "summit",
        "forum",
        "session",
        "sessions",
        "workshop",
        "week",
        "convention",
    }
)
_PLAIN_WORDS = frozenset({"the", "annual", "of", "for", "and", "on", "&"})
_WORD = r"(?:[A-Z][\w'&/-]*|\d{4}|\d+(?:st|nd|rd|th))"  # a capitalised word, a year or an ordinal
_LINK = r"(?:of|for|and|on|the|&)"
_NAME = re.compile(rf"\b(?:at|during)\s+(?:[Tt]he\s+)?({_WORD}(?:\s+(?:{_LINK}\s+)*{_WORD})*)")
_ASIDE = re.compile(r"\s*\([^()]*\)")  # "(ASCO)": a short form in brackets is not part of the name
_YEAR_OR_ORDINAL = re.compile(r"^(?:\d{4}|\d+(?:st|nd|rd|th))$")


def meeting_names(quote: str) -> frozenset[str]:
    """Return each meeting the quote says the data are presented at, as a comparable name.

    A name is a run of capitalised words after "at" or "during" that holds a meeting word
    ("Meeting", "Congress") and a word of its own; years, ordinals, "the" and "annual" are left out,
    and case does not count. A name not written this way is not read: the dates must then agree.
    """
    found: set[str] = set()
    for match in _NAME.finditer(_ASIDE.sub("", quote)):
        words = [w for w in match.group(1).split() if not _YEAR_OR_ORDINAL.match(w)]
        folded = [w.casefold() for w in words if w.casefold() not in {"the", "annual"}]
        if any(w in _MEETING_WORDS for w in folded) and any(
            w not in _MEETING_WORDS and w not in _PLAIN_WORDS for w in folded
        ):
            found.add(" ".join(folded))
    return frozenset(found)


# --- The rule ---


@dataclass(frozen=True)
class Entry:
    """An open entry as the rule compares it."""

    id: UUID
    company_id: UUID
    catalyst_type: str
    drug: str | None  # as the claim that created the entry names it
    roles: frozenset[str]  # its drug links: alone, in combination
    trials: frozenset[UUID]
    indication: str  # as stated, normalised; empty when none is stated
    indication_id: UUID | None  # as matched
    window: tuple[date, date]
    meetings: frozenset[str]  # the meeting names its quotes give
    rank: Rank  # the source behind its window

    def order(self) -> tuple[int, float]:
        """Best first: a higher tier, then a later publication."""
        return tuple(SourceTier).index(self.rank.tier), -self.rank.published_at.timestamp()


def _agree(a: object, b: object) -> bool:
    """Say whether two values are the same, or one side states none."""
    return not a or not b or a == b


def _meeting(entry: Entry) -> str | None:
    return next(iter(entry.meetings)) if len(entry.meetings) == 1 else None


def _names_only(_a: str, _b: str) -> bool:
    return False  # no reference tables: only the names themselves can agree


def same_event(a: Entry, b: Entry, same_drug: SameDrug = _names_only) -> bool:
    """Say whether two open entries are one event; every test must hold (see the module text)."""
    if a.id == b.id or (a.company_id, a.catalyst_type) != (b.company_id, b.catalyst_type):
        return False
    if not _agree(a.roles, b.roles) or not drug_names_agree(a.drug, b.drug, same_drug):
        return False
    if not (_agree(a.trials, b.trials) and _agree(a.indication, b.indication)):
        return False
    if not _agree(a.indication_id, b.indication_id):
        return False
    if a.window[0] > b.window[1] or b.window[0] > a.window[1]:
        return False
    if a.catalyst_type != CatalystType.CONFERENCE_PRESENTATION.value:
        return True
    mine, theirs = _meeting(a), _meeting(b)
    if (a.meetings and mine is None) or (b.meetings and theirs is None):
        return False  # an entry that names two meetings names neither for sure
    if mine is not None and theirs is not None:
        return mine == theirs  # two named meetings must be the same one, whatever the dates
    return a.window == b.window


def merge_plan(entries: Sequence[Entry], same_drug: SameDrug = _names_only) -> list[tuple[Entry, Entry]]:
    """Return (kept, merged) pairs: groups whose every entry is the same event as every other, with one best.

    A group where two entries are not the same event, or whose two best sources rank the same,
    merges nothing. The order of `entries` never changes the outcome.
    """
    plan: list[tuple[Entry, Entry]] = []
    alike: dict[UUID, set[UUID]] = {
        e.id: {o.id for o in entries if same_event(e, o, same_drug) and same_event(o, e, same_drug)}
        for e in entries
    }
    seen: set[UUID] = set()
    for entry in sorted(entries, key=lambda e: str(e.id)):
        if entry.id in seen or not alike[entry.id]:
            continue
        group, todo = {entry.id}, [entry.id]
        while todo:
            for other in alike[todo.pop()] - group:
                group.add(other)
                todo.append(other)
        seen |= group
        if any(alike[member] != group - {member} for member in group):
            continue  # not one event throughout: plain code cannot choose
        members = sorted((e for e in entries if e.id in group), key=Entry.order)
        if members[0].order() == members[1].order():
            continue  # two sources of the same rank: no rule says which to keep
        plan += [(members[0], other) for other in members[1:]]
    return sorted(plan, key=lambda pair: (str(pair[0].id), str(pair[1].id)))


# --- On the database ---


@dataclass
class _Facts:
    """What the rule reads of each entry beyond the live index."""

    drug: dict[UUID, str | None]
    quotes: defaultdict[UUID, list[str]]
    claims: defaultdict[UUID, list[UUID]]  # the claims its history cites, oldest first
    window_claim: dict[UUID, UUID]  # the claim behind its window
    roles: defaultdict[UUID, set[str]]


def _facts(session: Session, ids: Sequence[UUID]) -> _Facts:
    change, claim, link = TABLES[CatalystChange], TABLES[Claim], TABLES[CatalystDrug]
    facts = _Facts({}, defaultdict(list), defaultdict(list), {}, defaultdict(set))
    rows = session.execute(
        select(
            change.c.catalyst_id,
            change.c.field,
            change.c.claim_id,
            claim.c.agent,
            claim.c.value,
            claim.c.quote,
        )
        .join(claim, claim.c.id == change.c.claim_id)
        .where(change.c.catalyst_id.in_(ids))
        .order_by(change.c.learned_at, change.c.id)
    )
    for row in rows:
        if row.agent in _PLAIN_CODE:
            continue
        if row.claim_id not in facts.claims[row.catalyst_id]:
            facts.claims[row.catalyst_id].append(row.claim_id)
            if row.agent == ROLE:
                facts.quotes[row.catalyst_id].append(row.quote)
        if row.field == CatalystField.TITLE.value:  # set once, by the claim that created it
            drug = claim_value(row.value).get("drug")
            facts.drug[row.catalyst_id] = drug if isinstance(drug, str) else None
        elif row.field in WINDOW_FIELDS:
            facts.window_claim[row.catalyst_id] = row.claim_id
    for row in session.execute(select(link.c.catalyst_id, link.c.role).where(link.c.catalyst_id.in_(ids))):
        facts.roles[row.catalyst_id].add(str(row.role))
    return facts


def _entry(live: LiveCatalyst, facts: _Facts) -> Entry:
    catalyst_id = live.row.id
    meetings = frozenset[str]().union(*(meeting_names(q) for q in facts.quotes[catalyst_id]))
    return Entry(
        id=catalyst_id,
        company_id=live.key.company_id,
        catalyst_type=live.key.catalyst_type,
        drug=facts.drug.get(catalyst_id),
        roles=frozenset(facts.roles[catalyst_id]),
        trials=live.key.trials,
        indication=live.key.indication,
        indication_id=live.row.indication_id,
        window=live.window,
        meetings=meetings,
        rank=live.source,
    )


def _merge(session: Session, kept: LiveCatalyst, dropped: LiveCatalyst, facts: _Facts, at: datetime) -> None:
    """Mark `dropped` merged into `kept`, with its status change and its claims as `kept`'s extra sources."""
    table = TABLES[Claim]
    source: Any = session.execute(select(table).where(table.c.id == facts.window_claim[kept.row.id])).one()
    value: dict[str, JsonValue] = {
        "status": CatalystStatus.MERGED.value,
        "event": "duplicate_merged",
        "kept": str(kept.row.id),
        "merged": str(dropped.row.id),
        "text_version": claim_value(source.value).get("text_version"),
        "rule_version": RULE_VERSION,
    }
    claim = Claim(
        source_document_id=source.source_document_id,
        subject_type=ClaimSubject.CATALYST,
        subject_id=dropped.row.id,
        field=f"{FIELD_PREFIX}{dropped.row.id}",
        value=value,
        quote=source.quote,  # the kept entry's own source states the event, word for word
        quote_start=source.quote_start,
        quote_end=source.quote_end,
        agent=DEDUPLICATOR,
        prompt_id=PROMPT_ID,
        prompt_version=RULE_VERSION,
        provider=PROVIDER,
        model=MODEL,
        extracted_at=at,
    )
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
        catalyst_id=dropped.row.id,
        field=CatalystField.STATUS,
        old_value=dropped.row.status.value,
        new_value=CatalystStatus.MERGED.value,
        claim_id=claim_id,
        learned_at=max(at, dropped.last_learned + timedelta(microseconds=1)),
        is_correction=True,  # it fixes our own listing of one event twice
    )
    catalyst = TABLES[Catalyst]
    session.execute(
        update(catalyst).where(catalyst.c.id == dropped.row.id).values(status=CatalystStatus.MERGED.value)
    )
    session.execute(insert(TABLES[CatalystChange]).values(**change.model_dump()))
    links = [
        CatalystMergedSource(catalyst_id=kept.row.id, merged_id=dropped.row.id, claim_id=c, learned_at=at)
        for c in facts.claims[dropped.row.id]
    ]
    if links:
        rows = [link.model_dump() for link in links]
        session.execute(insert(TABLES[CatalystMergedSource]).values(rows).on_conflict_do_nothing())
    log_event(
        _log, "duplicate_merged", kept=str(kept.row.id), merged=str(dropped.row.id), claim_id=str(claim_id)
    )


def _groups(open_entries: Iterable[LiveCatalyst]) -> dict[UUID, list[LiveCatalyst]]:
    """Each company's open entries that share a type with another of its open entries."""
    by_kind: defaultdict[tuple[UUID, str], list[LiveCatalyst]] = defaultdict(list)
    for live in open_entries:
        by_kind[live.key.company_id, live.key.catalyst_type].append(live)
    groups: defaultdict[UUID, list[LiveCatalyst]] = defaultdict(list)
    for (company, _), entries in by_kind.items():
        if len(entries) > 1:
            groups[company] += entries
    return groups


def merge_duplicates(session: Session, at: datetime) -> int:
    """Merge every group of open entries that is surely one event; return how many entries were merged.

    Runs inside the writer's transaction and lock, after it has placed the night's claims.
    """
    live = load_live(session)
    groups = _groups(c for c in live.catalysts if c.row.status in OPEN)
    if not groups:
        return 0
    facts = _facts(session, [c.row.id for group in groups.values() for c in group])
    references = load_reference_data(session)
    merged = 0
    for company, group in groups.items():
        by_id = {c.row.id: c for c in group}
        entries = [_entry(c, facts) for c in group if c.row.id in facts.window_claim]
        for kept, dropped in merge_plan(entries, same_drug_rule(references, company)):
            _merge(session, by_id[kept.id], by_id[dropped.id], facts, at)
            merged += 1
    return merged
