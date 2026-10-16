"""Decide whether the merge robot may merge a pull request.

Used by `.github/workflows/merge-robot.yml`, which always runs this file from main, so a
pull request cannot change the rules that decide its own merge (ADR 0007). Prints one
JSON object, `{"action": ..., "reason": ...}`, where action is:

- "merge": every rule below holds;
- "owner": everything passed, but the pull request changes the robot or the review
  check, so the owner merges it by hand;
- "wait": a required check has not finished on this commit yet;
- "skip": the pull request is not eligible, or a required check did not succeed.

Usage: merge_gate.py SHA PR_JSON FILES_JSON CHECKS_JSON
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REQUIRED_CHECKS = ("Lint, types, tests, migrations", "Dependency audit", "Secret scan", "Claude review")
CHECK_APP = "github-actions"
HOLD_LABEL = "hold"
# Paths that change who or what decides a merge. The robot never merges these.
SELF_PATHS = (".github/workflows/", ".github/rulesets/", "scripts/merge_gate.py", "scripts/review_verdict.py")


def _decision(action: str, reason: str) -> dict[str, str]:
    return {"action": action, "reason": reason}


def _ineligible(sha: str, pr: dict[str, Any]) -> str | None:
    """Return why the pull request is not one the robot handles, or None."""
    raw_labels: list[Any] = pr.get("labels") or []
    labels = {str(label.get("name", "")) for label in raw_labels if isinstance(label, dict)}  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportUnknownVariableType]
    rules = [
        (pr.get("state") == "OPEN", "the pull request is not open"),
        (pr.get("isDraft") is False, "the pull request is a draft"),
        (pr.get("baseRefName") == "main", "the pull request does not target main"),
        (str(pr.get("headRefName", "")).startswith("claude/"), "the branch is not a claude/ branch"),
        (pr.get("isCrossRepository") is False, "the pull request comes from another repository"),
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


def decide(sha: str, pr: dict[str, Any], files: list[str], checks: list[dict[str, Any]]) -> dict[str, str]:
    """Return the robot's decision for one pull request at one commit."""
    reason = _ineligible(sha, pr)
    if reason:
        return _decision("skip", reason)
    problem = _check_problem(checks)
    if problem:
        return _decision(*problem)
    own = sorted(f for f in files if f.startswith(SELF_PATHS) or f in SELF_PATHS)
    if own:
        return _decision("owner", "it changes the merge robot or the review check: " + ", ".join(own))
    return _decision("merge", "every required check passed")


def main() -> int:
    """Read the four inputs, print the decision as JSON."""
    if len(sys.argv) != 5:
        print("usage: merge_gate.py SHA PR_JSON FILES_JSON CHECKS_JSON", file=sys.stderr)
        return 2
    sha = sys.argv[1]
    pr, files, checks = (json.loads(Path(p).read_text(encoding="utf-8")) for p in sys.argv[2:])
    print(json.dumps(decide(sha, pr, files, checks)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
