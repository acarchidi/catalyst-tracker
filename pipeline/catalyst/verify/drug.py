"""A claim's drug must be named in its quote or just before it (spec: Verification; ADR 0018, part 3e).

The planted-error test swapped quotes for a sentence about another drug's event with the same
date words, and every check passed them. The check `drug_named_near_quote` reads the stored copy
and passes when the claim's drug is named:
- in the quote itself; or
- in the `LOOK_BACK` characters before it, with no other drug of the document's company named
  between that mention and the end of the quote. Correct quotes often name the drug only in a
  headline, a lead-in, or the sentence before ("Topline data are expected in Q4 2026.").

A drug is named by the claim's own drug name or any name the reference tables give the drug it
was matched to. A name is found as the resolver compares drug names (case, marks, spaces, and
dashes do not count, so "SPY-003" and "spy 003" name SPY003) and as whole words ("SPY0031" does
not). Hidden characters (a soft hyphen, a zero-width space) are looked past. A combination
("SPY072 + IL-17A/F") is named when each of its drugs is, within `NEAR` characters of each other.
A drug's name inside another drug's longer name ("SPY072" in "SPY072 + IL-17A/F") does not name
it. A claim with no drug, such as a trial of several drugs, is not applicable.

The check cannot tell two events of the same drug apart: a quote from the drug's other sub-study,
with the same date words, passes.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any, cast
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.extract.agent import ROLE
from catalyst.extract.text import document_text
from catalyst.logs import log_event
from catalyst.references import drug_refs
from catalyst.resolve import DrugRef, name_key
from catalyst.schemas import Claim, CompanyDocument, EntityMatch, MatchedEntity, MatchStatus, SourceDocument
from catalyst.snapshots import SnapshotStore, SnapshotStoreError
from catalyst.verify.rules import CheckResult, Outcome
from catalyst.verify.run import store_check

CHECK = "drug_named_near_quote"
# How far before the quote the drug may be named. The gold set's longest gap is 655 characters
# (Acadia: the two quotations just before the quote name remlifanserin); see ADR 0018, part 3e.
LOOK_BACK = 1_000

_log = logging.getLogger(__name__)
_MARKS = re.compile(r"[®™©℠]")
# What may sit between the letters and digits of one name: "SPY 003", "SPY-003", "IL-17A/F".
_GAP = re.compile(r"[\s/+®™©℠\-\u2010-\u2015]")
_PLUS = re.compile(r"\s*\+\s*")
# Characters a reader does not see, which copies of releases sometimes carry inside or beside a
# name: soft hyphen, zero-width space and (non-)joiners, word joiner, byte-order mark.
_HIDDEN = frozenset("\u00ad\u200b\u200c\u200d\u2060\ufeff")
# A combination's drugs must be named within this many characters, about one sentence.
NEAR = 200

Span = tuple[int, int]


@dataclass(frozen=True)
class DrugNames:
    """One drug as this check looks for it: the name to show, and every name it goes by."""

    shown: str
    names: tuple[str, ...]


def _names(*names: str) -> tuple[str, ...]:
    """Keep the names in order, once per name as the resolver compares them."""
    kept: dict[str, str] = {}
    for name in names:
        if (key := name_key(name)) and key not in kept:
            kept[key] = name.strip()
    return tuple(kept.values())


def drug_names(drug: str | None, matched: Iterable[DrugRef]) -> DrugNames | None:
    """Return the claim's drug with every name the matched drug goes by; None when it names no drug."""
    if drug is None or not drug.strip():
        return None
    return DrugNames(drug.strip(), _names(drug, *(name for ref in matched for name, _ in ref.names)))


def other_drugs(
    drugs: Iterable[DrugRef],
    companies: Collection[UUID],
    claim: DrugNames | None,
    beside: Iterable[str | None] = (),
) -> tuple[DrugNames, ...]:
    """Return the other drugs a quote might be about instead of the claim's.

    They are every drug of the companies' that shares no name with the claim's, and every drug
    the release's other claims name (`beside`). Production stores a drug record only for a new
    code name, so brand and generic names such as LIVMARLI are known only from those claims
    (Opus review of PR #61).
    """
    mine = {name_key(name) for name in claim.names} if claim else set[str]()
    found: list[DrugNames] = []
    for ref in drugs:
        names = _names(*(name for name, _ in ref.names))
        if ref.company_id in companies and names and not mine & {name_key(n) for n in names}:
            found.append(DrugNames(names[0], names))
    known = mine | {name_key(name) for other in found for name in other.names}
    for name in beside:
        if name and (key := name_key(name)) and key not in known:
            known.add(key)
            found.append(DrugNames(name.strip(), (name.strip(),)))
    return tuple(found)


class _Text:
    """A stored copy's letters and digits as the resolver compares names, with where each one sits."""

    def __init__(self, text: str) -> None:
        keys: list[str] = []
        self.where: list[int] = []
        for at, char in enumerate(text):
            for key in _MARKS.sub("", unicodedata.normalize("NFKC", char)).casefold():
                if key.isalnum():
                    keys.append(key)
                    self.where.append(at)
        self.text, self.keys = text, "".join(keys)
        self._found: dict[str, list[Span]] = {}

    def spans(self, name: str) -> list[Span]:
        """Every place the text writes this name as whole words."""
        key = name_key(name)
        if key not in self._found:
            found: list[Span] = []
            at = self.keys.find(key) if key else -1
            while at != -1:
                span = (self.where[at], self.where[at + len(key) - 1] + 1)
                if self._whole(*span):
                    found.append(span)
                at = self.keys.find(key, at + 1)
            self._found[key] = found
        return self._found[key]

    def _whole(self, start: int, end: int) -> bool:
        """Say whether the span is whole words: hidden characters are looked past, as a reader would."""
        text, before, after = self.text, start - 1, end
        while before >= 0 and text[before] in _HIDDEN:
            before -= 1
        while after < len(text) and text[after] in _HIDDEN:
            after += 1
        if (before >= 0 and text[before].isalnum()) or (after < len(text) and text[after].isalnum()):
            return False
        return all(char.isalnum() or char in _HIDDEN or _GAP.fullmatch(char) for char in text[start:end])


@lru_cache(maxsize=16)
def _indexed(text: str) -> _Text:
    return _Text(text)


def _parts(name: str) -> list[str]:
    parts = [part for part in _PLUS.split(name) if part]
    return parts if len(parts) > 1 else []


def _within(inner: Span, outer: Span) -> bool:
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def _mentions(text: _Text, drug: DrugNames, end: int) -> list[Span]:
    """Every stretch ending by `end` that names the drug.

    That is one of its names, or, for a combination, each of its drugs within `NEAR` characters
    of the first.
    """
    found: list[Span] = []
    for name in drug.names:
        found += [span for span in text.spans(name) if span[1] <= end]
        groups = [[span for span in text.spans(part) if span[1] <= end] for part in _parts(name)]
        for first in (span for group in groups for span in group):
            fits = [[s for s in group if s[0] >= first[0] and s[1] <= first[0] + NEAR] for group in groups]
            if all(fits):
                found.append((first[0], max(min(stop for _, stop in fit) for fit in fits)))
    return found


def _first(
    theirs: Sequence[tuple[Span, str]], start: int, end: int, mine: Sequence[Span]
) -> tuple[int, str] | None:
    """Return the first other drug named in [start, end), apart from those inside the claim's own names."""
    found = [
        (span[0], shown)
        for span, shown in theirs
        if start <= span[0] and span[1] <= end and not any(_within(span, m) for m in mine)
    ]
    return min(found, default=None)


def drug_named(
    text: str, quote: str, start: int, end: int, drug: DrugNames | None, others: Sequence[DrugNames]
) -> CheckResult:
    """Pass when the drug is named in the quote, or just before it with no other drug named after it.

    A mention inside another drug's longer name ("SPY072" in "SPY072 + IL-17A/F") does not count:
    the quote may be about the combination, so the claim waits for review.
    """
    if drug is None:
        return CheckResult(CHECK, Outcome.NOT_APPLICABLE, "the claim names no drug")
    if text[start:end] != quote:
        return CheckResult(CHECK, Outcome.FAIL, "the quote is not at its stored place in the stored copy")
    index = _indexed(text)
    theirs = [(span, other.shown) for other in others for name in other.names for span in index.spans(name)]
    mine = [
        m
        for m in _mentions(index, drug, end)
        if not any(_within(m, span) and m != span for span, _ in theirs)
    ]
    latest = max((m[0] for m in mine), default=None)
    if latest is not None and latest >= start:
        return CheckResult(CHECK, Outcome.PASS)
    if latest is None or latest < start - LOOK_BACK:
        named = _first(theirs, start, end, ())
        if named:
            return CheckResult(CHECK, Outcome.FAIL, f"the quote names {named[1]}, not {drug.shown}")
        return CheckResult(
            CHECK, Outcome.FAIL, f"the quote and the text just before it do not name {drug.shown}"
        )
    named = _first(theirs, start, end, mine) or _first(theirs, latest, start, mine)
    if named is None:
        return CheckResult(CHECK, Outcome.PASS)
    if named[0] >= start:
        return CheckResult(CHECK, Outcome.FAIL, f"the quote names {named[1]}, not {drug.shown}")
    return CheckResult(
        CHECK, Outcome.FAIL, f"the text just before the quote names {named[1]} after {drug.shown}"
    )


# --- The job ----------------------------------------------------------------------------------


@dataclass
class DrugReport:
    """What one run found."""

    claims: int = 0
    documents_read: int = 0
    documents_unreadable: int = 0
    outcomes: dict[str, int] = field(default_factory=dict[str, int])

    def summary(self) -> dict[str, Any]:
        """Return the counts as plain JSON."""
        return {
            "claims": self.claims,
            "documents_read": self.documents_read,
            "documents_unreadable": self.documents_unreadable,
            "outcomes": self.outcomes,
        }


def _read(
    store: SnapshotStore, snapshot_key: str, content_type: str, report: DrugReport
) -> str | CheckResult:
    try:
        text = document_text(store.get(snapshot_key), content_type)
    except (SnapshotStoreError, httpx.HTTPError, OSError, ValueError) as error:
        report.documents_unreadable += 1
        log_event(_log, "drug_check_unreadable", snapshot_key=snapshot_key, error=type(error).__name__)
        reason = f"the stored copy of the source could not be read ({type(error).__name__})"
        return CheckResult(CHECK, Outcome.FAIL, reason)
    report.documents_read += 1
    return text


def _drug(value: object) -> str | None:
    drug = cast(dict[str, object], value).get("drug") if isinstance(value, dict) else None
    return drug if isinstance(drug, str) else None


def check_drug_named(
    session_factory: sessionmaker[Session], store: SnapshotStore, now: Callable[[], datetime]
) -> DrugReport:
    """Store `drug_named_near_quote` for every extractor claim; safe to re-run.

    Each document is read once per run, and only when one of its claims names a drug.
    """
    claim, document, links, match = (
        TABLES[Claim],
        TABLES[SourceDocument],
        TABLES[CompanyDocument],
        TABLES[EntityMatch],
    )
    report = DrugReport()
    outcomes: Counter[str] = Counter()
    at = now()
    with session_factory.begin() as session:
        owners: dict[UUID, set[UUID]] = {}
        for row in session.execute(select(links.c.source_document_id, links.c.company_id)):
            owners.setdefault(row.source_document_id, set()).add(row.company_id)
        drugs = drug_refs(session)
        by_id = {ref.id: ref for ref in drugs}
        matched: dict[UUID, list[DrugRef]] = {}
        resolved = select(match.c.claim_id, match.c.matched_id).where(
            match.c.entity_type == MatchedEntity.DRUG.value, match.c.status == MatchStatus.RESOLVED.value
        )
        for row in session.execute(resolved):
            if row.matched_id in by_id:
                matched.setdefault(row.claim_id, []).append(by_id[row.matched_id])
        query = (
            select(
                claim.c.id,
                claim.c.source_document_id,
                claim.c.value,
                claim.c.quote,
                claim.c.quote_start,
                claim.c.quote_end,
                document.c.snapshot_key,
                document.c.content_type,
            )
            .join(document, document.c.id == claim.c.source_document_id)
            .where(claim.c.agent == ROLE, claim.c.field.startswith("catalyst:"))
            .order_by(claim.c.extracted_at, claim.c.id)
        )
        rows = session.execute(query).all()
        beside: dict[UUID, list[tuple[UUID, str | None]]] = {}
        for row in rows:
            beside.setdefault(row.source_document_id, []).append((row.id, _drug(row.value)))
        texts: dict[UUID, str | CheckResult] = {}
        for row in rows:
            names = drug_names(_drug(row.value), matched.get(row.id, []))
            if names is None:
                result = drug_named("", row.quote, row.quote_start, row.quote_end, None, ())
            else:
                if row.source_document_id not in texts:
                    texts[row.source_document_id] = _read(store, row.snapshot_key, row.content_type, report)
                text = texts[row.source_document_id]
                named = [drug for claim_id, drug in beside[row.source_document_id] if claim_id != row.id]
                others = other_drugs(drugs, owners.get(row.source_document_id, set()), names, named)
                result = (
                    text
                    if isinstance(text, CheckResult)
                    else drug_named(text, row.quote, row.quote_start, row.quote_end, names, others)
                )
            store_check(session, row.id, result, at)
            report.claims += 1
            outcomes[f"{result.check}:{result.outcome.value}"] += 1
    report.outcomes = dict(outcomes)
    log_event(_log, "drug_named_checked", **report.summary())
    return report
