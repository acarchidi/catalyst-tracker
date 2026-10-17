"""Alerts: plain code turns job run summaries and spend into issues for the owner (ADR 0023).

No model is called. An alert's words come only from counts and names chosen here, never from
an error message in a run, so neither a secret nor words from a source document reach an
issue; the alert points to the run log instead. The output is a plan that `catalyst.issues`
posts: issues to raise, and alerts a clean run has solved.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine

from catalyst.llm.config import ModelConfig
from catalyst.llm.ledger import DatabaseLedger

DEFAULT_THRESHOLDS_PATH = Path(__file__).resolve().parents[2] / "config" / "orchestrator.toml"
OUTCOMES = frozenset({"success", "failure", "cancelled", "skipped"})  # a GitHub Actions step's outcome
JOB_NAMES = {
    "extract-catalysts": "Catalyst extraction",
    "collect": "Collection",
    "snapshots-verify": "Stored-copy check",
    "check-links": "Link check",
    "sample-records": "Ten-record sample",
    "migrate": "Database update",
    "spend-check": "Spending check",
}
Plan = dict[str, Any]


class _AlertLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    failed_runs_in_a_row: int = Field(ge=1)
    spend_share_of_cap: Decimal = Field(gt=0, le=1)


class _OrchestratorFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    alerts: _AlertLimits


@dataclass(frozen=True)
class Thresholds:
    """The alert thresholds from config/orchestrator.toml."""

    failed_runs_in_a_row: int
    spend_share_of_cap: Decimal


def load_thresholds(path: Path = DEFAULT_THRESHOLDS_PATH) -> Thresholds:
    """Read and validate the owner's threshold file."""
    with path.open("rb") as handle:
        limits = _OrchestratorFile.model_validate(tomllib.load(handle)).alerts
    return Thresholds(limits.failed_runs_in_a_row, limits.spend_share_of_cap)


@dataclass(frozen=True)
class JobRun:
    """One job (a workflow step) in a run: its name, its step outcome, and the JSON it printed."""

    name: str
    outcome: str
    summary: Any = None
    unreadable: bool = False


def parse_run(spec: str) -> JobRun:
    """Read `NAME:OUTCOME[:FILE]`, where FILE holds the JSON summary the job printed."""
    name, _, rest = spec.partition(":")
    outcome, _, path = rest.partition(":")
    if outcome not in OUTCOMES:
        raise ValueError(f"{spec!r}: the outcome must be one of {', '.join(sorted(OUTCOMES))}")
    if not path:
        return JobRun(name, outcome)
    try:
        return JobRun(name, outcome, json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return JobRun(name, outcome, unreadable=True)


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else plural or word + 's'}"


def _mapping(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _sequence(value: object) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


def _count(summary: Mapping[str, Any], key: str) -> int:
    value = summary.get(key)
    return value if isinstance(value, int) else 0


def _extract_problems(summary: dict[str, Any]) -> list[str]:
    second = _mapping(summary.get("second_reading"))
    found = [
        (_count(summary, "documents_failed"), "{} could not be read."),
        (bool(summary.get("stopped_early")), "It stopped early, before reading every document it was given."),
        ("error" in _mapping(summary.get("matches")), "Matching names to records did not finish."),
        ("error" in _mapping(summary.get("checks")), "The checks did not finish."),
        ("error" in second, "The second reading did not finish."),
        (_count(second, "documents_failed"), "The second reading could not read {}."),
        (bool(second.get("stopped_early")), "The second reading stopped early."),
    ]
    return [text.format(_plural(int(n), "document")) for n, text in found if n]


def _collect_problems(summary: list[Any]) -> list[str]:
    sources = [str(_mapping(entry).get("source")) for entry in summary if _mapping(entry).get("error")]
    return [f"Collection from {', '.join(sources)} had errors."] if sources else []


def _copy_problems(summary: dict[str, Any]) -> list[str]:
    words = {
        "missing": ("is missing", "are missing"),
        "changed": ("changed",) * 2,
        "unreadable": ("could not be read",) * 2,
    }
    found: list[str] = []
    for key, (one, many) in words.items():
        if n := _count(summary, key):
            found.append(f"{_plural(n, 'stored copy', 'stored copies')} {one if n == 1 else many}.")
    return found


def _problems(run: JobRun) -> list[str]:
    problems = ["Its report could not be read."] if run.unreadable else []
    if run.name == "extract-catalysts":
        problems += _extract_problems(_mapping(run.summary))
    elif run.name == "collect":
        problems += _collect_problems(_sequence(run.summary))
    elif run.name == "snapshots-verify":
        problems += _copy_problems(_mapping(run.summary))
    if run.outcome == "failure" and not problems:
        problems.append("It ended with an error before it could say what went wrong.")
    return problems


def _job_alert(run: JobRun, problems: list[str], limits: Thresholds, run_url: str) -> dict[str, Any]:
    job = JOB_NAMES.get(run.name, run.name)
    body = [
        f"**{job} did not finish cleanly.**",
        "",
        "What went wrong:",
        *(f"- {problem}" for problem in problems),
        "",
        f"What to do: ask the coding session to look at the run log: {run_url}",
        "",
        "This issue closes by itself after the next clean run.",
    ]
    key = f"job-failed:{run.name}"
    return {
        "key": key,
        "title": f"{job} needs a look",
        "body": "\n".join(body),
        "escalate_at": limits.failed_runs_in_a_row,
    }


def _spend_alert(role: str, spent: Decimal, cap: Decimal) -> dict[str, Any]:
    body = [
        f"The {role} role has spent ${spent:.2f} of its ${cap:.2f} daily cap today (New York time).",
        "",
        "It stops by itself at the cap and starts again after midnight, New York time. "
        "It never switches to a pricier model.",
        "",
        "What to do: nothing if this is rare. If it happens most days, the cap may be too low for "
        "the work. Raising it is your decision (config/models.toml).",
    ]
    title = f"The {role} role reached its daily spending cap"
    return {"key": f"spend-cap:{role}", "title": title, "body": "\n".join(body), "escalate_at": None}


def build_alerts(
    runs: Iterable[JobRun],
    spend: Mapping[str, tuple[Decimal, Decimal]] | None,
    limits: Thresholds,
    run_url: str,
) -> Plan:
    """Return the alerts this run raises, and the alerts it shows are solved.

    A skipped or cancelled step says nothing either way. `spend` maps each role to today's spend
    and its daily cap; None when this run did not check spending.
    """
    issues: list[dict[str, Any]] = []
    solved: list[dict[str, str]] = []
    clean = f"Solved: the run at {run_url} was clean, so this alert is closed."
    for run in runs:
        if run.outcome in {"cancelled", "skipped"}:
            continue
        if problems := _problems(run):
            issues.append(_job_alert(run, problems, limits, run_url))
        else:
            solved.append({"key": f"job-failed:{run.name}", "comment": clean})
    for role, (spent, cap) in sorted((spend or {}).items()):
        if spent >= cap * limits.spend_share_of_cap:
            issues.append(_spend_alert(role, spent, cap))
        else:
            comment = f"Solved: the {role} role is back under its daily cap (${spent:.2f} of ${cap:.2f})."
            solved.append({"key": f"spend-cap:{role}", "comment": comment})
    return {"mode": "alert", "label": "alert", "issues": issues, "solved": solved}


def spend_today(engine: Engine, config: ModelConfig, day: date) -> dict[str, tuple[Decimal, Decimal]]:
    """Return each role's spend on `day` (New York time) from `model_call`, with its daily cap."""
    ledger = DatabaseLedger(engine)
    return {name: (ledger.spent(name, day), role.daily_cap_usd) for name, role in config.roles.items()}
