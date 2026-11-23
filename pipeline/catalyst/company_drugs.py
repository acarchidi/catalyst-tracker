"""Read the drug words of a claim into the names a drug may carry (ADR 0060, parts 2 and 3).

A claim's `drug` is free text the extractor wrote. Plain code reads it with the drug list's own
name rules (`catalyst.drug_list.registry_names`, ADR 0035), a few more form words, and a few
more refusals:
- a strength, route, or form at either end is not part of the name ("X foam 0.3%" is X);
- a bracket set off by a space gives a second name of the same drug ("BRAND (generic)");
- a list ("A, B and C") or a combination ("A plus B", "A with B") names each of its drugs;
- a phrase, a stand-in, a description, a trial, a target, a date, a congress, an agency, or an
  advice word names no drug, and neither does a name of more than `MOST_CHARS` characters.
When any part names no drug, the whole names none, so the claim waits for a person.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from catalyst.drug_list import ListedName, registry_names
from catalyst.drug_list_words import CLAIM_FORM_WORDS, CLAIM_STOP_WORDS

STOP_WORDS = CLAIM_STOP_WORDS
MOST_CHARS = 60  # a longer name is a description
MOST_DRUGS = 8  # a longer list is not one claim's drugs
# Prefixes of things shaped like code names that are not drugs: diseases, targets, years, quarters;
# medical congresses, agencies, and filings ("ASCO 2027", "FDA 2027"; CP4 round 2); and advice
# words, which must never reach a title (spec: no buy or sell ratings).
NOT_DRUG_PREFIXES = frozenset(
    {"COVID", "SARS", "IL", "CD", "FY", "CY", "Q", "H", "NCT", "ICD", "GLP", "HER", "PD"}
    | {"ASCO", "ESMO", "ASH", "AACR", "EASL", "AASLD", "FDA", "EMA", "PDUFA", "NDA", "BLA", "IND"}
    | {"BUY", "SELL"}
)
# A year (1900 to 2099), or a half or quarter ("2H26", "1Q2027"), names a date, not a drug.
_DATE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)|(?<![^\W_])[1-4][HQ]\d{2,4}(?![^\W_])", re.IGNORECASE)
# What joins the drugs of a list or a combination; "/" and "or" are left alone, so they name none.
_JOINERS = re.compile(
    r"(?:\s*(?:[+,;&]|(?<![^\W_])(?:in\s+combination\s+with|and|plus|with)(?![^\W_])))+\s*", re.IGNORECASE
)
_COMBINES = re.compile(r"\+|plus|with", re.IGNORECASE)
_LETTERS = re.compile(r"[^\W\d_]+")
_WORDS = re.compile(r"[^\W_]+")


@dataclass(frozen=True)
class Stated:
    """The drugs a claim's drug words state, each with the names given it; `combined` for "A plus B"."""

    drugs: tuple[tuple[ListedName, ...], ...]
    combined: bool = False


def _is_name(name: ListedName) -> bool:
    words = {w.casefold() for w in (*_WORDS.findall(name.name), *name.name.split())}
    prefix = _LETTERS.match(name.name)
    return not (
        len(name.name) > MOST_CHARS
        or words & STOP_WORDS
        or _DATE.search(name.name)
        or (prefix is not None and prefix.group(0).upper() in NOT_DRUG_PREFIXES)
    )


def stated_drugs(text: str) -> Stated | None:
    """Return each drug the words state with its names, the main one first; None when any part names none."""
    text = text.strip()
    bracketed = "(" in text or "[" in text
    parts = [text] if bracketed else _JOINERS.split(text)
    if not text or len(parts) > MOST_DRUGS:
        return None
    drugs: list[tuple[ListedName, ...]] = []
    for part in parts:
        names = registry_names(part, CLAIM_FORM_WORDS) or []
        if not names or not names[0].main or not all(_is_name(name) for name in names):
            return None
        drugs.append(tuple(names))
    combined = not bracketed and any(_COMBINES.search(joiner) for joiner in _JOINERS.findall(text))
    return Stated(tuple(drugs), combined)
