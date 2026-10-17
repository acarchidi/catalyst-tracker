"""Score the extractor against hand-checked catalysts (spec: Gold set; ADR 0014).

A proposal matches a gold event when their quotes overlap in the text (the same sentence), or,
failing that, when both name the same type and the same window. Recall counts gold events
found; precision counts proposals that are gold events. Each field is then scored on the
matched pairs. Totals add events across documents, so a long release weighs more.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from catalyst.extract.agent import EXTRACTOR_PROMPT, ROLE, ExtractedCatalyst, extract_catalysts
from catalyst.extract.dates import DateWindow, parse_window
from catalyst.extract.text import QuoteSpan, document_text, find_quote
from catalyst.llm.client import ModelClient
from catalyst.llm.config import RoleConfig
from catalyst.llm.prompts import Prompt
from catalyst.schemas.enums import CatalystType


@dataclass(frozen=True)
class GoldCatalyst:
    """One hand-checked event: its type, drug, date words, and quote, as the source writes them."""

    catalyst_type: CatalystType
    drug: str | None
    date_text: str
    quote: str


@dataclass(frozen=True)
class DocumentScore:
    """Counts for one document."""

    gold: int
    proposed: int
    matched: int
    type_right: int
    window_right: int
    drug_right: int
    quote_found: int


@dataclass(frozen=True)
class Scores:
    """Each score from 0 to 1, rounded to three places. CI blocks a drop in any of them."""

    recall: float
    precision: float
    catalyst_type: float
    window: float
    drug: float
    quote_found: float


def _same_drug(a: str | None, b: str | None) -> bool:
    def norm(name: str | None) -> str:
        return re.sub(r"[^0-9a-z]", "", (name or "").casefold())

    return norm(a) == norm(b)


def _overlap(a: QuoteSpan, b: QuoteSpan) -> int:
    return max(0, min(a.end, b.end) - max(a.start, b.start))


def _key(window: DateWindow | None) -> tuple[date, date, str] | None:
    return (window.start, window.end, window.precision.value) if window else None


def score_document(
    text: str, published: date, gold: list[GoldCatalyst], proposals: list[ExtractedCatalyst]
) -> DocumentScore:
    """Match proposals to gold events in one document and count what is right."""
    gold_spans: list[QuoteSpan] = []
    for event in gold:
        span = find_quote(text, event.quote)
        if span is None:
            raise ValueError(f"gold quote not found in the document: {event.quote[:60]!r}")
        gold_spans.append(span)
    gold_windows = [_key(parse_window(event.date_text, published)) for event in gold]
    free = list(range(len(proposals)))
    pairs: list[tuple[int, int]] = []
    for g, event in enumerate(gold):

        def closeness(p: int, g: int = g, event: GoldCatalyst = event) -> tuple[int, int]:
            proposal = proposals[p]
            shared = _overlap(proposal.quote, gold_spans[g]) if proposal.quote else 0
            same_event = (
                proposal.catalyst_type is event.catalyst_type and _key(proposal.window) == gold_windows[g]
            )
            if not shared and not same_event:
                return (0, 0)
            return (int(proposal.catalyst_type is event.catalyst_type), shared + int(same_event))

        best = max(free, key=closeness, default=None)
        if best is not None and closeness(best) != (0, 0):
            pairs.append((g, best))
            free.remove(best)
    return DocumentScore(
        gold=len(gold),
        proposed=len(proposals),
        matched=len(pairs),
        type_right=sum(proposals[p].catalyst_type is gold[g].catalyst_type for g, p in pairs),
        window_right=sum(_key(proposals[p].window) == gold_windows[g] for g, p in pairs),
        drug_right=sum(_same_drug(proposals[p].drug, gold[g].drug) for g, p in pairs),
        quote_found=sum(p.quote is not None for p in proposals),
    )


def _share(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 1.0


def total(documents: list[DocumentScore]) -> Scores:
    """Add the counts across documents and turn them into scores."""
    gold = sum(d.gold for d in documents)
    proposed = sum(d.proposed for d in documents)
    matched = sum(d.matched for d in documents)
    return Scores(
        recall=_share(matched, gold),
        precision=_share(matched, proposed),
        catalyst_type=_share(sum(d.type_right for d in documents), matched),
        window=_share(sum(d.window_right for d in documents), matched),
        drug=_share(sum(d.drug_right for d in documents), matched),
        quote_found=_share(sum(d.quote_found for d in documents), proposed),
    )


@dataclass(frozen=True)
class GoldDocument:
    """One recorded press release and the events a person confirmed in it."""

    sha256: str
    published: date
    content_type: str
    catalysts: tuple[GoldCatalyst, ...]


def load_gold(path: Path) -> list[GoldDocument]:
    """Read the gold set: one JSON object per line (evals/extract_catalysts/README.md)."""
    documents: list[GoldDocument] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry: dict[str, Any] = json.loads(line)
        events = tuple(
            GoldCatalyst(CatalystType(c["catalyst_type"]), c["drug"], c["date_text"], c["quote"])
            for c in entry["catalysts"]
        )
        documents.append(
            GoldDocument(
                entry["sha256"], date.fromisoformat(entry["published"]), entry["content_type"], events
            )
        )
    return documents


def gold_fingerprint(path: Path) -> str:
    """Return the SHA-256 of the gold set file, so a score names the exact set it was earned on."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate(
    client: ModelClient,
    gold: list[GoldDocument],
    copies: Path,
    *,
    role: str = ROLE,
    prompt_ref: tuple[str, str] = EXTRACTOR_PROMPT,
) -> tuple[Scores, list[DocumentScore]]:
    """Run a reader on every gold document (copies in `copies/<sha256>.htm`) and score it.

    The extractor is the default; the verifier's prompt is scored the same way, on the same set.
    """
    results: list[DocumentScore] = []
    for document in gold:
        text = document_text((copies / f"{document.sha256}.htm").read_bytes(), document.content_type)
        extraction = extract_catalysts(client, text, document.published, role=role, prompt_ref=prompt_ref)
        results.append(
            score_document(text, document.published, list(document.catalysts), list(extraction.catalysts))
        )
    return total(results), results


EVALS = Path(__file__).resolve().parents[3] / "evals" / "extract_catalysts"
# The verifier's second reading is scored on the same gold set; its scores sit in their own folder.
VERIFY_EVALS = EVALS.parent / "verify_catalysts"
REPOSITORY = EVALS.parents[1]
FIRST_VERSION = "1.0.0"


def prompt_fingerprint(prompt: Prompt) -> str:
    """Return the SHA-256 of what the model is sent from the prompt file: its instructions and schema.

    A score names this fingerprint, so editing a prompt's words without a new version is caught.
    """
    content = json.dumps({"system": prompt.system, "output_schema": prompt.output_schema}, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()


def _identity(prompt: Prompt, role: RoleConfig) -> dict[str, object]:
    """Return what earned a score: the prompt and every role setting that changes answers."""
    return {
        "prompt_id": prompt.id,
        "prompt_version": prompt.version,
        "prompt_sha256": prompt_fingerprint(prompt),
        "model": role.model,
        "effort": role.effort,
        "max_tokens": role.max_tokens,
    }


def scores_record(
    scores: Scores, prompt: Prompt, role: RoleConfig, gold_path: Path, documents: int
) -> dict[str, Any]:
    """Return what `scores.json` holds: the scores and exactly what earned them."""
    return {
        **_identity(prompt, role),
        "gold_sha256": gold_fingerprint(gold_path),
        "documents": documents,
        "scores": asdict(scores),
    }


def _mismatches(name: str, record: dict[str, Any], expected: dict[str, object]) -> list[str]:
    return [
        f"{name} names {key} {record.get(key)!r}, not the current {value!r}"
        for key, value in expected.items()
        if record.get(key) != value
    ]


def gate_problems(
    prompt: Prompt,
    role: RoleConfig,
    evals: Path = EVALS,
    gold: Path | None = None,
    *,
    allow_unscored: bool = True,
) -> list[str]:
    """Return why the current prompt and model may not ship; empty when they may.

    Scored: `scores.json` must name exactly this prompt (id, version, and the fingerprint of its
    words), this model with its effort and token limit, and this gold set. No score may be
    below `baseline.json`, which must exist and name every score. Unscored: allowed only for
    the first prompt version, and only for the exact prompt words and role settings recorded
    in `unscored.json`. So a new version, edited words, another
    model, or another gold set all fail until new scores are committed (ADR 0014).

    The verifier passes `allow_unscored=False`: its second reading never runs without scores
    (review of PR #31). The gold set is `evals/gold.jsonl` if that folder has one, otherwise the
    set the prompt file names.
    """
    expected = _identity(prompt, role)
    scores_path = evals / "scores.json"
    if not scores_path.exists() and not allow_unscored:
        return [f"{prompt.id} has no scores.json: run `catalyst eval-extractor --prompt {prompt.id}`"]
    if not scores_path.exists():
        if prompt.version != FIRST_VERSION:
            return [f"prompt version {prompt.version} has no scores; run `catalyst eval-extractor`"]
        unscored_path = evals / "unscored.json"
        if not unscored_path.exists():
            return ["no scores.json and no unscored.json: the prompt has never been checked"]
        return _mismatches("unscored.json", json.loads(unscored_path.read_text(encoding="utf-8")), expected)
    record: dict[str, Any] = json.loads(scores_path.read_text(encoding="utf-8"))
    if gold is None:
        gold = evals / "gold.jsonl" if (evals / "gold.jsonl").exists() else REPOSITORY / prompt.eval_set
    expected["gold_sha256"] = gold_fingerprint(gold)
    problems = _mismatches("scores.json", record, expected)
    baseline_path = evals / "baseline.json"
    if not baseline_path.exists():
        return [*problems, "scores.json exists but baseline.json does not: there is no floor to check"]
    floor: dict[str, float] = json.loads(baseline_path.read_text(encoding="utf-8"))
    missing = sorted(set(Scores.__dataclass_fields__) - set(floor))
    if missing:
        problems.append(f"baseline.json names no floor for {', '.join(missing)}")
    for name, minimum in floor.items():
        earned = float(record["scores"].get(name, 0.0))
        if earned < minimum:
            problems.append(f"{name} fell to {earned}, below the baseline {minimum}")
    return problems
