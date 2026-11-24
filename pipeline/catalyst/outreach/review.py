"""The pre-publication review's mechanical checks (owner order 2026-11-24; ADR 0055, decision 7).

`docs/issue-review.md` lists every check. The checks here are plain code over the built issue,
its pages, and the promotional note. Nothing reaches the owner until all of them pass and the
fresh-eyes reviewer has read the result. A check never changes the issue: a failure is fixed in
code or in the data and the issue is built again.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from html import unescape
from urllib.parse import urlsplit

from catalyst.api.access import FREE_HORIZON_DAYS
from catalyst.outreach.digest import Digest

NOTE_OPENING = "In this week's Catalyst Tracker newsletter: "
NOTE_LINK = "[paste free post link]"
NOTE_LINK_LENGTH = 23  # X counts every link as 23 characters
NOTE_MAX = 280

# Words the owner banned from the note (2026-11-24), and stock phrases of machine-written copy.
NOTE_BANNED = (
    "biotech catalysts",
    "every entry links",
    "sec sentence",
    "delve",
    "unlock",
    "game-changer",
    "game changer",
    "landscape",
    "navigate",
    "buckle up",
    "dive in",
    "here's",
    "stay tuned",
    "don't miss",
    "in today's",
    "key takeaways",
    "crucial",
    "pivotal",
    "groundbreaking",
    "revolutioniz",
    "seamless",
    "robust",
    "testament",
    "tapestry",
    "realm",
    "embark",
    "elevate",
    "harness",
    "supercharge",
    "a must",
    "you won't want to",
    "not just",
)
ADVICE = ("buy", "sell", "price target", "should invest", "upside", "bullish", "bearish", "outperform")

_LINK = re.compile(r'href="([^"]*)"')
_SOURCE_HOSTS = frozenset({"www.sec.gov", "sec.gov", "clinicaltrials.gov", "www.clinicaltrials.gov"})
_WHEN = re.compile(r"When: ([^<]*)")
_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_BAD_WHEN = (
    (re.compile(rf"\bin ({_MONTHS}) \d{{1,2}}\b"), "a month with days reads 'on', not 'in'"),
    (re.compile(r"\b(this|next) year\b"), "a year must be named"),
    (re.compile(r"\bin (in|by|during|throughout|later|late this)\b"), "two prepositions"),
    (re.compile(rf"\bin ({_MONTHS})\.$"), "a month with no year"),
    (re.compile(r"\bin (first|second|third|fourth) (half|quarter)\b"), "missing 'the'"),
)
_NOTE_MARKS = (
    ("note_dash", "[\u2013\u2014]", "no en or em dashes"),
    ("note_punctuation", "!", "no exclamation marks"),
    ("note_question", "\\?", "no questions"),
    ("note_hashtag", "#", "no hashtags"),
    ("note_emoji", "[\U0001f300-\U0001faff\u2600-\u27bf]", "no emojis"),
    ("note_construction", r"\bnot\b[^.]{0,40}\bit'?s\b", "no 'not X, it's Y'"),
)
_STATE_SUFFIX = re.compile(r"[\\/]\s*[A-Za-z]{2,3}[\\/]?\s*$")
_SHOUTING = re.compile(r"[A-Z]{4,}")


@dataclass(frozen=True)
class Finding:
    """One failed check: which, where, and what to fix."""

    check: str
    where: str
    detail: str


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub("[\u2010-\u2015]", "-", text)
    return " ".join(text.lower().split())


def review_digest(digest: Digest) -> list[Finding]:
    """Check each listed entry's data: quote, ticker, name, dates, and duplicates (practice A)."""
    findings: list[Finding] = []
    horizon = digest.issue_date + timedelta(days=FREE_HORIZON_DAYS)
    seen: set[tuple[int, str]] = set()
    entries = [move.catalyst for move in digest.moved] + list(digest.new) + list(digest.upcoming)
    for entry in entries:
        where = f"{entry.company.name} | {entry.title}"
        window = entry.window
        quotes = " ".join(_norm(source.quote) for source in entry.sources)
        if window.raw_text and _norm(window.raw_text) not in quotes:
            findings.append(
                Finding("window_not_in_quote", where, f"no source quote holds {window.raw_text!r}")
            )
        ticker = entry.company.ticker or ""
        if len(ticker) == 5 and ticker[-1] in "WURZ":
            findings.append(
                Finding("ticker_not_common_stock", where, f"{ticker}: warrant, unit, right or CVR")
            )
        name = entry.company.name
        if _STATE_SUFFIX.search(name) or (name == name.upper() and _SHOUTING.search(name)):
            findings.append(
                Finding("raw_company_name", where, "SEC conformed name; use the company's own spelling")
            )
        if window.end < digest.issue_date:
            findings.append(Finding("window_ended", where, f"window ended {window.end.isoformat()}"))
        if entry.status not in ("upcoming", "slipped"):
            findings.append(Finding("not_open", where, f"status {entry.status}"))
        key = (entry.company.cik, entry.title.lower())
        if key in seen:
            findings.append(Finding("duplicate", where, "listed twice"))
        seen.add(key)
        if digest.access == "free" and window.start > horizon:
            findings.append(Finding("paid_only_in_free", where, "starts after the free 30 days"))
    return findings


def review_page(page: str) -> list[Finding]:
    """Check a built page or fragment: links, When lines, the notice, and advice words (B, C, D)."""
    findings: list[Finding] = []
    for link in _LINK.findall(page):
        address = unescape(link)
        parts = urlsplit(address)
        fragment_ok = parts.fragment.startswith(":~:text=") and len(parts.fragment) > len(":~:text=")
        if parts.scheme != "https" or parts.hostname not in _SOURCE_HOSTS or not fragment_ok:
            findings.append(Finding("bad_link", address[:120], "not a source address with a quote fragment"))
    for line in _WHEN.findall(page):
        text = unescape(line).strip()
        for pattern, why in _BAD_WHEN:
            if pattern.search(text):
                findings.append(Finding("when_grammar", text, why))
    body = re.sub(r"(?s)^.*?<body>|</body>.*$", "", page)
    if body.count("<small>") != 1:
        findings.append(Finding("notice_missing", "page", "the notice must appear once, in small print"))
    elif not re.search(r"<p><small>[^<]*</small></p>\s*$", body):
        findings.append(Finding("notice_not_last", "page", "the small-print notice must be the last element"))
    text = _norm(unescape(re.sub(r"(?s)<small>.*?</small>", " ", re.sub(r"(?s)<head>.*?</head>", " ", body))))
    text = _norm(re.sub(r"<[^>]+>", " ", text))
    for word in ADVICE:
        if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", text):
            findings.append(Finding("advice_words", word, "advice wording outside the notice"))
    return findings


def review_note(note: str) -> list[Finding]:
    """Check the promotional note against the owner's rules of 2026-11-24 (practice E)."""
    note = note.rstrip("\n")
    findings: list[Finding] = []

    def fail(check: str, detail: str) -> None:
        findings.append(Finding(check, "note", detail))

    if not note.startswith(NOTE_OPENING):
        fail("note_opening", f"must start {NOTE_OPENING!r}")
    if not note.endswith(NOTE_LINK):
        fail("note_link", f"must end {NOTE_LINK!r}")
    length = len(note) - (len(NOTE_LINK) if note.endswith(NOTE_LINK) else 0) + NOTE_LINK_LENGTH
    if length > NOTE_MAX:
        fail("note_length", f"{length} characters counting the link as {NOTE_LINK_LENGTH}")
    body = note[: -len(NOTE_LINK)].rstrip() if note.endswith(NOTE_LINK) else note
    statements = body.split("\n\n")
    if any("\n" in s for s in statements) or "\n\n\n" in note:
        fail("note_layout", "statements are separated by exactly one blank line")
    if not re.search(r"\b\d+\b[^.]*\bprograms?\b", statements[-1]):
        fail("note_count", "the last statement gives the count of programs added from last week")
    low = note.lower()
    findings += [
        Finding("note_banned_words", "note", w)
        for w in NOTE_BANNED + ADVICE
        if re.search(rf"(?<![a-z]){re.escape(w)}", low)
    ]
    findings += [
        Finding(check, "note", why)
        for check, pattern, why in _NOTE_MARKS
        if re.search(pattern, note if check != "note_construction" else low)
    ]
    return findings
