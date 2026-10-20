"""Decide whether the merge robot may merge a pull request.

Used by `.github/workflows/merge-robot.yml`, which always runs this file from main, so a
pull request cannot change the rules that decide its own merge (ADR 0007). Prints one
JSON object, `{"action": ..., "reason": ...}`, where action is:

- "merge": every rule below holds;
- "owner": everything passed, but the pull request changes a path in OWNER_PATHS or
  an instruction file in OWNER_FILE_NAMES, so the robot leaves it out and the coding agent
  merges it after a separate review (ADR 0036);
- "wait": a required check has not finished on this commit yet;
- "skip": the pull request is not eligible, or a required check did not succeed.

Usage: merge_gate.py SHA PR_JSON FILES_JSON CHECKS_JSON

The repository this runs in comes from GITHUB_REPOSITORY (set by GitHub Actions); without it,
nothing merges. The repository is public (2026-10-20), so anyone can open a pull request from a
fork and name its branch claude/. The robot merges only pull requests whose branch is in this
repository and that the repository's owner opened.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

REQUIRED_CHECKS = ("Lint, types, tests, migrations", "Dependency audit", "Secret scan", "Claude review")
CHECK_APP = "github-actions"
HOLD_LABEL = "hold"
# Paths that change who or what decides a merge: the robot and the review, the rules the
# reviewer judges by (it reads them from the pull request), settings that make a required
# check weaker, the spending caps, and the score floors. The robot never merges these;
# the coding agent does, after a separate review (ADR 0036).
OWNER_PATHS = (
    ".github/workflows/",
    ".github/rulesets/",
    ".claude/",
    "scripts/merge_gate.py",
    "scripts/review_verdict.py",
    "scripts/overseer_verdict.py",  # the build overseer's actor: what it may do (ADR 0019 Part B)
    "config/overseer.toml",  # the overseer's daily run limit, which spends on the review key
    "docs/spec.md",
    "docs/quality-gates.md",
    ".gitleaks.toml",
    ".pre-commit-config.yaml",
    "config/models.toml",
    "config/orchestrator.toml",  # the alert and oversight thresholds (ADR 0019, ADR 0023)
    "evals/extract_catalysts/baseline.json",  # the floor CI holds every prompt to (ADR 0014)
    "evals/extract_catalysts/unscored.json",  # the retired exemption from that floor
    "evals/verify_catalysts/baseline.json",  # the second reading's floor (ADR 0018, part 2)
    "evals/verify_catalysts/unscored.json",  # an exemption the second reading must never get
    "evals/planted_errors/baseline.json",  # the planted-error test's floor (ADR 0018, part 4)
)
# Instruction files the reviewer loads, wherever they sit in the repository.
OWNER_FILE_NAMES = ("AGENTS.md", "CLAUDE.md", ".mcp.json")


def _decision(action: str, reason: str) -> dict[str, str]:
    return {"action": action, "reason": reason}


def _field(pr: dict[str, Any], key: str, inner: str) -> str:
    """Return pr[key][inner] as text, or "" when it is missing or not an object."""
    value: Any = pr.get(key)
    if not isinstance(value, dict):
        return ""
    found: Any = value.get(inner)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return found if isinstance(found, str) else ""


def _ineligible(sha: str, pr: dict[str, Any], repo: str) -> str | None:
    """Return why the pull request is not one the robot handles, or None."""
    raw_labels: list[Any] = pr.get("labels") or []
    labels = {str(label.get("name", "")) for label in raw_labels if isinstance(label, dict)}  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportUnknownVariableType]
    owner, _, name = repo.partition("/")
    head_repo = f"{_field(pr, 'headRepositoryOwner', 'login')}/{_field(pr, 'headRepository', 'name')}"
    rules = [
        (bool(owner and name) and "/" not in name, "the robot does not know which repository it runs in"),
        (pr.get("state") == "OPEN", "the pull request is not open"),
        (pr.get("isDraft") is False, "the pull request is a draft"),
        (pr.get("baseRefName") == "main", "the pull request does not target main"),
        (str(pr.get("headRefName", "")).startswith("claude/"), "the branch is not a claude/ branch"),
        (pr.get("isCrossRepository") is False, "the pull request comes from another repository"),
        (head_repo == repo, "the pull request comes from another repository"),
        (_field(pr, "author", "login") == owner, "the pull request was not opened by the repository owner"),
        (pr.get("headRefOid") == sha, "the pull request has newer commits than the checked one"),
        (HOLD_LABEL not in labels, f'the pull request has the "{HOLD_LABEL}" label'),
    ]
    return next((reason for ok, reason in rules if not ok), None)


def _check_problem(checks: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Return (action, reason) for the first required check not passed, or None."""
    for name in REQUIRED_CHECKS:
        runs = [c for c in checks if c.get("name") == name and c.get("app") == CHECK_APP]
        if not runs:
            return "wait", f'"{name}" has not started on this commit'
        latest = max(runs, key=lambda c: int(c.get("id", 0)))
        if latest.get("status") != "completed":
            return "wait", f'"{name}" is still running'
        if latest.get("conclusion") != "success":
            return "skip", f'"{name}" did not pass ({latest.get("conclusion")})'
    return None


def decide(
    sha: str, pr: dict[str, Any], files: list[str], checks: list[dict[str, Any]], *, repo: str
) -> dict[str, str]:
    """Return the robot's decision for one pull request at one commit, in repository `repo`."""
    reason = _ineligible(sha, pr, repo)
    if reason:
        return _decision("skip", reason)
    problem = _check_problem(checks)
    if problem:
        return _decision(*problem)
    own = sorted(f for f in files if f.startswith(OWNER_PATHS) or Path(f).name in OWNER_FILE_NAMES)
    if own:
        return _decision(
            "owner", "it changes the merge rules, the review, or a protected setting: " + ", ".join(own)
        )
    return _decision("merge", "every required check passed")


def main() -> int:
    """Read the four inputs, print the decision as JSON."""
    if len(sys.argv) != 5:
        print("usage: merge_gate.py SHA PR_JSON FILES_JSON CHECKS_JSON", file=sys.stderr)
        return 2
    sha = sys.argv[1]
    pr, files, checks = (json.loads(Path(p).read_text(encoding="utf-8")) for p in sys.argv[2:])
    print(json.dumps(decide(sha, pr, files, checks, repo=os.environ.get("GITHUB_REPOSITORY", ""))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
