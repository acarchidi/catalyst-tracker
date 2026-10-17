"""The prompt-evaluation gate: CI blocks an unscored prompt change or a drop in any score (AGENTS.md)."""

import json
from pathlib import Path

from catalyst.extract.agent import EXTRACTOR_PROMPT, ROLE
from catalyst.extract.evaluate import EVALS, gate_problems, gold_fingerprint
from catalyst.llm.config import load_config


def test_the_current_prompt_and_model_may_ship() -> None:
    model = load_config().roles[ROLE].model
    assert gate_problems(EXTRACTOR_PROMPT, model) == []


def _evals(tmp_path: Path, scores: dict[str, object] | None, baseline: dict[str, float] | None) -> Path:
    (tmp_path / "gold.jsonl").write_text('{"sha256": "a"}\n')
    if scores is not None:
        (tmp_path / "scores.json").write_text(json.dumps(scores))
    if baseline is not None:
        (tmp_path / "baseline.json").write_text(json.dumps(baseline))
    return tmp_path


def _record(
    tmp_path: Path, version: str = "1.1.0", model: str = "m", recall: float = 0.9
) -> dict[str, object]:
    return {
        "prompt_id": "extract_catalysts",
        "prompt_version": version,
        "model": model,
        "gold_sha256": gold_fingerprint(tmp_path / "gold.jsonl"),
        "scores": {"recall": recall},
    }


def test_the_first_version_may_ship_unscored(tmp_path: Path) -> None:
    assert gate_problems(("extract_catalysts", "1.0.0"), "m", _evals(tmp_path, None, None)) == []


def test_a_later_version_needs_scores(tmp_path: Path) -> None:
    (problem,) = gate_problems(("extract_catalysts", "1.1.0"), "m", _evals(tmp_path, None, None))
    assert "no scores" in problem


def test_scores_must_match_the_prompt_the_model_and_the_gold_set(tmp_path: Path) -> None:
    evals = _evals(tmp_path, None, None)
    (evals / "scores.json").write_text(json.dumps(_record(tmp_path, version="1.0.0")))
    assert any("prompt_version" in p for p in gate_problems(("extract_catalysts", "1.1.0"), "m", evals))
    assert any("model" in p for p in gate_problems(("extract_catalysts", "1.0.0"), "other", evals))
    (evals / "gold.jsonl").write_text('{"sha256": "b"}\n')
    assert any("gold_sha256" in p for p in gate_problems(("extract_catalysts", "1.0.0"), "m", evals))


def test_a_drop_below_the_baseline_is_blocked(tmp_path: Path) -> None:
    evals = _evals(tmp_path, None, {"recall": 0.95})
    (evals / "scores.json").write_text(json.dumps(_record(tmp_path, recall=0.9)))
    (problem,) = gate_problems(("extract_catalysts", "1.1.0"), "m", evals)
    assert "recall fell to 0.9" in problem
    (evals / "scores.json").write_text(json.dumps(_record(tmp_path, recall=0.95)))
    assert gate_problems(("extract_catalysts", "1.1.0"), "m", evals) == []


def test_the_gate_reads_the_real_evals_folder() -> None:
    assert EVALS.name == "extract_catalysts"
    assert (EVALS / "README.md").exists()
