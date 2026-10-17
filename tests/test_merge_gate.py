"""The merge robot merges only eligible pull requests whose required checks all succeeded."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "merge_gate.py"
SHA = "a" * 40
REQUIRED = ["Lint, types, tests, migrations", "Dependency audit", "Secret scan", "Claude review"]


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("merge_gate", SCRIPT)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load()


def pr(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "state": "OPEN",
        "isDraft": False,
        "baseRefName": "main",
        "headRefName": "claude/snapshot-store",
        "headRefOid": SHA,
        "isCrossRepository": False,
        "labels": [],
    }
    return base | changes


def run(name: str, conclusion: str | None = "success", run_id: int = 1, **changes: Any) -> dict[str, Any]:
    status = "completed" if conclusion else "in_progress"
    check = {"name": name, "status": status, "conclusion": conclusion, "id": run_id, "app": "github-actions"}
    return check | changes


def green() -> list[dict[str, Any]]:
    return [run(name) for name in REQUIRED]


def action(pull: dict[str, Any], checks: list[dict[str, Any]], files: list[str] | None = None) -> str:
    return gate.decide(SHA, pull, files or ["pipeline/catalyst/snapshots.py"], checks)["action"]


def test_all_green_merges() -> None:
    assert action(pr(), green()) == "merge"


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "CLOSED"},
        {"isDraft": True},
        {"baseRefName": "claude/other"},
        {"headRefName": "feature/x"},
        {"headRefName": "dependabot/uv/x"},
        {"isCrossRepository": True},
        {"headRefOid": "b" * 40},
        {"labels": [{"name": "hold"}]},
    ],
)
def test_ineligible_pull_requests_are_skipped(changes: dict[str, Any]) -> None:
    assert action(pr(**changes), green()) == "skip"


@pytest.mark.parametrize("missing", REQUIRED)
def test_a_missing_required_check_waits(missing: str) -> None:
    assert action(pr(), [c for c in green() if c["name"] != missing]) == "wait"


def test_a_running_check_waits() -> None:
    checks = [*green()[:3], run("Claude review", None)]
    assert action(pr(), checks) == "wait"


@pytest.mark.parametrize("conclusion", ["failure", "skipped", "cancelled", "neutral", "timed_out"])
def test_anything_but_success_is_not_merged(conclusion: str) -> None:
    checks = [*green()[:3], run("Claude review", conclusion)]
    assert action(pr(), checks) == "skip"


def test_the_latest_run_of_a_check_counts() -> None:
    first, second = run("Claude review", "success", 1), run("Claude review", "failure", 2)
    passed_then_failed = [*green()[:3], first, second]
    first, second = run("Claude review", "failure", 1), run("Claude review", "success", 2)
    failed_then_passed = [*green()[:3], first, second]
    assert action(pr(), passed_then_failed) == "skip"
    assert action(pr(), failed_then_passed) == "merge"


def test_a_check_with_the_same_name_from_another_app_does_not_count() -> None:
    checks = [*green()[:3], run("Claude review", app="someone-elses-app")]
    assert action(pr(), checks) == "wait"


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/merge-robot.yml",
        ".github/workflows/claude-review.yml",
        ".github/rulesets/main.json",
        "scripts/merge_gate.py",
        "scripts/review_verdict.py",
        "scripts/overseer_verdict.py",  # the build overseer's actor (ADR 0019 Part B)
        "AGENTS.md",
        "CLAUDE.md",
        "pipeline/catalyst/CLAUDE.md",
        "web/AGENTS.md",
        ".claude/settings.json",
        ".mcp.json",
        "docs/spec.md",
        "docs/quality-gates.md",
        ".gitleaks.toml",
        ".pre-commit-config.yaml",
        "config/models.toml",
        "evals/extract_catalysts/baseline.json",
        "evals/extract_catalysts/unscored.json",
        "evals/verify_catalysts/baseline.json",
        "evals/verify_catalysts/unscored.json",
    ],
)
def test_changes_to_the_rules_review_or_protected_settings_go_to_the_owner(path: str) -> None:
    decision = gate.decide(SHA, pr(), ["README.md", path], green())
    assert decision["action"] == "owner"
    assert path in decision["reason"]


def test_owner_paths_still_wait_for_checks_first() -> None:
    assert action(pr(), green()[:3], [".github/workflows/ci.yml"]) == "wait"


def test_command_line_prints_the_decision(tmp_path: Path) -> None:
    paths: list[str] = []
    inputs: list[tuple[str, object]] = [("pr", pr()), ("files", ["README.md"]), ("checks", green())]
    for name, value in inputs:
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        paths.append(str(path))
    done = subprocess.run(  # noqa: S603 - fixed interpreter and script path
        [sys.executable, "-I", str(SCRIPT), SHA, *paths], capture_output=True, check=True, text=True
    )
    assert json.loads(done.stdout) == {"action": "merge", "reason": "every required check passed"}


@pytest.mark.parametrize(
    "path",
    [
        "docs/roadmap.md",
        "docs/adr/0008-x.md",
        "pyproject.toml",
        "pipeline/catalyst/cli.py",
        "evals/extract_catalysts/scores.json",
        "evals/verify_catalysts/scores.json",  # the owner-only floor protects it (ADR 0018, part 2)
    ],
)
def test_ordinary_paths_merge(path: str) -> None:
    assert action(pr(), green(), [path]) == "merge"
