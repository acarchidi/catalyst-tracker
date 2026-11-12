"""A catalyst's indication: one model reads, a second checks, plain code holds both (ADR 0053).

Owner decision 2026-11-10: "feed any ambiguous problems of this type to an LLM. then, have
another LLM check its work." So:

1. Plain code offers the candidate sentences of the catalyst's stored source that name its drug
   (`catalyst.extract.indication.candidates`).
2. The first reader (`indicate_catalysts`, under the extractor's role and cap) is given the
   drug, the catalyst's own quote, and the numbered candidates. It picks the one sentence that
   states which disease this event concerns, and copies the disease from it, or answers none.
3. Plain code checks the answer: the sentence is a candidate, the disease is written in it as
   given, with no brackets, and names one MeSH disease.
4. The second reader (`check_indication`, under the verifier's role and cap, with its own
   prompt) is given only the drug, the event, the chosen sentence, and the disease. It says
   whether the sentence states that this drug is aimed at this disease for this event, and if
   not, why (another drug's disease, a side effect, an earlier result, ...).

An indication stands only when both readers agree and every plain-code check passes. Neither
reader has tools or database access; both read only stored, visible source text (spec: Agent
architecture). Every refusal keeps its reason. A refused indication never holds the catalyst.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from catalyst.extract.agent import ROLE
from catalyst.extract.evaluate import GoldDocument, gold_fingerprint, prompt_fingerprint
from catalyst.extract.indication import (
    Found,
    IndicationMiss,
    candidates,
    hard_check,
    usable_drug,
)
from catalyst.extract.text import QuoteSpan, document_text, visible_text
from catalyst.llm.client import ModelClient
from catalyst.llm.config import RoleConfig, load_config
from catalyst.llm.prompts import Prompt, load_prompt
from catalyst.mesh import MeshIndex, Miss, normal_words
from catalyst.verify.second import VERIFIER_ROLE

READER_PROMPT = ("indicate_catalysts", "1.0.0")
CHECKER_PROMPT = ("check_indication", "1.0.0")
CONFIRMED = "states_target"


@dataclass(frozen=True)
class Verdict:
    """The second reader's answer."""

    states: bool
    reason: str


@dataclass(frozen=True)
class Outcome:
    """What became of one catalyst's indication: found, or the reason it is not, and the check's verdict."""

    result: Found | IndicationMiss | Miss
    verdict: Verdict | None = None


def _one_line(words: str) -> str:
    """Return stored words as one plain line: no line breaks, no invisible characters, at most 1000."""
    plain = "".join(ch if ch.isprintable() else " " if ch.isspace() else "" for ch in words)
    return " ".join(plain.split())[:1000]


def _field(words: str) -> str:
    """Return stored words as one JSON string, so source text cannot imitate the layout around it."""
    return json.dumps(_one_line(words), ensure_ascii=False)


def reader_input(drug: str, event: str, offered: Sequence[QuoteSpan]) -> str:
    """Return what the first reader reads: the drug, the event, then the numbered candidates."""
    lines = [f"[{number}] {_field(span.quote)}" for number, span in enumerate(offered, start=1)]
    return f"Drug: {_field(drug)}\nEvent: {_field(event)}\n\nCandidate sentences:\n" + "\n".join(lines)


def checker_input(drug: str, event: str, sentence: str, disease: str) -> str:
    """Return what the second reader reads: the drug, the event, the one sentence, and the disease."""
    return (
        f"Drug: {_field(drug)}\nEvent: {_field(event)}\n"
        f"Sentence: {_field(sentence)}\nDisease: {_field(disease)}"
    )


def find_indication(
    client: ModelClient,
    text: str,
    visible: str,
    drug: str,
    event: str,
    index: MeshIndex,
    *,
    reader_role: str = ROLE,
    checker_role: str = VERIFIER_ROLE,
) -> Outcome:
    """Find this catalyst's indication: offer, read, check by code, then check by the second reader."""
    if not usable_drug(drug):
        return Outcome(IndicationMiss.UNUSABLE_DRUG)
    if len({d for _, _, found in index.terms_in(normal_words(event)) for d in found}) > 1:
        return Outcome(IndicationMiss.EVENT_NAMES_SEVERAL)
    offered = candidates(text, visible, drug)
    if not offered:
        return Outcome(IndicationMiss.NO_CANDIDATE)
    read = client.run(reader_role, load_prompt(*READER_PROMPT), reader_input(drug, event, offered)).data
    number, disease = read.get("sentence"), read.get("disease")
    if number is None or not disease:
        return Outcome(IndicationMiss.NO_SENTENCE)
    if not isinstance(number, int) or not 1 <= number <= len(offered):
        return Outcome(IndicationMiss.NOT_A_CANDIDATE)
    sentence = offered[number - 1]
    checked = hard_check(sentence, str(disease), index)
    if not isinstance(checked, Found):
        return Outcome(checked)
    answer = client.run(
        checker_role, load_prompt(*CHECKER_PROMPT), checker_input(drug, event, sentence.quote, checked.words)
    ).data
    verdict = Verdict(bool(answer.get("states")), str(answer.get("reason")))
    if verdict.states and verdict.reason == CONFIRMED:
        return Outcome(checked, verdict)
    return Outcome(IndicationMiss.NOT_CONFIRMED, verdict)


# --- Scoring both readers (eval-extractor --prompt indicate_catalysts | check_indication) ---


@dataclass(frozen=True)
class GoldDisease:
    """One gold event's disease, and the MeSH names an answer may carry to count as right."""

    disease: str | None
    mesh: tuple[str, ...]


def load_diseases(path: Path) -> dict[str, list[GoldDisease]]:
    """Read the disease labels: one line per gold event, in the gold set's order."""
    rows: dict[str, list[tuple[int, GoldDisease]]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry: dict[str, Any] = json.loads(line)
            label = GoldDisease(entry["disease"], tuple(entry["mesh"]))
            rows.setdefault(entry["sha256"], []).append((int(entry["event"]), label))
    return {sha: [label for _, label in sorted(found, key=lambda row: row[0])] for sha, found in rows.items()}


@dataclass(frozen=True)
class ReaderScores:
    """How both readers and the hard checks together do on the gold events that name a drug.

    Recorded, not floored. `wrong` matters most: a wrong indication would be published.
    """

    events: int
    found_right: float  # of events with an accepted MeSH name, the share found with one of them
    none_right: float  # of events with none accepted, the share left without an indication
    wrong: float  # of all events, the share given a MeSH disease not accepted for it


@dataclass(frozen=True)
class CheckerScores:
    """How the second reader does on the planted cases; recorded, not floored."""

    cases: int
    accepts_true: float  # of true cases, the share it confirms
    refuses_false: float  # of false cases, the share it refuses


def _share(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 1.0


def evaluate_readers(
    client: ModelClient,
    gold: Sequence[GoldDocument],
    labels: dict[str, list[GoldDisease]],
    copies: Path,
    index: MeshIndex,
) -> tuple[ReaderScores, list[dict[str, Any]]]:
    """Run both readers on every gold event that names a drug; return scores and each answer."""
    answers: list[dict[str, Any]] = []
    for document in gold:
        if not document.catalysts:
            continue
        content = (copies / f"{document.sha256}.htm").read_bytes()
        text = document_text(content, document.content_type)
        visible = visible_text(content, document.content_type)
        for event, label in zip(document.catalysts, labels[document.sha256], strict=True):
            if not event.drug:
                continue
            outcome = find_indication(client, text, visible, event.drug, event.quote, index)
            found = outcome.result if isinstance(outcome.result, Found) else None
            answers.append(
                {
                    "sha256": document.sha256,
                    "drug": event.drug,
                    "gold": label.disease,
                    "accepted": list(label.mesh),
                    "words": found.words if found else None,
                    "mesh": found.descriptor.name if found else None,
                    "reason": None if found else str(outcome.result),
                    "verdict": asdict(outcome.verdict) if outcome.verdict else None,
                }
            )
    named = [a for a in answers if a["accepted"]]
    empty = [a for a in answers if not a["accepted"]]
    scores = ReaderScores(
        events=len(answers),
        found_right=_share(sum(a["mesh"] in a["accepted"] for a in named), len(named)),
        none_right=_share(sum(a["mesh"] is None for a in empty), len(empty)),
        wrong=_share(
            sum(a["mesh"] is not None and a["mesh"] not in a["accepted"] for a in answers), len(answers)
        ),
    )
    return scores, answers


def evaluate_checker(client: ModelClient, cases_path: Path) -> tuple[CheckerScores, list[dict[str, Any]]]:
    """Run the second reader on the planted cases (evals/check_indication/cases.jsonl)."""
    answers: list[dict[str, Any]] = []
    for line in cases_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case: dict[str, Any] = json.loads(line)
        reply = client.run(
            VERIFIER_ROLE,
            load_prompt(*CHECKER_PROMPT),
            checker_input(case["drug"], case["event"], case["sentence"], case["disease"]),
        ).data
        confirmed = bool(reply.get("states")) and reply.get("reason") == CONFIRMED
        answers.append(
            {
                "name": case["name"],
                "true": case["true"],
                "confirmed": confirmed,
                "reason": reply.get("reason"),
            }
        )
    true = [a for a in answers if a["true"]]
    false = [a for a in answers if not a["true"]]
    scores = CheckerScores(
        cases=len(answers),
        accepts_true=_share(sum(a["confirmed"] for a in true), len(true)),
        refuses_false=_share(sum(not a["confirmed"] for a in false), len(false)),
    )
    return scores, answers


def score_record(
    scores: ReaderScores | CheckerScores,
    prompts: Sequence[Prompt],
    roles: Sequence[RoleConfig],
    sets: Sequence[Path],
    answers: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a scores record: the scores, each answer, and exactly what earned them."""
    return {
        "prompts": [
            {"prompt_id": p.id, "prompt_version": p.version, "prompt_sha256": prompt_fingerprint(p)}
            for p in prompts
        ],
        "models": [{"model": r.model, "effort": r.effort, "max_tokens": r.max_tokens} for r in roles],
        "sets": {path.name: gold_fingerprint(path) for path in sets},
        "events": len(answers),
        "scores": asdict(scores),
        "answers": answers,
    }


# --- The gate before the readers run on live data (ADR 0053, decision 2) ---

EVALS_ROOT = Path(__file__).resolve().parents[3] / "evals"
# Which prompts, roles and sets earned each folder's scores, and which scores may only fall.
_SCORE_FIELDS = {
    "indicate_catalysts": frozenset(ReaderScores.__dataclass_fields__) - {"events"},
    "check_indication": frozenset(CheckerScores.__dataclass_fields__) - {"cases"},
}
_SCORED: dict[str, tuple[list[tuple[str, str]], list[str], list[str]]] = {
    "indicate_catalysts": (
        [READER_PROMPT, CHECKER_PROMPT],
        [ROLE, VERIFIER_ROLE],
        ["extract_catalysts/gold.jsonl", "indicate_catalysts/indications.jsonl"],
    ),
    "check_indication": ([CHECKER_PROMPT], [VERIFIER_ROLE], ["check_indication/cases.jsonl"]),
}
CEILINGS = frozenset({"wrong"})  # a share of wrong indications may not rise above its baseline


def _identity_problems(
    folder: str,
    record: dict[str, Any],
    scored: tuple[list[tuple[str, str]], list[str], list[str]],
    evals: Path,
) -> list[str]:
    """Return how a scores record differs from the current prompts, role settings, and label sets."""
    prompts, roles, sets = scored
    problems: list[str] = []
    if [(p.get("prompt_id"), p.get("prompt_version")) for p in record.get("prompts", [])] != prompts:
        problems.append(f"{folder}/scores.json is not for prompts {prompts}")
    else:
        for entry, ref in zip(record["prompts"], prompts, strict=True):
            if entry.get("prompt_sha256") != prompt_fingerprint(load_prompt(*ref)):
                problems.append(f"{folder}/scores.json is for other words of {ref[0]} {ref[1]}")
    config = load_config()
    models = [
        {
            "model": config.roles[r].model,
            "effort": config.roles[r].effort,
            "max_tokens": config.roles[r].max_tokens,
        }
        for r in roles
    ]
    if record.get("models") != models:
        problems.append(f"{folder}/scores.json is for other models or settings than {roles}")
    wanted = {Path(name).name: gold_fingerprint(evals / name) for name in sets if (evals / name).exists()}
    if len(wanted) != len(sets) or record.get("sets") != wanted:
        problems.append(f"{folder}/scores.json is for other label sets than {sets}")
    return problems


def _floor_problems(folder: str, earned: dict[str, Any], floor: dict[str, float]) -> list[str]:
    """Return each score past its floor (or, for `wrong`, its ceiling), missing, or not a number.

    The scores that need a floor come from the scorer's own fields, not from the record, so a
    record that leaves one out is refused (Opus review of PR 126). NaN passes no comparison, so a
    score must be a finite number.
    """
    problems: list[str] = []
    required = _SCORE_FIELDS[folder]
    unfloored = sorted(required - set(floor))
    if unfloored:
        problems.append(f"{folder}/baseline.json names no floor for {', '.join(unfloored)}")
    for name in sorted(required - set(earned)):
        problems.append(f"{folder}/scores.json has no {name} score")
    for name, limit in floor.items():
        raw = earned.get(name)
        if raw is None:
            continue  # counted above, or not a score this folder records
        if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(raw):
            problems.append(f"{folder}/scores.json gives {name} as {raw!r}, not a number")
            continue
        value = float(raw)
        if name in CEILINGS and value > limit:
            problems.append(f"{name} rose to {value}, above the ceiling {limit}")
        elif name not in CEILINGS and value < limit:
            problems.append(f"{name} fell to {value}, below the floor {limit}")
    return problems


def indication_gate_problems(evals: Path = EVALS_ROOT) -> list[str]:
    """Return why the indication readers may not run on live data; empty when they may.

    Each folder's scores.json must name exactly the current prompts (by fingerprint), the models
    and settings of their roles in config/models.toml, and the current label sets; and every score
    must meet its folder's baseline.json, a floor, or for `wrong` a ceiling.
    """
    problems: list[str] = []
    for folder, scored in _SCORED.items():
        scores_path, baseline_path = evals / folder / "scores.json", evals / folder / "baseline.json"
        record: dict[str, Any] | None = None
        if scores_path.exists():
            loaded: dict[str, Any] = json.loads(scores_path.read_text(encoding="utf-8"))
            problems += _identity_problems(folder, loaded, scored, evals)
            record = loaded
        else:
            problems.append(f"{folder} has no scores.json: run `catalyst eval-extractor --prompt {folder}`")
        if not baseline_path.exists():
            problems.append(f"{folder} has no baseline.json: there is no floor to check")
        elif record is not None:
            floor: dict[str, float] = json.loads(baseline_path.read_text(encoding="utf-8"))
            problems += _floor_problems(folder, record.get("scores", {}), floor)
    return problems
