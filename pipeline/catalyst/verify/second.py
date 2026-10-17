"""The second reading: compare the verifier's catalysts with the extractor's (ADR 0018, part 2).

The verifier reads the same release with a different prompt and does not see the first answer.
Plain code then pairs each extracted catalyst with one of the verifier's: the quotes overlap, or,
in different sentences, both give the same type, complete window, and named drug. Each verifier
catalyst vouches for one extracted catalyst at most. A pair passes only when the type, the
window, and the drug all agree; a missing window or a missing drug never agrees, except that a
deal need not name a drug.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from catalyst.verify.rules import CheckResult, Outcome

CHECK = "second_reading_agrees"
VERIFIER_ROLE = "verifier"
VERIFIER_PROMPT = ("verify_catalysts", "1.0.0")

Window = tuple[str | None, str | None, str | None]  # start, end, precision, as a claim stores them
_NO_DRUG_TYPE = "merger_acquisition"


@dataclass(frozen=True)
class Reading:
    """One catalyst as a stored claim states it, with where its quote sits in the document."""

    claim_id: UUID
    catalyst_type: str
    drug: str | None
    window: Window
    quote_start: int
    quote_end: int


def _name(drug: str | None) -> str:
    # Case, spaces, hyphens, and punctuation do not matter ("VX-548" is "vx 548"); every letter and
    # digit does, in any script (IFN-alpha written with the Greek letter is not IFN-beta).
    return re.sub(r"[\W_]", "", unicodedata.normalize("NFKC", drug or "").casefold())


def _never(_a: str, _b: str) -> bool:
    return False


def _complete(window: Window) -> bool:
    return all(window)


def _overlap(a: Reading, b: Reading) -> int:
    return max(0, min(a.quote_end, b.quote_end) - max(a.quote_start, b.quote_start))


def _drugs_agree(first: Reading, second: Reading, same_drug: Callable[[str, str], bool]) -> bool:
    a, b = _name(first.drug), _name(second.drug)
    if not a and not b:
        # A deal need not name a drug; any other event with no drug in either reading goes to review.
        return first.catalyst_type == second.catalyst_type == _NO_DRUG_TYPE
    if not a or not b:
        return False
    return a == b or same_drug(first.drug or "", second.drug or "")


def _closeness(
    first: Reading, second: Reading, same_drug: Callable[[str, str], bool]
) -> tuple[int, ...] | None:
    """Rank a possible pair, best first; None when the two cannot be the same event.

    They are the same event when their quotes overlap, or, in different sentences, when they
    give the same type, the same complete window, and the same named drug.
    """
    shared = _overlap(first, second)
    same_type = first.catalyst_type == second.catalyst_type
    same_window = _complete(first.window) and first.window == second.window
    drugs = _drugs_agree(first, second, same_drug)
    if not shared and not (same_type and same_window and drugs):
        return None
    return (int(same_type and same_window and drugs), int(same_type), int(same_window), int(drugs), shared)


def _shown(window: Window) -> str:
    start, end, precision = (part or "none" for part in window)
    return f"{start} to {end} ({precision})"


def _differences(first: Reading, second: Reading, same_drug: Callable[[str, str], bool]) -> list[str]:
    found: list[str] = []
    if first.catalyst_type != second.catalyst_type:
        found.append(f"type {first.catalyst_type}, second reading {second.catalyst_type}")
    if not _complete(first.window) or first.window != second.window:
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


def compare(
    first: Sequence[Reading],
    second: Sequence[Reading],
    same_drug: Callable[[str, str], bool] = _never,
) -> dict[UUID, CheckResult]:
    """Return each extracted catalyst's outcome against the verifier's catalysts from the same document.

    `same_drug` says whether two different names are one drug in the reference tables. The best
    possible pairs are taken first, so the order of either list never changes an outcome.
    """
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
    results: dict[UUID, CheckResult] = {}
    for reading in first:
        match = paired.get(reading.claim_id)
        if match is None:
            results[reading.claim_id] = CheckResult(
                CHECK, Outcome.FAIL, "the second reading did not find this event"
            )
            continue
        found = _differences(reading, match, same_drug)
        if found:
            reason = "the second reading differs: " + "; ".join(found)
            results[reading.claim_id] = CheckResult(CHECK, Outcome.FAIL, reason)
        else:
            results[reading.claim_id] = CheckResult(CHECK, Outcome.PASS)
    return results
