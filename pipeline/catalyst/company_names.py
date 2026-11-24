r"""The company name readers see (ADR 0068).

SEC's conformed names are often in capitals ("MACROGENICS INC") or carry EDGAR's state suffix
("Viridian Therapeutics, Inc.\DE"). Title case is no fix: it gives "Macrogenics" and "Biomarin".
So the display name is the company's own spelling, as its own stored press releases write it:
- the releases are the 8-K exhibits 99 filed under the company's own SEC number, the newest
  `RELEASES_READ` of them;
- a spelling is a run of words that, compared after the resolver's normalization (case,
  punctuation, legal-form words), is the SEC name, with the legal form the text writes after it
  ("MacroGenics, Inc.", "Incyte Corporation"). It must start with a capital or a digit, and a
  spelling with no small letter is a headline, not a spelling;
- short legal words that are also words ("as", "Co-founder") count only in capitals or with
  their dots or slash; only spaces, commas, full stops, and hyphens join a name's words; control
  and invisible characters are removed first, and a spelling over `MAX_NAME_CHARS` is not used;
- a spelling with a legal form beats one without; then the most frequent; then the longer; then
  the first seen, newest release first;
- with no spelling, it is the SEC name without its state suffix, and nothing else changes.

The step stores the name, the release it came from, and a digest of its inputs (this code, the
SEC name, and the releases read). A run reads a company's releases again only when that digest
changes, so a re-run with nothing new reads and writes nothing. A copy that cannot be read keeps
no digest, so it is tried again next run.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.coverage import normalize_name, without_state_suffix
from catalyst.db.tables import TABLES
from catalyst.extract.text import TEXT_VERSION, document_text
from catalyst.logs import log_event
from catalyst.schemas import Company, CompanyDocument, DocumentMatch, SourceDocument
from catalyst.snapshots import SnapshotStore, SnapshotStoreError
from catalyst.verify.incremental import code_version, input_digest

__all__ = [
    "RELEASES_READ",
    "NamesReport",
    "display_name",
    "own_spelling",
    "recompute_display_names",
    "spelled_in",
    "without_state_suffix",
]

RELEASES_READ = 3  # the newest own press releases read per company
PRESS_RELEASE = "EX-99"  # the exhibit type of an 8-K press release, as `document_type` names it
_MAX_FORM_WORDS = 3  # "Company N.V.", "Co., Ltd."
_LEGAL = frozenset(
    [
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "co",
        "company",
        "ltd",
        "limited",
        "plc",
        "llc",
        "lp",
        "sa",
        "ag",
        "nv",
        "se",
        "ab",
        "as",
        "asa",
        "spa",
        "bv",
        "gmbh",
        "kk",
        "oyj",
    ]
)
_ABBREVIATED = frozenset({"inc", "corp", "co", "ltd"})  # their period is part of the name
# Legal words that are also English or name parts ("joined Incyte as", "Co-founder"): they count
# only in capitals ("AS", "SE") or written with their dots or slash ("Co.", "S.A.", "A/S").
_SHORT_LEGAL = frozenset({"co", "sa", "ag", "nv", "se", "ab", "as", "asa", "kk", "bv", "lp", "spa"})
# One legal word after a name: dotted letters ("N.V."), "A/S", or a word with an optional full
# stop, never one joined to the next word by a hyphen ("Co-founder").
_FORM_WORD = re.compile(r",? +((?:[^\W\d_]\.){2,}|[^\W\d_]/[^\W\d_]|[^\W\d_]+\.?)(?![\w\-\u2010-\u2015])")
# Between two words of a name only plain spaces, commas, full stops, and hyphens; "&" is a word.
_GAP = r"[ ,.\-]{1,3}"
_AROUND_AND = r"[ ,.\-]{0,3}"
MAX_NAME_CHARS = 120  # a longer spelling is a misreading, not a company name

_log = logging.getLogger(__name__)


def _name_pattern(target: str) -> re.Pattern[str]:
    """Return a pattern for the normalized name's words as a text may write them."""
    words = target.split()
    pattern = ""
    for index, word in enumerate(words):
        if index:
            pattern += _AROUND_AND if "and" in (word, words[index - 1]) else _GAP
        pattern += r"(?:and|&)" if word == "and" else re.escape(word)
    return re.compile(rf"(?<![^\W_]){pattern}(?![^\W_])", re.IGNORECASE)


def _with_form(text: str, end: int) -> str:
    """Return the legal form written right after a name ending at `end` (", Inc.", " N.V."), or ""."""
    form, at = "", end
    for _ in range(_MAX_FORM_WORDS):
        found = _FORM_WORD.match(text, at)
        if found is None:
            break
        word = found.group(1)
        bare = word.replace(".", "").replace("/", "").casefold()
        if bare not in _LEGAL:
            break
        if bare in _SHORT_LEGAL and not ("." in word or "/" in word or word.isupper()):
            break
        if word.endswith(".") and word.count(".") == 1 and bare not in _ABBREVIATED:
            word = word[:-1]  # "Corporation." ends a sentence
        form += found.group(0)[: found.start(1) - found.start(0)] + word
        at = found.end()
    return form


@dataclass(frozen=True)
class _Seen:
    spelling: str
    has_form: bool
    text_index: int
    order: int


def printable(text: str) -> str:
    """Return the text without control or invisible format characters; line breaks become spaces."""
    kept: list[str] = []
    for char in text:
        if unicodedata.category(char) in ("Cc", "Cf"):
            kept.append(" " if char in "\t\n\r\f\v" else "")
        else:
            kept.append(char)
    return "".join(kept)


def _spellings(sec_name: str, texts: Sequence[str]) -> list[_Seen]:
    target = normalize_name(sec_name)
    if not target:
        return []
    pattern = _name_pattern(target)
    seen: list[_Seen] = []
    for index, text in enumerate(map(printable, texts)):
        for found in pattern.finditer(text):
            name = found.group(0)
            form = _with_form(text, found.end())
            spelling = " ".join(f"{name}{form}".split())
            if len(spelling) > MAX_NAME_CHARS or not spelling.isprintable():
                continue
            if not (spelling[0].isupper() or spelling[0].isdigit()) or not any(c.islower() for c in spelling):
                continue
            if normalize_name(spelling) != target:
                continue
            seen.append(_Seen(spelling, bool(form), index, len(seen)))
    return seen


def spelled_in(sec_name: str, texts: Sequence[str]) -> tuple[str, int] | None:
    """Return the company's own spelling in `texts` (newest first) and the first text that has it."""
    seen = _spellings(sec_name, texts)
    pool = [s for s in seen if s.has_form] or seen
    if not pool:
        return None
    counts = Counter(s.spelling for s in pool)
    first: dict[str, _Seen] = {}
    for item in pool:
        first.setdefault(item.spelling, item)
    # Most frequent; on a tie the longer legal form ("Company N.V." over "Company"); then the first seen.
    best = max(counts, key=lambda spelling: (counts[spelling], len(spelling), -first[spelling].order))
    return best, first[best].text_index


def own_spelling(sec_name: str, texts: Sequence[str]) -> str | None:
    """Return the company's own spelling of its SEC name in `texts`, newest first, or None."""
    found = spelled_in(sec_name, texts)
    return found[0] if found else None


def display_name(sec_name: str, texts: Sequence[str]) -> str:
    """Return the name readers see: the company's own spelling, else the SEC name without its suffix."""
    return own_spelling(sec_name, texts) or _plain_sec_name(sec_name)


def _plain_sec_name(sec_name: str) -> str:
    """Return the SEC name without its state suffix, and without control or format characters."""
    return " ".join(printable(without_state_suffix(sec_name)).split()) or sec_name


@dataclass
class NamesReport:
    """What one run of the display-name step did."""

    companies: int = 0
    unchanged: int = 0  # same inputs as last time: nothing read
    changed: int = 0  # name or its release changed
    from_documents: int = 0  # computed this run from the company's own spelling
    documents_read: int = 0
    documents_unreadable: int = 0
    writes_failed: int = 0  # names the database refused; each company is written on its own
    stopped_for_time: bool = False

    def summary(self) -> dict[str, Any]:
        """Return the counts as plain JSON."""
        return {
            "companies": self.companies,
            "unchanged": self.unchanged,
            "changed": self.changed,
            "from_documents": self.from_documents,
            "documents_read": self.documents_read,
            "documents_unreadable": self.documents_unreadable,
            "writes_failed": self.writes_failed,
            "stopped_for_time": self.stopped_for_time,
        }


@dataclass(frozen=True)
class _Release:
    id: UUID
    sha256: str
    snapshot_key: str
    content_type: str


@dataclass(frozen=True)
class _Stored:
    id: UUID
    sec_name: str
    display_name: str | None
    document_id: UUID | None
    inputs: str | None


def _releases(session: Session) -> dict[UUID, list[_Release]]:
    """Return each company's newest own press releases, newest first."""
    link, document = TABLES[CompanyDocument], TABLES[SourceDocument]
    query = (
        select(
            link.c.company_id,
            document.c.id,
            document.c.sha256,
            document.c.snapshot_key,
            document.c.content_type,
        )
        .join(document, document.c.id == link.c.source_document_id)
        .where(link.c.matched_by == DocumentMatch.SEC_CIK, document.c.document_type.contains(PRESS_RELEASE))
        .order_by(link.c.company_id, document.c.published_at.desc().nulls_last(), document.c.id)
    )
    found: defaultdict[UUID, list[_Release]] = defaultdict(list)
    for row in session.execute(query):
        if len(found[row.company_id]) < RELEASES_READ:
            found[row.company_id].append(_Release(row.id, row.sha256, row.snapshot_key, row.content_type))
    return found


def _read(store: SnapshotStore, release: _Release, report: NamesReport) -> str | None:
    try:
        text = document_text(store.get(release.snapshot_key), release.content_type)
    except (SnapshotStoreError, httpx.HTTPError, OSError, ValueError) as error:
        report.documents_unreadable += 1
        log_event(
            _log, "display_name_unreadable", snapshot_key=release.snapshot_key, error=type(error).__name__
        )
        return None
    report.documents_read += 1
    return text


def _computed(
    company: _Stored, releases: list[_Release], store: SnapshotStore, report: NamesReport
) -> tuple[str, UUID | None, bool]:
    """Return the display name, the release that spells it, and whether every release was read."""
    read = [(release, _read(store, release, report)) for release in releases]
    texts = [(release, text) for release, text in read if text is not None]
    found = spelled_in(company.sec_name, [text for _, text in texts])
    complete = len(texts) == len(read)
    if found is None:
        return _plain_sec_name(company.sec_name), None, complete
    report.from_documents += 1
    return found[0], texts[found[1]][0].id, complete


def _write(session: Session, write: dict[str, Any], report: NamesReport) -> None:
    """Store one company's name in its own savepoint, so a name the database refuses skips only it."""
    table = TABLES[Company]
    try:
        with session.begin_nested():
            session.execute(
                update(table)
                .where(table.c.id == write["id"])
                .values(
                    display_name=write["name"],
                    display_name_document_id=write["document"],
                    display_name_inputs=write["inputs"],
                )
            )
    except SQLAlchemyError as error:
        report.writes_failed += 1
        log_event(_log, "display_name_not_stored", company_id=str(write["id"]), error=type(error).__name__)


def recompute_display_names(
    session_factory: sessionmaker[Session],
    store: SnapshotStore,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> NamesReport:
    """Store every company's display name; safe to re-run. Past `deadline`, the rest wait a run."""
    table = TABLES[Company]
    report = NamesReport()
    version = code_version(__name__)
    with session_factory() as session:
        releases = _releases(session)
        query = select(
            table.c.id,
            table.c.name,
            table.c.display_name,
            table.c.display_name_document_id,
            table.c.display_name_inputs,
        ).order_by(table.c.cik)
        companies = [_Stored(*row) for row in session.execute(query)]
    writes: list[dict[str, Any]] = []
    for company in companies:
        mine = releases.get(company.id, [])
        digest = input_digest(
            version, company.sec_name, TEXT_VERSION, [(r.id, r.sha256, r.content_type) for r in mine]
        )
        if company.inputs == digest and company.display_name is not None:
            report.companies += 1
            report.unchanged += 1
            continue
        if deadline is not None and clock() > deadline:
            report.stopped_for_time = True
            break
        report.companies += 1
        name, document_id, complete = _computed(company, mine, store, report)
        if (name, document_id) != (company.display_name, company.document_id):
            report.changed += 1
        writes.append(
            {"id": company.id, "name": name, "document": document_id, "inputs": digest if complete else None}
        )
    with session_factory.begin() as session:
        for write in writes:
            _write(session, write, report)
    log_event(_log, "display_names_computed", **report.summary())
    return report
