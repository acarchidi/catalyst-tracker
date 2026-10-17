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

from catalyst.extract.agent import ExtractedCatalyst, extract_catalysts
from catalyst.extract.dates import DateWindow, parse_window
from catalyst.extract.text import QuoteSpan, document_text, find_quote
from catalyst.llm.client import ModelClient
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
    client: ModelClient, gold: list[GoldDocument], copies: Path
) -> tuple[Scores, list[DocumentScore]]:
    """Run the extractor on every gold document (copies in `copies/<sha256>.htm`) and score it."""
    results: list[DocumentScore] = []
    for document in gold:
        text = document_text((copies / f"{document.sha256}.htm").read_bytes(), document.content_type)
        extraction = extract_catalysts(client, text, document.published)
        results.append(
            score_document(text, document.published, list(document.catalysts), list(extraction.catalysts))
        )
    return total(results), results


EVALS = Path(__file__).resolve().parents[3] / "evals" / "extract_catalysts"
FIRST_VERSION = "1.0.0"


def scores_record(
    scores: Scores, prompt: tuple[str, str], model: str, gold_path: Path, documents: int
) -> dict[str, Any]:
    """Return what `scores.json` holds: the scores and exactly what earned them."""
    return {
        "prompt_id": prompt[0],
        "prompt_version": prompt[1],
        "model": model,
        "gold_sha256": gold_fingerprint(gold_path),
        "documents": documents,
        "scores": asdict(scores),
    }


def gate_problems(prompt: tuple[str, str], model: str, evals: Path = EVALS) -> list[str]:
    """Return why the current prompt and model may not ship; empty when they may.

    The first prompt version may ship unscored. Any later version, another model, or a changed
    gold set needs a `scores.json` earned on exactly that combination, and no score in it may be
    lower than in `baseline.json` (AGENTS.md: CI blocks a drop in any field's accuracy).
    """
    scores_path, baseline_path = evals / "scores.json", evals / "baseline.json"
    if not scores_path.exists():
        if prompt[1] == FIRST_VERSION:
            return []
        return [
            f"prompt version {prompt[1]} has no scores; run `catalyst eval-extractor` and commit scores.json"
        ]
    record: dict[str, Any] = json.loads(scores_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    expected = {
        "prompt_id": prompt[0],
        "prompt_version": prompt[1],
        "model": model,
        "gold_sha256": gold_fingerprint(evals / "gold.jsonl"),
    }
    for key, value in expected.items():
        if record.get(key) != value:
            problems.append(f"scores.json was earned with {key} {record.get(key)!r}, not {value!r}")
    if baseline_path.exists():
        floor: dict[str, float] = json.loads(baseline_path.read_text(encoding="utf-8"))
        for name, minimum in floor.items():
            earned = float(record["scores"].get(name, 0.0))
            if earned < minimum:
                problems.append(f"{name} fell to {earned}, below the baseline {minimum}")
    return problems
