"""Does a deal claim's quote date the closing, or another step of the deal? (ADR 0083).

A merger or acquisition has several dated steps before it closes: the stockholder vote at a special
meeting (or the stockholders' approval), the end of the HSR waiting period or another regulator's
clearance, the expiry of a tender offer, the record date for the vote. An extractor claim of type
`merger_acquisition` can carry any of these dates, and the writer would move an open entry's close
window to it (NextCure: a 9 December vote inside a fourth-quarter
close; Opus review of PR #164).

Plain code reads the quote. A claim may move an open deal entry's window only when its quote ties
closing words to the deal, as ADR 0080 ties completion words to it: a deal word and then the
closing verb ("the merger is expected to close", "the transaction ... to be consummated"), or the
closing words and then the deal word ("close the acquisition", "closing of the merger", "completion
of the acquisition"). "Close of business" and "closing conditions" are not closing words, and
"voting agreements" and "close of business" name no step. A quote
that names another step is held with that step's name, even beside closing words, since plain code
cannot tell which of the two its date is for. A step named only in a condition after the date words
("..., subject to approval at a special meeting and the expiration of the HSR waiting period")
dates nothing and is not read. When unsure, the window stays until a person approves the move:
every reason here is a yes-or-no question about the claim's own date (Opus review of PR #173, B2).
"""

from __future__ import annotations

import re

NO_CLOSING_WORDS = "the quote has no closing words for the deal, so its date may not be the closing"
_OTHER_STEP = "the quote dates another step of the deal ({steps}), not its closing"
_BESIDE = (
    "the quote names another step of the deal ({steps}) beside its closing, so its date may not be "
    "the closing"
)

_DASH = r"[\s\-\u2010-\u2015]+"
_DEAL = (
    r"(?:merger|acquisition|transaction|deal|business\s+combination|purchase|sale|divestiture|tender\s+offer)"
)
_ARTICLE = r"(?:(?:its|the|their|our|this|such)\s+)?"
_ANNOUNCED = r"(?:(?:previously[\s-]+(?:announced|disclosed|reported)|pending|proposed)\s+)?"
_CLOSE = (
    r"(?:clos(?:e|es|ed|ing)|consummat(?:e|es|ed|ing|ion)|complet(?:e|es|ed|ing|ion))\b"
    r"(?!\s+of\s+business)(?!\s+conditions?\b)"
)
# "The merger is expected to close", "the transaction ... to be consummated".
_DEAL_THEN_CLOSE = re.compile(rf"\b{_DEAL}\b[^.;:]{{0,120}}?\b{_CLOSE}", re.IGNORECASE)
# "close the acquisition", "closing of the merger", "completion of the proposed Mentari acquisition".
_CLOSE_THEN_DEAL = re.compile(
    rf"\b{_CLOSE}\s+(?:of\s+)?{_ARTICLE}{_ANNOUNCED}(?:[\w\-\u2019']+\s+){{0,2}}?{_DEAL}\b", re.IGNORECASE
)

# Each step that comes before the closing, by the words that name it, in the order reasons list them.
_STEPS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "a stockholder vote",
        re.compile(
            # "voting agreements" are signed support, not the vote (Opus review of PR #173, nit).
            r"\bvot(?:e|es|ed|ing)\b(?!\s+(?:and\s+support\s+)?agreements?\b)|"
            r"\b(?:special|annual)\s+meeting\b|\b(?:stockholders?|shareholders?)['\u2019]?\s+meeting\b|"
            # Their approval is the vote, even without the word (Opus review of PR #173, B1).
            r"\b(?:stockholders?|shareholders?)\b[^.;]{0,40}?\bapprov(?:e|es|ed|ing|al)\b|"
            r"\bapprov(?:e|es|ed|al)\b[^.;]{0,30}?\b(?:by|of)\s+(?:(?:the|its|our)\s+)?"
            r"(?:[\w\-\u2019']+\s+){0,2}?(?:stockholders?|shareholders?)\b|"
            r"\b(?:stockholder|shareholder)s?['\u2019]?\s+approvals?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "a waiting period or regulatory clearance",
        re.compile(
            rf"(?-i:\bHSR\b)|\bHart{_DASH}Scott{_DASH}Rodino\b|\bwaiting\s+period\b|"
            # A regulator's clearance or approval (Opus review of PR #173, B1).
            r"(?-i:\bFTC\b|\bDOJ\b)|\bFederal\s+Trade\s+Commission\b|\bDepartment\s+of\s+Justice\b|"
            r"\b(?:regulatory|antitrust|competition)\s+(?:clearances?|approvals?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "a tender offer's expiry",
        re.compile(
            r"\b(?:tender\s+offer|the\s+offer)\b[^.;]{0,120}?\b(?:expir\w*|extended)\b|"
            r"\bexpir\w*\s+(?:date\s+)?of\s+the\s+(?:tender\s+)?offer\b",
            re.IGNORECASE,
        ),
    ),
    (
        "a record date",
        # "Close of business" alone is a time of day, not the record date (Opus review of PR #173, nit).
        re.compile(r"\brecord\s+date\b|\bof\s+record\b", re.IGNORECASE),
    ),
)
# A condition that follows the date words: what it names is not what the date is for.
_CONDITION = re.compile(
    r"\b(?:subject\s+to|conditioned\s+(?:on|upon)|contingent\s+(?:on|upon)|conditions?\s+including)\b",
    re.IGNORECASE,
)


def _dating_part(quote: str, date_text: str | None) -> str:
    """Return the quote without a condition that follows its date words; the whole quote otherwise."""
    if not date_text:
        return quote
    at = quote.casefold().find(date_text.casefold())
    if at < 0:
        return quote
    condition = _CONDITION.search(quote, at + len(date_text))
    return quote[: condition.start()] if condition else quote


def has_closing_words(text: str) -> bool:
    """Say whether the text ties closing words to the deal (see the module text)."""
    return bool(_DEAL_THEN_CLOSE.search(text) or _CLOSE_THEN_DEAL.search(text))


def steps_named(text: str) -> list[str]:
    """Return the deal steps other than the closing that the text names, in a fixed order."""
    return [name for name, pattern in _STEPS if pattern.search(text)]


def _template(text: str) -> str:
    names = "|".join(re.escape(name) for name, _ in _STEPS)
    before, after = text.split("{steps}")
    return rf"{re.escape(before)}(?:{names})(?:, (?:{names}))*{re.escape(after)}"


_HOLD = re.compile(rf"{re.escape(NO_CLOSING_WORDS)}|{_template(_OTHER_STEP)}|{_template(_BESIDE)}")


def is_deal_step_hold(reason: str) -> bool:
    """Say whether a reason is one `deal_step_hold` gives, word for word."""
    return _HOLD.fullmatch(reason) is not None


def deal_step_hold(quote: str, date_text: str | None) -> str | None:
    """Return why a deal claim may not move an open entry's close window, or None when it may."""
    part = _dating_part(quote, date_text)
    steps, closing = steps_named(part), has_closing_words(part)
    if steps:
        return (_BESIDE if closing else _OTHER_STEP).format(steps=", ".join(steps))
    return None if closing else NO_CLOSING_WORDS
