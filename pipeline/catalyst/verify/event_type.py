"""A claim's catalyst type must be stated by its quote (CP4 review, O1; ADR 0018, part 5d).

CP4 found a topline sentence published as an FDA decision date: no check read the type against
the quote, so only the second reading could catch a type swap. The check
`type_named_near_quote` is plain code over a fixed table of words per catalyst type (`WORDS`):
- the quote holds words for the claimed type: pass;
- the quote holds words for other types only: fail, since it states another kind of event;
- the quote holds words for an FDA decision date or an advisory meeting, and the claim is a
  readout, a submission, or an acceptance: it passes only on that type's strong words (`STRONG`:
  "topline", "readout"; "submit", "submission"; "accept", "validate"). A sentence about an FDA
  date names "data" or "BLA" in passing ("The BLA, supported by data from RAINIER, has a PDUFA
  target action date of August 28, 2027"), so those weak words do not make it a readout or a
  submission (CP4 round 2);
- the quote holds no type's words at all, as a table row does ("SKYLINE Part A Ulcerative
  Colitis SPY003 Sept 2026"): it passes only when the claimed type is the last kind of event
  named in the `LOOK_BACK` characters before it, in the stored copy (the table's lead-in,
  "expected topline readouts"), as the drug check takes the drug named last. Only then is the
  copy read.

Anything else fails, so a missing word holds the claim for a person. Words are whole words, case
not counting, except short acronyms (IND, NDA, CRL), which must be in capitals. The check can
pass a wrong type whose words the quote also holds ("present ... data" is a conference
presentation and a readout); the second reading compares the type too.

`LABELS` gives each type the fixed words a published title starts with (`catalyst.publish.title`).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.extract.agent import ROLE
from catalyst.extract.text import document_text
from catalyst.logs import log_event
from catalyst.schemas import CatalystType, Claim, SourceDocument
from catalyst.snapshots import SnapshotStore, SnapshotStoreError
from catalyst.verify.rules import CheckResult, Outcome
from catalyst.verify.run import store_check

CHECK = "type_named_near_quote"
# How far before a quote that names no kind of event its type's words may be. The gold set's one
# such quote, a table row, is 135 characters after "topline readouts" in the table's lead-in.
LOOK_BACK = 300

T = CatalystType
# Words that say each kind of event, as regular expressions; each is matched as a whole word, with
# case not counting. `(?-i:...)` keeps an acronym in capitals, so "index" is not "IND".
WORDS: Mapping[CatalystType, tuple[str, ...]] = MappingProxyType(
    {
        T.PDUFA_DATE: (
            r"(?-i:PDUFA)",
            r"(?:target\s+)?action\s+date",
            r"(?:FDA|approval|regulatory)\s+decision",
            r"decision\s+date",
            r"(?:goal|target)\s+date",
            # "complete its review", "FDA review", "Priority Review": the agency's review (CP4 round 2)
            r"(?:complete[sd]?|completing|conclude[sd]?|concluding)\s+(?:its|the|their)\s+review",
            r"(?:FDA|regulatory|priority|standard)\s+review",
            r"user\s+fee",
        ),
        T.ADVISORY_COMMITTEE: (r"advisory\s+committees?", r"advisory\s+panel", r"(?-i:AdCom)"),
        T.TOPLINE_READOUT: (r"top-?\s?line", r"data", r"results?", r"read-?\s?outs?", r"read\s+out"),
        T.CONFERENCE_PRESENTATION: (
            r"present(?:s|ed|ing|ation|ations)?",
            r"conferences?",
            r"congress(?:es)?",
            r"symposi(?:um|a)",
            r"posters?",
            r"abstracts?",
            r"(?:annual|scientific|medical)\s+meetings?",
            r"late-?breaking",
        ),
        T.REGULATORY_SUBMISSION: (
            r"submit(?:s|ted|ting)?",
            r"submissions?",
            r"fil(?:e|es|ed|ing|ings)",
            r"applications?",
            r"(?-i:IND|NDA|BLA|MAA|CTA|sNDA|sBLA)s?",
        ),
        T.REGULATORY_ACCEPTANCE: (r"accept(?:s|ed|ing|ance)?", r"validat(?:e|es|ed|ion)"),
        T.CHMP_OPINION: (
            r"(?-i:CHMP)",
            r"Committee\s+for\s+Medicinal\s+Products\s+for\s+Human\s+Use",
            r"opinions?",
        ),
        T.COMPLETE_RESPONSE_LETTER: (r"complete\s+response", r"(?-i:CRL)s?"),
        T.LABEL_EXPANSION: (
            # "open-label" names a trial design, and "dose expansion" or "expanded access" a cohort or
            # a program, not a label (CP4 round 2), so "expansion" alone is not a word for this type.
            r"(?<!open-)(?<!open\s)label(?:s|ing)?",
            r"supplemental",
            r"(?-i:sNDA|sBLA)s?",
            r"(?:new|additional|expanded)\s+indications?",
        ),
        T.ENROLLMENT_COMPLETION: (
            r"enrol(?:l|ls|led|ling|lment|lments|ment|ments)",
            r"last\s+(?:patient|participant|subject)s?",
        ),
        T.OFFERING: (
            r"offerings?",
            r"private\s+placements?",
            r"registered\s+direct",
            r"proceeds",
            r"underwrit(?:ten|er|ers|ing)",
            r"at-the-market",
            r"(?-i:ATM)",
            r"priced",
        ),
        T.SHELF_FILING: (r"shelf", r"Form\s+[SF]-3", r"(?-i:[SF]-3)", r"registration\s+statements?"),
        T.MERGER_ACQUISITION: (
            r"merg(?:e|es|ed|er|ers|ing)",
            r"acqui(?:re|res|red|ring|sition|sitions)",
            r"tender\s+offer",
            r"business\s+combination",
            r"definitive\s+agreement",
        ),
        T.EARNINGS: (
            r"earnings",
            r"financial\s+results",
            r"quarterly\s+results",
            r"results\s+of\s+operations",
            r"fiscal",
        ),
    }
)
# The fixed words a title starts with, and how a reason names the type.
LABELS: Mapping[CatalystType, str] = MappingProxyType(
    {
        T.PDUFA_DATE: "FDA decision date",
        T.ADVISORY_COMMITTEE: "Advisory committee meeting",
        T.TOPLINE_READOUT: "Topline data",
        T.CONFERENCE_PRESENTATION: "Conference presentation",
        T.REGULATORY_SUBMISSION: "Regulatory submission",
        T.REGULATORY_ACCEPTANCE: "Regulatory filing acceptance",
        T.CHMP_OPINION: "CHMP opinion",
        T.COMPLETE_RESPONSE_LETTER: "Complete response letter",
        T.LABEL_EXPANSION: "Label expansion",
        T.ENROLLMENT_COMPLETION: "Enrollment completion",
        T.OFFERING: "Offering",
        T.SHELF_FILING: "Shelf registration",
        T.MERGER_ACQUISITION: "Merger or acquisition",
        T.EARNINGS: "Earnings report",
    }
)
# An article for the types a reason names with one ("an FDA decision date"); the rest take none.
_ARTICLE = frozenset(
    {
        T.PDUFA_DATE,
        T.ADVISORY_COMMITTEE,
        T.CONFERENCE_PRESENTATION,
        T.REGULATORY_SUBMISSION,
        T.REGULATORY_ACCEPTANCE,
        T.CHMP_OPINION,
        T.COMPLETE_RESPONSE_LETTER,
        T.LABEL_EXPANSION,
        T.OFFERING,
        T.SHELF_FILING,
        T.MERGER_ACQUISITION,
        T.EARNINGS,
    }
)
# The FDA's own events. A quote with their words is about them, unless it names a readout, a
# submission, or an acceptance by one of that type's strong words below.
DOMINANT = frozenset({T.PDUFA_DATE, T.ADVISORY_COMMITTEE})
STRONG: Mapping[CatalystType, tuple[str, ...]] = MappingProxyType(
    {
        T.TOPLINE_READOUT: (r"top-?\s?line", r"read-?\s?outs?", r"read\s+out"),
        T.REGULATORY_SUBMISSION: (r"submit(?:s|ted|ting)?", r"submissions?"),
        T.REGULATORY_ACCEPTANCE: (r"accept(?:s|ed|ing|ance)?", r"validat(?:e|es|ed|ion)"),
    }
)


def _pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)(?:" + "|".join(words) + r")(?!\w)", re.IGNORECASE)


_PATTERNS: Mapping[CatalystType, re.Pattern[str]] = MappingProxyType(
    {kind: _pattern(words) for kind, words in WORDS.items()}
)
_STRONG: Mapping[CatalystType, re.Pattern[str]] = MappingProxyType(
    {kind: _pattern(words) for kind, words in STRONG.items()}
)
if set(WORDS) != set(CatalystType) or set(LABELS) != set(CatalystType):
    raise RuntimeError("every catalyst type needs its words and its label")

_log = logging.getLogger(__name__)


def types_named(text: str) -> frozenset[CatalystType]:
    """Return every kind of event the text holds words for."""
    return frozenset(kind for kind, pattern in _PATTERNS.items() if pattern.search(text))


def last_named(text: str) -> frozenset[CatalystType]:
    """Return the kind of event the text names last: the type whose words start latest.

    More than one only when one word is a word of each ("sNDA"); empty when it names none.
    """
    starts = {
        kind: max(m.start() for m in found)
        for kind, pattern in _PATTERNS.items()
        if (found := list(pattern.finditer(text)))
    }
    latest = max(starts.values(), default=None)
    return frozenset(kind for kind, at in starts.items() if at == latest)


def _plain(kind: CatalystType) -> str:
    """Return a type's label as a sentence uses it: "topline data", "FDA decision date"."""
    label = LABELS[kind]
    return label if label[:2].isupper() else label[0].lower() + label[1:]


def _named(kind: CatalystType) -> str:
    """Return a type as a reason names it: "an FDA decision date", "topline data"."""
    shown = _plain(kind)
    if kind not in _ARTICLE:
        return shown
    return f"{'an' if shown[0] in 'aeiouAEIOUF' else 'a'} {shown}"


def _failed(reason: str) -> CheckResult:
    return CheckResult(CHECK, Outcome.FAIL, reason)


def needs_copy(quote: str) -> bool:
    """Say whether the check must read the copy: only when the quote names no kind of event."""
    return not types_named(quote)


def type_named(text: str | None, quote: str, start: int, end: int, catalyst_type: object) -> CheckResult:
    """Pass when the quote holds words for the claim's type (see the module text).

    `text` is the stored copy's text; it is read only when the quote names no kind of event, and
    may be None otherwise.
    """
    try:
        kind = CatalystType(catalyst_type)
    except ValueError:
        return _failed(f"'{catalyst_type}' is not a catalyst type")
    found = types_named(quote)
    if kind in found:
        dominant = sorted(found & DOMINANT)
        if kind in _STRONG and dominant and not _STRONG[kind].search(quote):
            named = " or ".join(_plain(k) for k in dominant)
            return _failed(f"the quote has words for {named}, and only passing words for {_plain(kind)}")
        return CheckResult(CHECK, Outcome.PASS)
    if found:
        others = " or ".join(_plain(k) for k in sorted(found))
        return _failed(f"the quote has words for {others}, not for {_named(kind)}")
    if text is None:
        return _failed("the quote names no kind of event, and the stored copy was not read")
    if text[start:end] != quote:
        return _failed("the quote is not at its stored place in the stored copy")
    last = last_named(text[max(0, start - LOOK_BACK) : start])
    if kind in last:
        return CheckResult(CHECK, Outcome.PASS)
    if last:
        named = " or ".join(_plain(k) for k in sorted(last))
        return _failed(
            f"the quote names no kind of event, and the last kind named in the {LOOK_BACK} characters "
            f"before it is {named}, not {_named(kind)}"
        )
    return _failed(
        f"the quote names no kind of event, and the {LOOK_BACK} characters before it have no words "
        f"for {_named(kind)}"
    )


# --- The job ----------------------------------------------------------------------------------


@dataclass
class TypeReport:
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
    store: SnapshotStore, snapshot_key: str, content_type: str, report: TypeReport
) -> str | CheckResult:
    try:
        text = document_text(store.get(snapshot_key), content_type)
    except (SnapshotStoreError, httpx.HTTPError, OSError, ValueError) as error:
        report.documents_unreadable += 1
        log_event(_log, "type_check_unreadable", snapshot_key=snapshot_key, error=type(error).__name__)
        return _failed(f"the stored copy of the source could not be read ({type(error).__name__})")
    report.documents_read += 1
    return text


def _kind(value: object) -> object:
    return value.get("catalyst_type") if isinstance(value, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def check_type_named(
    session_factory: sessionmaker[Session], store: SnapshotStore, now: Callable[[], datetime]
) -> TypeReport:
    """Store `type_named_near_quote` for every extractor claim; safe to re-run.

    A document is read once per run, and only when one of its quotes names no kind of event.
    """
    claim, document = TABLES[Claim], TABLES[SourceDocument]
    report = TypeReport()
    outcomes: Counter[str] = Counter()
    at = now()
    with session_factory.begin() as session:
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
        texts: dict[Any, str | CheckResult] = {}
        for row in session.execute(query).all():
            text: str | CheckResult | None = None
            if needs_copy(row.quote):
                if row.source_document_id not in texts:
                    texts[row.source_document_id] = _read(store, row.snapshot_key, row.content_type, report)
                text = texts[row.source_document_id]
            if isinstance(text, CheckResult):
                result = text
            else:
                result = type_named(text, row.quote, row.quote_start, row.quote_end, _kind(row.value))
            store_check(session, row.id, result, at)
            report.claims += 1
            outcomes[f"{result.check}:{result.outcome.value}"] += 1
    report.outcomes = dict(outcomes)
    log_event(_log, "type_named_checked", **report.summary())
    return report
