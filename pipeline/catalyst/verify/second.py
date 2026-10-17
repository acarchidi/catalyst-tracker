"""The second reading: compare the verifier's catalysts with the extractor's (ADR 0018, part 2).

The verifier reads the same release with a different prompt and does not see the first answer.
Plain code then pairs each extracted catalyst with one of the verifier's, as the gold-set scorer
pairs proposals with gold events: the quotes overlap, or both give the same type and window.
Each verifier catalyst vouches for one extracted catalyst at most. A pair passes only when the
type, the window, and the drug all agree.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from catalyst.verify.rules import CheckResult, Outcome

CHECK = "second_reading_agrees"
VERIFIER_ROLE = "verifier"
VERIFIER_PROMPT = ("verify_catalysts", "1.0.0")

Window = tuple[str | None, str | None, str | None]  # start, end, precision, as a claim stores them


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
    return re.sub(r"[^0-9a-z]", "", (drug or "").casefold())


def _never(_a: str, _b: str) -> bool:
    return False


def _overlap(a: Reading, b: Reading) -> int:
    return max(0, min(a.quote_end, b.quote_end) - max(a.quote_start, b.quote_start))


def _closeness(first: Reading, second: Reading) -> tuple[int, int]:
    shared = _overlap(first, second)
    same_event = first.catalyst_type == second.catalyst_type and first.window == second.window
    if not shared and not same_event:
        return (0, 0)
    return (int(first.catalyst_type == second.catalyst_type), shared + int(same_event))


def _shown(window: Window) -> str:
    start, end, precision = (part or "none" for part in window)
    return f"{start} to {end} ({precision})"


def _differences(first: Reading, second: Reading, same_drug: Callable[[str, str], bool]) -> list[str]:
    found: list[str] = []
    if first.catalyst_type != second.catalyst_type:
        found.append(f"type {first.catalyst_type}, second reading {second.catalyst_type}")
    if first.window != second.window:
        found.append(f"window {_shown(first.window)}, second reading {_shown(second.window)}")
    drugs_agree = _name(first.drug) == _name(second.drug) or (
        first.drug is not None and second.drug is not None and same_drug(first.drug, second.drug)
    )
    if not drugs_agree:
        found.append(f"drug {first.drug or 'none'}, second reading {second.drug or 'none'}")
    return found


def compare(
    first: Sequence[Reading],
    second: Sequence[Reading],
    same_drug: Callable[[str, str], bool] = _never,
) -> dict[UUID, CheckResult]:
    """Return each extracted catalyst's outcome against the verifier's catalysts from the same document.

    `same_drug` says whether two different names are one drug in the reference tables.
    """
    free = list(second)
    results: dict[UUID, CheckResult] = {}
    for reading in first:
        best = max(free, key=lambda other: _closeness(reading, other), default=None)
        if best is None or _closeness(reading, best) == (0, 0):
            results[reading.claim_id] = CheckResult(
                CHECK, Outcome.FAIL, "the second reading did not find this event"
            )
            continue
        free.remove(best)
        found = _differences(reading, best, same_drug)
        if found:
            reason = "the second reading differs: " + "; ".join(found)
            results[reading.claim_id] = CheckResult(CHECK, Outcome.FAIL, reason)
        else:
            results[reading.claim_id] = CheckResult(CHECK, Outcome.PASS)
    return results
