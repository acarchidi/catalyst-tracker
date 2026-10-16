"""The review gate fails closed: only an explicit, consistent pass lets a pull request through."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "review_verdict.py"


def run(result: str | None, tmp_path: Path) -> tuple[int, str]:
    report = tmp_path / "report.md"
    env = {k: v for k, v in os.environ.items() if k != "REVIEW_RESULT"}
    if result is not None:
        env["REVIEW_RESULT"] = result
    done = subprocess.run(  # noqa: S603 - fixed interpreter and script path
        [sys.executable, str(SCRIPT), str(report)], env=env, capture_output=True, check=False
    )
    return done.returncode, report.read_text(encoding="utf-8")


def finding(problem: str) -> dict[str, object]:
    return {"file": "pipeline/catalyst/x.py", "line": 3, "problem": problem}


def result(verdict: str, blocking: list[dict[str, object]], optional: list[dict[str, object]]) -> str:
    return json.dumps(
        {"verdict": verdict, "summary": "Looked at it.", "blocking": blocking, "optional": optional}
    )


def test_clean_pass_passes(tmp_path: Path) -> None:
    code, report = run(result("pass", [], [finding("a nit")]), tmp_path)
    assert code == 0
    assert report.startswith("## Claude review: passed")
    assert "`pipeline/catalyst/x.py:3`: a nit" in report


def test_blocking_finding_fails(tmp_path: Path) -> None:
    code, report = run(result("fail", [finding("secret in log")], []), tmp_path)
    assert code == 1
    assert "**Must fix before merging**" in report
    assert "secret in log" in report


def test_pass_with_blocking_findings_fails(tmp_path: Path) -> None:
    code, report = run(result("pass", [finding("weakened check")], []), tmp_path)
    assert code == 1
    assert "counts as a fail" in report


def test_fail_verdict_without_findings_fails(tmp_path: Path) -> None:
    code, _ = run(result("fail", [], []), tmp_path)
    assert code == 1


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not json",
        "[]",
        json.dumps({"verdict": "pass", "summary": "ok", "blocking": []}),
        json.dumps({"verdict": "PASS", "summary": "ok", "blocking": [], "optional": []}),
        json.dumps({"verdict": "pass", "summary": 1, "blocking": [], "optional": []}),
        json.dumps({"verdict": "pass", "summary": "ok", "blocking": "none", "optional": []}),
        json.dumps({"verdict": "pass", "summary": "ok", "blocking": ["x"], "optional": []}),
    ],
)
def test_missing_or_malformed_result_fails(raw: str | None, tmp_path: Path) -> None:
    code, report = run(raw, tmp_path)
    assert code == 1
    assert "did not finish" in report
