"""Text in a source that is written to steer a model, not to inform a reader (ADR 0018, part 5c).

A press release informs people. Text in it that gives orders to "AI systems", claims a chat
role ("SYSTEM:"), or copies the extractor's own answer format is a prompt injection, whatever
it asks for. Plain code cannot tell which of the document's statements the injection changed,
so the extractor flags every proposal from such a document and a person reads it.

The patterns are deliberately narrow: none matches a recorded release in the gold set or the
test fixtures. They are one layer, not the defence: the quote, date, and second-reading checks
hold an injected value whether or not the words here are found.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

# Tag characters (U+E0020 to U+E007E) spell ASCII invisibly; a model may still read them.
_TAG_START, _TAG_END = 0xE0020, 0xE007E
_READER = r"(?:AI|A\.I\.|LLMs?|language[\s-]+models?|chatbots?|bots?|crawlers?|scrapers?|assistants?|models?)"
_MACHINE = (
    r"(?:AI|A\.I\.|automated|machine|language[\s-]+model|LLM)\s+"
    r"(?:systems?|readers?|agents?|models?|tools?|assistants?|parsers?|extractors?|verifiers?|pipelines?)"
)
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE | re.MULTILINE))
    for name, pattern in (
        (
            "orders to drop earlier instructions",
            r"\b(?:ignore|disregard|forget|override|overwrite)\s+(?:all\s+|any\s+)?(?:of\s+)?"
            r"(?:the\s+|your\s+|these\s+|those\s+|its\s+)?"
            r"(?:previous|prior|above|earlier|preceding|foregoing|original|system|other)\s+"
            r"(?:instructions?|prompts?|rules|directions|guidance|messages?|context)\b",
        ),
        ("a chat role label", r"^\s*(?:system|assistant)(?:\s+(?:prompt|message|note|notice|override))?\s*:"),
        (
            "a chat-format token",
            r"\[/?INST\]|<\|im_(?:start|end)\|>|<\|(?:system|assistant|user)\|>|<</?SYS>>",
        ),
        (
            "text addressed to machines",
            rf"\b(?:note|message|notice|instructions?|attention|memo)\s+(?:to|for)\s+"
            rf"(?:any\s+|all\s+|the\s+)?(?:{_MACHINE}|{_READER})\b",
        ),
        ("text addressed to machines", rf"\b(?:to|for)\s+(?:all\s+|any\s+|the\s+)?{_MACHINE}\s*[:,]"),
        ("text addressed to machines", rf"^\s*{_MACHINE}\s*[:,]"),
        ("text addressed to machines", rf"\b{_MACHINE}\s+(?:must|should|shall|are\s+to)\b"),
        ("a role given to the reader", rf"\byou\s+are\s+(?:now\s+)?(?:an?\s+|the\s+)?{_READER}\b"),
        (
            "an order to mark records as checked",
            r"\b(?:mark|flag|label)\s+(?:this|these|the|every|all|each|any)\s+(?:\w+\s+){0,3}"
            r"as\s+(?:verified|confirmed|passed|approved|publishable|trusted|correct)\b",
        ),
        (
            "an order to publish",
            r"\bpublish\s+(?:this|these|the|every|all|each)\s+(?:\w+\s+){0,2}"
            r"(?:claims?|records?|entries|values?|catalysts?)\b",
        ),
        (
            "an order to skip review",
            r"\b(?:skip|bypass)\s+(?:the\s+|any\s+|all\s+|human\s+)*(?:review|verification|checks?)\b"
            r"|\bwithout\s+(?:any\s+)?human\s+review\b",
        ),
        (
            "the tracker's own field or check names",
            r"\b(?:catalyst_type|date_text|window_start|window_end|window_precision|date_confidence|"
            r"quote_start|quote_end|text_version|extractor_checks|date_words_in_quote|"
            r"window_matches_words|names_matched|second_reading_agrees|independent_source|"
            r"company_named_in_source|advisory_meeting_noticed|safe_to_write|"
            r"quote_matches_stored_copy|tool_calls?|tool_use|function_call)\b",
        ),
        (
            "an answer or tool call in the reader's format",
            r"[{,\[]\s*\"(?:catalysts|catalyst_type|date_text|quote|tool_calls?|tool_use|arguments|"
            r"verified|publish|problems)\"\s*:",
        ),
        (
            "an answer or tool call in the reader's format",
            r"</?(?:tool_call|tool_use|function_calls?|invoke)\b",
        ),
    )
)


def plain(text: str) -> str:
    """Return the text as a model may read it: tag characters spelled out, other invisible marks removed.

    Compatibility forms (fullwidth letters) become ordinary letters, so they cannot hide a pattern.
    """
    spelled = "".join(chr(ord(ch) - 0xE0000) if _TAG_START <= ord(ch) <= _TAG_END else ch for ch in text)
    normal = unicodedata.normalize("NFKC", spelled)
    return "".join(ch for ch in normal if unicodedata.category(ch) != "Cf")


def instruction_markers(text: str) -> tuple[str, ...]:
    """Return each kind of model-steering text found in `text`, with the words found, in order."""
    readable = plain(text)
    found: list[str] = []
    for name, pattern in _PATTERNS:
        match = pattern.search(readable)
        if match:
            found.append(f"{name}: {' '.join(match.group(0).split())[:80]}")
    return tuple(found)


def invisible_characters(*texts: str | None) -> tuple[str, ...]:
    """Return the code points of every invisible formatting character in the texts.

    These are Unicode "format" characters: zero-width spaces and joiners, direction overrides
    and isolates, soft hyphens, the byte-order mark, and tag characters. A reader cannot see
    them, but code reads them, so the words a person checks can differ from the words stored.
    """
    seen = {f"U+{ord(ch):04X}" for text in texts if text for ch in text if unicodedata.category(ch) == "Cf"}
    return tuple(sorted(seen))


def hidden_letters(text: str, visible: str) -> bool:
    """Say whether the page hides any letter: `text` has letters that its visible text lacks.

    Hidden words can steer a reader, and every check that reads the whole text (the drug check
    looking just before a quote, the company check) would read them too. Hidden page numbers and
    lone hidden spaces are not words, so they do not count.
    """
    shown = Counter(ch for ch in visible if ch.isalpha())
    return bool(Counter(ch for ch in text if ch.isalpha()) - shown)
