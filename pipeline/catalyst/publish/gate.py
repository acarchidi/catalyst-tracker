"""The publish decision: a short rule over stored check outcomes and the stored copy (ADR 0018, part 5).

Spec, publish rule: a claim publishes only when the quote match, the second extraction, and the
rule checks pass, and a second source agrees or the source is a regulator or SEC filing (the
`independent_source` check). Anything else waits for review with every reason. A required check
with no stored row has not passed (ADR 0018). The quote is matched again here, against the
stored copy, rather than read from a row (spec: Provenance).

Left to later parts: `window_not_past` applies only before first publication (5b, with the
catalyst writer); prompt-injection fixtures (5c).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from uuid import UUID

import httpx

from catalyst.extract.agent import ROLE
from catalyst.extract.text import TEXT_VERSION, document_text
from catalyst.schemas import CheckOutcome, Claim, ClaimCheck, GateDecision
from catalyst.snapshots import (
    SnapshotIntegrityError,
    SnapshotNotFoundError,
    SnapshotStore,
    SnapshotStoreError,
    snapshot_key,
)
from catalyst.verify import independent, second
from catalyst.verify.rules import RULES, CheckResult, ClaimFacts

QUOTE_CHECK = "quote_matches_stored_copy"

# Each rule names its own check; an empty claim is enough to ask it, so the names live in one place.
_ASK = ClaimFacts(id=UUID(int=0), value={}, quote="", published=date.min, matches=(), trial_phases={})
RULE_CHECKS: tuple[str, ...] = tuple(rule(_ASK, date.min).check for rule in RULES)
# Added by PR #49 (`catalyst.verify.advisory.CHECK`), not on main yet. Until then no claim has
# this row, so every claim waits for review. Import the constant once that pull request merges.
ADVISORY_CHECK = "advisory_meeting_noticed"
REQUIRED_CHECKS: tuple[str, ...] = (*RULE_CHECKS, second.CHECK, independent.CHECK, ADVISORY_CHECK)

_PASSING = (CheckOutcome.PASS, CheckOutcome.NOT_APPLICABLE)


@dataclass(frozen=True)
class UnreadableCopy:
    """Why the stored copy of a claim's source could not be read."""

    reason: str


@dataclass(frozen=True)
class Hold:
    """One reason a claim waits for review, and the check it comes from."""

    check: str
    reason: str
    missing: bool = False

    @property
    def label(self) -> str:
        """A short form for counting: which check, and whether it failed or never ran."""
        return f"{self.check} has not run" if self.missing else f"{self.check} failed"


@dataclass(frozen=True)
class Decision:
    """The gate's decision on one claim, with every hold that keeps it from publishing."""

    decision: GateDecision
    holds: tuple[Hold, ...] = ()

    @property
    def reasons(self) -> tuple[str, ...]:
        """Every reason, in plain words, in the order the gate found them."""
        return tuple(hold.reason for hold in self.holds)


def read_copy(store: SnapshotStore, key: str, sha256: str, content_type: str) -> str | UnreadableCopy:
    """Return the stored copy's text exactly as the extractor computed it, or why it cannot be read."""
    if key != snapshot_key(sha256):
        return UnreadableCopy("the stored copy's key does not match the document's recorded hash")
    try:
        return document_text(store.get(key), content_type)
    except SnapshotNotFoundError:
        return UnreadableCopy("the stored copy is missing")
    except SnapshotIntegrityError:
        return UnreadableCopy("the stored copy no longer matches its hash")
    except (SnapshotStoreError, httpx.HTTPError, ValueError):
        return UnreadableCopy("the stored copy could not be read")


def match_quote(claim: Claim, copy: str | UnreadableCopy) -> CheckResult:
    """Check that the claim's quote is, character for character, at its stored place in the copy.

    The offsets hold only for the text version they were found with; any other version fails.
    """
    if isinstance(copy, UnreadableCopy):
        return CheckResult(QUOTE_CHECK, CheckOutcome.FAIL, copy.reason)
    value = claim.value if isinstance(claim.value, dict) else {}
    version = value.get("text_version")
    if not isinstance(version, str) or not version:
        return CheckResult(
            QUOTE_CHECK, CheckOutcome.FAIL, "the claim does not say which text version it used"
        )
    if version != TEXT_VERSION:
        return CheckResult(
            QUOTE_CHECK,
            CheckOutcome.FAIL,
            f"the quote's place was found with text version {version}, not the current "
            f"{TEXT_VERSION}; it must be found again",
        )
    if copy[claim.quote_start : claim.quote_end] != claim.quote:
        return CheckResult(
            QUOTE_CHECK, CheckOutcome.FAIL, "the quote is not at its stored place in the stored copy"
        )
    return CheckResult(QUOTE_CHECK, CheckOutcome.PASS)


def decide(claim: Claim, checks: Iterable[ClaimCheck], quote_ok: CheckResult) -> Decision:
    """Decide one claim: publish only when the quote matched and every required check passed or did not apply.

    `checks` are the claim's stored rows; rows of checks no longer required are ignored.
    """
    if quote_ok.check != QUOTE_CHECK:
        raise ValueError(f"the quote result must come from {QUOTE_CHECK}, not {quote_ok.check}")
    stored: dict[str, ClaimCheck] = {}
    for row in checks:
        if row.claim_id != claim.id:
            raise ValueError(f"a {row.check_name} row belongs to another claim")
        if row.check_name in stored:
            raise ValueError(f"the {row.check_name} check is listed twice")
        stored[row.check_name] = row
    if claim.agent != ROLE or not claim.field.startswith("catalyst:"):
        return Decision(
            GateDecision.REVIEW,
            (Hold("extractor_claim", "only the extractor's catalyst claims can publish"),),
        )
    holds: list[Hold] = []
    if quote_ok.outcome is not CheckOutcome.PASS:
        holds.append(Hold(QUOTE_CHECK, quote_ok.reason or f"{QUOTE_CHECK} did not pass"))
    for name in REQUIRED_CHECKS:
        row = stored.get(name)
        if row is None:
            holds.append(Hold(name, f"check {name} has not run", missing=True))
        elif row.outcome not in _PASSING:
            holds.append(Hold(name, f"{name}: {row.reason or 'failed'}"))
    return Decision(GateDecision.REVIEW, tuple(holds)) if holds else Decision(GateDecision.PUBLISH)
