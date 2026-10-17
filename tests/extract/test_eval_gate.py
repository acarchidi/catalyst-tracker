"""The prompt-evaluation gate: CI blocks an unscored prompt or model change and any drop in a score."""

import json
from decimal import Decimal
from pathlib import Path

from catalyst.extract.agent import EXTRACTOR_PROMPT, ROLE
from catalyst.extract.evaluate import EVALS, gate_problems, gold_fingerprint, prompt_fingerprint
from catalyst.llm.config import RoleConfig, load_config
from catalyst.llm.prompts import Prompt, load_prompt

REAL = load_prompt(*EXTRACTOR_PROMPT)
ROLE_M = RoleConfig(
    provider="anthropic", model="m", max_tokens=1000, effort="medium", daily_cap_usd=Decimal(1)
)
ALL_SCORES = {
    "recall": 0.5,
    "precision": 0.5,
    "catalyst_type": 0.5,
    "window": 0.5,
    "drug": 0.5,
    "quote_found": 0.5,
}


def _role(**changes: object) -> RoleConfig:
    return ROLE_M.model_copy(update=changes)


def _prompt(version: str = "1.0.0", system: str | None = None) -> Prompt:
    return REAL.model_copy(update={"version": version, "system": system or REAL.system})


def _identity(prompt: Prompt, role: RoleConfig = ROLE_M) -> dict[str, object]:
    return {
        "prompt_id": prompt.id,
        "prompt_version": prompt.version,
        "prompt_sha256": prompt_fingerprint(prompt),
        "model": role.model,
        "effort": role.effort,
        "max_tokens": role.max_tokens,
    }


def _evals(tmp_path: Path, **files: object) -> Path:
    (tmp_path / "gold.jsonl").write_text('{"sha256": "a"}\n')
    for name, content in files.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(content))
    return tmp_path


def _scores(tmp_path: Path, prompt: Prompt, recall: float = 0.9) -> dict[str, object]:
    (tmp_path / "gold.jsonl").write_text('{"sha256": "a"}\n')
    return _identity(prompt) | {
        "gold_sha256": gold_fingerprint(tmp_path / "gold.jsonl"),
        "scores": ALL_SCORES | {"recall": recall},
    }


def test_the_current_prompt_and_model_may_ship() -> None:
    assert gate_problems(REAL, load_config().roles[ROLE]) == []


def test_the_first_version_may_ship_unscored_only_as_recorded(tmp_path: Path) -> None:
    evals = _evals(tmp_path, unscored=_identity(_prompt()))
    assert gate_problems(_prompt(), ROLE_M, evals) == []


def test_another_model_needs_scores_even_for_the_first_version(tmp_path: Path) -> None:
    """Review of PR #17: a model change must fail CI before any scores exist."""
    evals = _evals(tmp_path, unscored=_identity(_prompt()))
    (problem,) = gate_problems(_prompt(), _role(model="another-model"), evals)
    assert "model" in problem


def test_edited_prompt_words_are_caught_without_a_new_version(tmp_path: Path) -> None:
    """Review of PR #17: the gate checks the words the model reads, not only the version number."""
    evals = _evals(tmp_path, unscored=_identity(_prompt()))
    (problem,) = gate_problems(_prompt(system="Find every event."), ROLE_M, evals)
    assert "prompt_sha256" in problem
    scored = _evals(tmp_path, scores=_scores(tmp_path, _prompt("1.1.0")), baseline=ALL_SCORES)
    (problem,) = gate_problems(_prompt("1.1.0", system="Find every event."), ROLE_M, scored)
    assert "prompt_sha256" in problem


def test_a_later_version_needs_scores(tmp_path: Path) -> None:
    (problem,) = gate_problems(_prompt("1.1.0"), ROLE_M, _evals(tmp_path))
    assert "no scores" in problem


def test_with_neither_scores_nor_a_record_the_gate_fails(tmp_path: Path) -> None:
    (problem,) = gate_problems(_prompt(), ROLE_M, _evals(tmp_path))
    assert "never been checked" in problem


def test_scores_must_match_the_prompt_the_model_and_the_gold_set(tmp_path: Path) -> None:
    evals = _evals(tmp_path, scores=_scores(tmp_path, _prompt("1.1.0")), baseline=ALL_SCORES)
    assert gate_problems(_prompt("1.1.0"), ROLE_M, evals) == []
    assert any("prompt_version" in p for p in gate_problems(_prompt("1.2.0"), ROLE_M, evals))
    assert any("model" in p for p in gate_problems(_prompt("1.1.0"), _role(model="other"), evals))
    (evals / "gold.jsonl").write_text('{"sha256": "b"}\n')
    assert any("gold_sha256" in p for p in gate_problems(_prompt("1.1.0"), ROLE_M, evals))


def test_a_drop_below_the_baseline_is_blocked(tmp_path: Path) -> None:
    evals = _evals(
        tmp_path, scores=_scores(tmp_path, _prompt("1.1.0")), baseline=ALL_SCORES | {"recall": 0.95}
    )
    (problem,) = gate_problems(_prompt("1.1.0"), ROLE_M, evals)
    assert "recall fell to 0.9" in problem


def test_scores_without_a_baseline_are_blocked(tmp_path: Path) -> None:
    evals = _evals(tmp_path, scores=_scores(tmp_path, _prompt("1.1.0")))
    (problem,) = gate_problems(_prompt("1.1.0"), ROLE_M, evals)
    assert "baseline.json does not" in problem


def test_the_gate_reads_the_real_evals_folder() -> None:
    assert EVALS.name == "extract_catalysts"
    assert (EVALS / "unscored.json").exists()


def test_other_effort_or_token_limit_needs_new_scores(tmp_path: Path) -> None:
    """Review of PR #17: settings that change answers count as a model change."""
    evals = _evals(tmp_path, unscored=_identity(_prompt()))
    assert any("effort" in p for p in gate_problems(_prompt(), _role(effort="high"), evals))
    assert any("max_tokens" in p for p in gate_problems(_prompt(), _role(max_tokens=2000), evals))


def test_a_baseline_must_name_every_score(tmp_path: Path) -> None:
    """Review of PR #17: a partial baseline would let the missing score drop unseen."""
    partial = {k: v for k, v in ALL_SCORES.items() if k != "drug"}
    evals = _evals(tmp_path, scores=_scores(tmp_path, _prompt("1.1.0")), baseline=partial)
    (problem,) = gate_problems(_prompt("1.1.0"), ROLE_M, evals)
    assert "drug" in problem
