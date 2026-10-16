"""Turn the Claude review's structured output into a pass or fail.

Used by `.github/workflows/claude-review.yml`. Reads the reviewer's JSON from the
`REVIEW_RESULT` environment variable, writes a plain-English report to the path given
as the only argument, and exits 0 only when the reviewer said "pass" and listed no
blocking findings. Anything missing, malformed, or contradictory fails, so a review
that did not finish can never let a pull request through.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

HEADER = "## Claude review"


def _findings(result: dict[str, Any], key: str) -> list[dict[str, Any]] | None:
    """Return the findings list under `key`, or None when it is not a list of objects."""
    value = result.get(key)
    if not isinstance(value, list):
        return None
    items: list[Any] = value  # pyright: ignore[reportUnknownVariableType]
    if not all(isinstance(item, dict) for item in items):
        return None
    return items


def _line(finding: dict[str, Any]) -> str:
    """Format one finding as a bullet with its file and line."""
    where = str(finding.get("file", "?"))
    if isinstance(finding.get("line"), int):
        where = f"{where}:{finding['line']}"
    return f"- `{where}`: {finding.get('problem', '(no description)')}"


def judge(raw: str, *, key_present: bool = True) -> tuple[bool, str]:
    """Return whether the review passed, and the report to post (always newline-terminated)."""
    if not key_present:
        return False, (
            f"{HEADER}: not set up\n\nThe reviewer key `CATALYST_REVIEW_API_KEY` is not in the repository's "
            "secrets yet, so no review ran. See docs/setup.md Part 6, step C.\n"
        )
    try:
        result: Any = json.loads(raw)
    except json.JSONDecodeError:
        result = None
    if not isinstance(result, dict):
        return (
            False,
            f"{HEADER}: did not finish\n\nThe reviewer returned no usable result. Re-run the check.\n",
        )
    data: dict[str, Any] = result  # pyright: ignore[reportUnknownVariableType]
    blocking = _findings(data, "blocking")
    optional = _findings(data, "optional")
    verdict = data.get("verdict")
    summary = data.get("summary")
    if (
        blocking is None
        or optional is None
        or verdict not in ("pass", "fail")
        or not isinstance(summary, str)
    ):
        return False, f"{HEADER}: did not finish\n\nThe reviewer's result was incomplete. Re-run the check.\n"
    passed = verdict == "pass" and not blocking
    title = "passed" if passed else "changes needed"
    parts = [f"{HEADER}: {title}", "", summary.strip()]
    if blocking:
        parts += ["", "**Must fix before merging**", *map(_line, blocking)]
    if optional:
        parts += ["", "**Optional**", *map(_line, optional)]
    if verdict == "pass" and blocking:
        parts += ["", "The reviewer said pass but listed blocking findings, so this counts as a fail."]
    return passed, "\n".join(parts) + "\n"


def main() -> int:
    """Write the report and return the exit code."""
    if len(sys.argv) != 2:
        print("usage: review_verdict.py REPORT_PATH", file=sys.stderr)
        return 2
    key_present = os.environ.get("REVIEW_KEY_PRESENT", "true") == "true"
    passed, report = judge(os.environ.get("REVIEW_RESULT", ""), key_present=key_present)
    Path(sys.argv[1]).write_text(report, encoding="utf-8")
    print(report)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
