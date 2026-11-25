"""The second reading: compare the verifier's catalysts with the extractor's (ADR 0018, part 2; ADR 0062).

The verifier reads the same release with a different prompt and does not see the first answer.
Plain code then pairs each extracted catalyst with one of the verifier's: the quotes overlap, or,
in different sentences, both give the same type, complete window, and named drug. Each verifier
catalyst vouches for one extracted catalyst at most. A pair passes only when the type, the
window, and the drug all agree; a missing window or a missing drug never agrees, except that a
deal need not name a drug.

What agrees (ADR 0062). The window: both close on the same day and the second reading's opens no
earlier than the claim's ("by Q3 2027" and "Q3 2027"), since the claim then says no more than the
second reading confirms; a claim that opens later, or closes on another day, still fails. The
drug: the two write one drug in two ways, by a second name in brackets ("Zentra (zentocel)"),
form words ("gel 1%"), or another word joining the same drugs ("X + Y", "X in combination
with Y"); "X with Y" is never "X". A verifier catalyst its own checks flagged is paired, so the
reason says it was found, but it never vouches.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from catalyst.verify.rules import CheckResult, Outcome

CHECK = "second_reading_agrees"
VERIFIER_ROLE = "verifier"
VERIFIER_PROMPT = ("verify_catalysts", "1.0.0")

NOT_FOUND = "the second reading did not find this event"
FLAGGED = "the second reading found this event, but its own checks flagged it: "
DIFFERS = "the second reading differs: "

Window = tuple[str | None, str | None, str | None]  # start, end, precision, as a claim stores them
_NO_DRUG_TYPE = "merger_acquisition"
SameDrug = Callable[[str, str], bool]
# A second name for the drug before it: brackets after a name and a space ("Zentra (zentocel)").
# Brackets inside a name are part of it: "(R)-zentocel" is not "(S)-zentocel".
_ASIDE = re.compile(r"(?<=\S)\s+\(([^()]*)\)")
_BRACKETS = re.compile(r"\([^()]*\)")
# Words that join the drugs of a combination. "and" is left out: it may list two events' drugs.
_JOIN = re.compile(r"\s+(?:in\s+combination\s+with|combined\s+with|with|plus)\s+|\s*\+\s*", re.IGNORECASE)
# Marks and words that list or join two drugs; a string with one outside its brackets is never one
# name, so its bracketed names never stand for it (`_one_name`).
_LISTING = re.compile(r"[,/&;+]|\b(?:and|or|with|plus|combined|combination|versus|vs)\b", re.IGNORECASE)
# Words at either end of a name that say its form or route, or that a name follows, never which
# drug it is. A drug's forms share its one drug record. No short forms ("IV", "SC"): "collagen IV".
_DESCRIBING = frozenset(
    [
        "candidate",
        "capsule",
        "capsules",
        "cream",
        "film",
        "formerly",
        "gel",
        "infusion",
        "inhalation",
        "inhaled",
        "injectable",
        "injection",
        "intramuscular",
        "intravenous",
        "intravitreal",
        "nasal",
        "ointment",
        "oral",
        "powder",
        "previously",
        "program",
        "solution",
        "spray",
        "subcutaneous",
        "sublingual",
        "suspension",
        "tablet",
        "tablets",
        "topical",
    ]
)
_STRENGTH = re.compile(r"\d[\d.,]*(?:mg|mcg|\u00b5g|ug|ml|g|kg|iu|%)")
_SHORTEST_NAME = 3  # letters and digits in a name found in brackets or left after form words


@dataclass(frozen=True)
class Reading:
    """One catalyst as a stored claim states it, with where its quote sits in the document.

    `problems` are the reader's own checks on it, as short codes; only a reading with none vouches.
    """

    claim_id: UUID
    catalyst_type: str
    drug: str | None
    window: Window
    quote_start: int
    quote_end: int
    problems: tuple[str, ...] = ()


def name_key(drug: str | None) -> str:
    """Return a drug name with case, spaces, hyphens, and punctuation removed."""
    # Case, spaces, hyphens, and punctuation do not matter ("VX-548" is "vx 548"); every letter and
    # digit does, in any script (IFN-alpha written with the Greek letter is not IFN-beta).
    return re.sub(r"[\W_]", "", unicodedata.normalize("NFKC", drug or "").casefold())


def _never(_a: str, _b: str) -> bool:
    return False


def _complete(window: Window) -> bool:
    return all(window)


def _dates(window: Window) -> tuple[date, date] | None:
    """Return the window's two days, or None when it is not two dates in order."""
    try:
        start, end = date.fromisoformat(window[0] or ""), date.fromisoformat(window[1] or "")
    except ValueError:
        return None
    return (start, end) if start <= end else None


def _windows_agree(first: Reading, second: Reading) -> bool:
    """Say whether both windows close on the same day and the second reading's opens no earlier (ADR 0062).

    The claim's window is the one published. "By Q3 2027" runs from the release to the end of
    September; a second reading of "Q3 2027" closes on the same day and only opens later, so the
    claim says nothing the second reading does not confirm. A claim that opens later than the
    second reading, or closes on another day, says something else; a missing window never agrees.
    """
    mine, theirs = _dates(first.window), _dates(second.window)
    return mine is not None and theirs is not None and mine[0] <= theirs[0] and mine[1] == theirs[1]


def _overlap(a: Reading, b: Reading) -> int:
    return max(0, min(a.quote_end, b.quote_end) - max(a.quote_start, b.quote_start))


def _bare(name: str) -> str:
    """Return a name without the form, route, and strength words at its ends; never an empty name."""
    words = name.split()

    def describes(word: str) -> bool:
        plain = word.strip(".,;:").casefold()
        return plain in _DESCRIBING or _STRENGTH.fullmatch(plain) is not None

    while len(words) > 1 and describes(words[-1]):
        words.pop()
    while len(words) > 1 and describes(words[0]):
        words.pop(0)
    return " ".join(words)


def _names(drug: str) -> tuple[str, list[str]]:
    """Return a drug's main name and the other names it gives in brackets ("A (B, C)")."""
    others = [_bare(name) for aside in _ASIDE.findall(drug) for name in aside.split(",")]
    return _bare(_ASIDE.sub(" ", drug)), others


def _one_name(drug: str) -> bool:
    """Say whether a string is one name, at most with one bracketed name after it ("NAME (OTHER)").

    Outside its brackets it may hold form words and marks, but no listing mark or word, and no
    joining word: "X and Y (Z)", "X/Y (Z)", "X, Y (Z)", "X & Y (Z)", "X with Y (Z)" are lists.
    """
    groups = _BRACKETS.findall(drug)
    if len(groups) > 1 or (groups and len(_ASIDE.findall(drug)) != 1):
        return False
    return _LISTING.search(_BRACKETS.sub(" ", drug)) is None


def _same_name(a: str, b: str, same_drug: SameDrug) -> bool:
    """Say whether two names are one drug: the same main name, or one's main name in the other's brackets.

    Two names that share only a name in brackets do not agree: brackets also hold form words.
    A list's or combination's brackets name one of its drugs, not the whole ("X and Keytruda
    (pembrolizumab)" is not pembrolizumab), so bracketed names count only when both are one name
    (`_one_name`); a combination's parts are matched one by one in `_drugs_agree`.
    """
    if name_key(a) == name_key(b) or same_drug(a, b):
        return True
    if not (_one_name(a) and _one_name(b)):
        return False
    (main_a, others_a), (main_b, others_b) = _names(a), _names(b)
    tries = [(main_a, main_b)]
    tries += [(main_a, other) for other in others_b] + [(other, main_b) for other in others_a]
    return any(
        min(len(name_key(x)), len(name_key(y))) >= _SHORTEST_NAME
        and (name_key(x) == name_key(y) or same_drug(x, y))
        for x, y in tries
    )


def _parts(drug: str) -> list[str]:
    """Return the drugs of "X with Y plus Z", in order; joining words in brackets do not count."""
    masked = _BRACKETS.sub(lambda found: "_" * len(found.group()), drug)
    cuts = [0, *(edge for found in _JOIN.finditer(masked) for edge in found.span()), len(drug)]
    parts = [drug[start:end].strip() for start, end in zip(cuts[::2], cuts[1::2], strict=True)]
    return parts if all(name_key(part) for part in parts) else [drug]


def _drugs_agree(first: Reading, second: Reading, same_drug: SameDrug) -> bool:
    if not name_key(first.drug) and not name_key(second.drug):
        # A deal need not name a drug; any other event with no drug in either reading goes to review.
        return first.catalyst_type == second.catalyst_type == _NO_DRUG_TYPE
    return drug_names_agree(first.drug, second.drug, same_drug)


def drug_names_agree(first: str | None, second: str | None, same_drug: SameDrug = _never) -> bool:
    """Say whether two drug names are one drug or the same combination (ADR 0062); a missing name never is.

    Also used to find two entries of one event (ADR 0081), so both rules always read drugs alike.
    """
    a, b = first or "", second or ""
    if not name_key(a) or not name_key(b):
        return False
    if _same_name(a, b, same_drug):
        return True
    # A combination: the same drugs in the same order, whatever word joins them. "X with Y" is not
    # "X": a drug alone and in a combination are two programmes with their own dates (ADR 0062).
    mine, theirs = _parts(a), _parts(b)
    return len(mine) == len(theirs) > 1 and all(
        _same_name(x, y, same_drug) for x, y in zip(mine, theirs, strict=True)
    )


def _closeness(first: Reading, second: Reading, same_drug: SameDrug) -> tuple[int, ...] | None:
    """Rank a possible pair, best first; None when the two cannot be the same event.

    They are the same event when their quotes overlap, or, in different sentences, when they
    give the same type, the same complete window, and the same named drug. A pair that passes
    comes first, and among those the one with the very same window, then the very same name.
    """
    shared = _overlap(first, second)
    same_type = first.catalyst_type == second.catalyst_type
    same_window = _complete(first.window) and first.window == second.window
    drugs = _drugs_agree(first, second, same_drug)
    if not shared and not (same_type and same_window and drugs):
        return None
    windows = _windows_agree(first, second)
    clean = not second.problems
    passes = clean and same_type and windows and drugs
    return (
        int(passes),
        int(passes and same_window),
        int(passes and name_key(first.drug) == name_key(second.drug)),
        int(same_type),
        int(windows),
        int(drugs),
        int(clean),
        shared,
    )


def _shown(window: Window) -> str:
    start, end, precision = (part or "none" for part in window)
    return f"{start} to {end} ({precision})"


def _differences(first: Reading, second: Reading, same_drug: SameDrug) -> list[str]:
    found: list[str] = []
    if first.catalyst_type != second.catalyst_type:
        found.append(f"type {first.catalyst_type}, second reading {second.catalyst_type}")
    if not _windows_agree(first, second):
        found.append(f"window {_shown(first.window)}, second reading {_shown(second.window)}")
    if not _drugs_agree(first, second, same_drug):
        found.append(f"drug {first.drug or 'none'}, second reading {second.drug or 'none'}")
    return found


_Candidate = tuple[tuple[int, ...], Reading, Reading]


def _best_first(candidate: _Candidate) -> tuple[object, ...]:
    """Sort key: the closest pair first; ties go to the earlier quotes, then by claim id.

    The key depends only on the readings, never on list order, so the pairing is the same however
    either list is ordered.
    """
    rank, a, b = candidate
    tie = (a.quote_start, a.quote_end, b.quote_start, b.quote_end, str(a.claim_id), str(b.claim_id))
    return (tuple(-x for x in rank), tie)


def pairs(
    first: Sequence[Reading],
    second: Sequence[Reading],
    same_drug: SameDrug = _never,
) -> dict[UUID, Reading]:
    """Return the verifier's catalyst paired with each extracted catalyst that has one, closest first."""
    candidates: list[_Candidate] = [
        (rank, a, b) for a in first for b in second if (rank := _closeness(a, b, same_drug)) is not None
    ]
    candidates.sort(key=_best_first)
    paired: dict[UUID, Reading] = {}
    used: set[UUID] = set()
    for _, a, b in candidates:
        if a.claim_id not in paired and b.claim_id not in used:
            paired[a.claim_id] = b
            used.add(b.claim_id)
    return paired


def compare(
    first: Sequence[Reading],
    second: Sequence[Reading],
    same_drug: SameDrug = _never,
) -> dict[UUID, CheckResult]:
    """Return each extracted catalyst's outcome against the verifier's catalysts from the same document.

    `same_drug` says whether two different names are one drug in the reference tables. The best
    possible pairs are taken first, so the order of either list never changes an outcome.
    """
    paired = pairs(first, second, same_drug)
    results: dict[UUID, CheckResult] = {}
    for reading in first:
        match = paired.get(reading.claim_id)
        if match is None:
            results[reading.claim_id] = CheckResult(CHECK, Outcome.FAIL, NOT_FOUND)
        elif match.problems:
            # Found, but the verifier's own copy cannot vouch (ADR 0018): say so, with its codes.
            reason = FLAGGED + ", ".join(match.problems)
            results[reading.claim_id] = CheckResult(CHECK, Outcome.FAIL, reason)
        elif found := _differences(reading, match, same_drug):
            results[reading.claim_id] = CheckResult(CHECK, Outcome.FAIL, DIFFERS + "; ".join(found))
        else:
            results[reading.claim_id] = CheckResult(CHECK, Outcome.PASS)
    return results


def outcome_kind(result: CheckResult, second: Sequence[Reading]) -> str:
    """Name an outcome for counting: the run's summary carries these counts, never a claim's values."""
    if result.outcome is Outcome.PASS:
        return "agrees"
    reason = result.reason or ""
    if reason.startswith(FLAGGED):
        return "flagged"
    if reason.startswith(DIFFERS):
        return "differs"
    return "not_found" if second else "no_second_events"
