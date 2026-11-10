"""The weekday schedule of the live jobs (ADR 0051), judged against what a scheduled run gets.

A scheduled GitHub Actions run has no inputs: every `inputs.x` is empty. So each step a
scheduled run must take has to fall back to its default, and no step that reads private data
or costs more than the daily read may run without an explicit choice.
"""

import re
from pathlib import Path
from typing import Any

import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _load(name: str) -> dict[Any, Any]:
    data: dict[Any, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return data


def _steps(workflow: dict[Any, Any], job: str) -> dict[str, str]:
    """Return each named step's `if:` text (empty when it always runs)."""
    steps: list[dict[str, Any]] = workflow["jobs"][job]["steps"]
    return {str(step["name"]): str(step.get("if", "")) for step in steps if "name" in step}


def _runs_on_schedule(condition: str, default_mode: str) -> bool:
    """Evaluate a step condition the way a scheduled run would: no inputs, nothing cancelled."""
    if not condition:
        return True
    expression = condition.removeprefix("${{").removesuffix("}}").strip()
    expression = expression.replace(f"(inputs.mode || '{default_mode}')", f"'{default_mode}'")
    expression = expression.replace("inputs.mode", "''").replace("!cancelled()", "True")
    expression = expression.replace("||", " or ").replace("&&", " and ").replace("!=", " != ")
    assert re.fullmatch(r"[\s'a-z_=!()A-Z]+", expression), expression
    return bool(eval(expression))  # noqa: S307 - a fixed expression built from the repository's own file


def test_both_live_jobs_run_on_weekdays() -> None:
    collect = _load("collect-live.yml")[True]["schedule"][0]["cron"]
    extract = _load("extract-live.yml")[True]["schedule"][0]["cron"]
    assert collect.endswith("* * 1-5")  # Monday to Friday evenings
    assert extract.endswith("* * 2-6")  # the following night, Tuesday to Saturday, in UTC


def test_a_scheduled_collection_collects_sec_filings_for_every_company() -> None:
    workflow = _load("collect-live.yml")
    steps = _steps(workflow, "collect")
    assert "Measure or compress the stored copies" in steps
    ran = {name for name, cond in steps.items() if _runs_on_schedule(cond, "collect")}
    assert {"Bring the live database up to date", "Collect", "Build alerts from this run"} <= ran
    assert "Measure or compress the stored copies" not in ran
    body = (WORKFLOWS / "collect-live.yml").read_text(encoding="utf-8")
    assert "COMPANIES: ${{ inputs.companies || 'coverage' }}" in body
    assert "SOURCES: ${{ inputs.sources || 'sec' }}" in body
    assert "SINCE=$(date -u -d '10 days ago' +%F)" in body


def test_a_scheduled_extraction_reads_at_most_20_releases_and_nothing_private() -> None:
    workflow = _load("extract-live.yml")
    steps = _steps(workflow, "extract")
    # The steps checked below exist, so "not in ran" cannot pass because one was renamed.
    assert {"Export published and held catalysts", "Score a reader on the gold set"} <= set(steps)
    ran = {name for name, cond in steps.items() if _runs_on_schedule(cond, "extract")}
    assert "Extract catalysts from the newest press releases" in ran
    assert "Build alerts from this run" in ran
    # The export and the gold-set scoring never run without an explicit choice.
    assert "Export published and held catalysts" not in ran
    assert "Score a reader on the gold set" not in ran
    body = (WORKFLOWS / "extract-live.yml").read_text(encoding="utf-8")
    assert "LIMIT: ${{ inputs.limit || 20 }}" in body


def test_the_live_jobs_never_overlap() -> None:
    for name in ("collect-live.yml", "extract-live.yml"):
        concurrency = _load(name)["concurrency"]
        assert concurrency == {"group": "live-database", "cancel-in-progress": False}
