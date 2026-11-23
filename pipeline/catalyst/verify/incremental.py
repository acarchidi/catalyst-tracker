"""Incremental checks: each stored outcome says which code found it and from what (ADR 0064).

A check row records `rule_version`, a hash of the source of the check's module and of every
module of ours it imports, directly or not, so any change to that code is a new version without
anyone remembering to bump one. The checks that read a claim's stored copy also record
`input_digest`, a hash of everything else they read for the claim (the copy's hash, the quote and
its place, the company and drug records they compare). Such a check skips a claim only while
both are unchanged; anything new or changed, held or published, is checked again. An outcome
that came from a copy that could not be read keeps no digest, so it is tried again next run.

Checks that read no copy are computed in full every run, since they depend on today's date and
on other claims; only rows whose outcome, reason, or version changed are written, a thousand to
a statement.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from sqlalchemy import case, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalyst.db.tables import TABLES
from catalyst.schemas import ClaimCheck
from catalyst.verify.rules import CheckResult

BATCH = 1000  # rows per statement, well inside Postgres's limit on parameters
_CHECK = TABLES[ClaimCheck]
_PACKAGE = "catalyst"
# The module whose code decides each check. A check not listed is a rule check or the
# independent-source check, both decided by `catalyst.verify.run` and the modules it imports.
CHECK_MODULES: dict[str, str] = {
    "company_named_in_source": "catalyst.verify.company",
    "drug_named_near_quote": "catalyst.verify.drug",
    "type_named_near_quote": "catalyst.verify.event_type",
    "advisory_meeting_noticed": "catalyst.verify.advisory",
    "second_reading_agrees": "catalyst.verify.second_run",
    "safe_to_write": "catalyst.publish.sanity",
}
DEFAULT_MODULE = "catalyst.verify.run"


def _source(module: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):  # a name imported from a module, not a module itself
        return None
    return Path(spec.origin) if spec is not None and spec.origin and spec.origin.endswith(".py") else None


def _imports(module: str, path: Path) -> set[str]:
    """Every module of ours that `module`'s source imports, including `from x import submodule`."""
    found: set[str] = set()
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1] + ([base] if base else []))
            names = [base, *(f"{base}.{alias.name}" for alias in node.names)]
        for name in names:
            if (name == _PACKAGE or name.startswith(f"{_PACKAGE}.")) and _source(name) is not None:
                found.add(name)
    return found


@cache
def module_closure(module: str) -> frozenset[str]:
    """Return `module` and every module of ours it imports, directly or through others."""
    seen: set[str] = set()
    todo = [module]
    while todo:
        name = todo.pop()
        path = _source(name)
        if name in seen or path is None:
            continue
        seen.add(name)
        todo.extend(_imports(name, path) - seen)
    return frozenset(seen)


@cache
def code_version(module: str) -> str:
    """Return a short hash of the source of `module` and of every module of ours it imports."""
    digest = hashlib.sha256()
    for name in sorted(module_closure(module)):
        path = _source(name)
        if path is not None:
            digest.update(name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def check_version(check: str) -> str:
    """Return the version of the code that decides `check`."""
    return code_version(CHECK_MODULES.get(check, DEFAULT_MODULE))


def _plain(value: object) -> Any:
    """Turn a check's inputs into JSON with one spelling each: sets sorted, records as fields."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, UUID | date | datetime):
        return str(value)
    if isinstance(value, Enum):
        return _plain(cast(object, value.value))
    if is_dataclass(value) and not isinstance(value, type):
        return {"__record__": type(value).__name__, **_plain(asdict(value))}
    if isinstance(value, Mapping):
        items = cast(Mapping[object, object], value).items()
        return {str(_plain(key)): _plain(item) for key, item in items}
    if isinstance(value, set | frozenset):
        members = [_plain(item) for item in cast(Collection[object], value)]
        return sorted(members, key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, list | tuple):
        return [_plain(item) for item in cast(Iterable[object], value)]
    raise TypeError(f"no stable form for {type(value).__name__}")


def input_digest(*parts: object) -> str:
    """Return the SHA-256 of a check's inputs for one claim."""
    text = json.dumps(_plain(parts), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class Stamp:
    """What a stored check row says, as a run compares it."""

    outcome: str
    reason: str | None
    rule_version: str | None
    input_digest: str | None


def load_stamps(session: Session, checks: Collection[str] | None = None) -> dict[tuple[UUID, str], Stamp]:
    """Return the stored rows, by claim and check name; all checks unless `checks` names some."""
    query = select(
        _CHECK.c.claim_id,
        _CHECK.c.check_name,
        _CHECK.c.outcome,
        _CHECK.c.reason,
        _CHECK.c.rule_version,
        _CHECK.c.input_digest,
    )
    if checks is not None:
        query = query.where(_CHECK.c.check_name.in_(list(checks)))
    return {
        (row.claim_id, row.check_name): Stamp(row.outcome, row.reason, row.rule_version, row.input_digest)
        for row in session.execute(query)
    }


def unchanged(stamp: Stamp | None, check: str, digest: str | None) -> bool:
    """Say whether a stored row was found by today's code from these same inputs."""
    return (
        stamp is not None
        and digest is not None
        and stamp.input_digest == digest
        and stamp.rule_version == check_version(check)
    )


def incremental_counts(claims_unchanged: int, stopped_for_time: bool) -> dict[str, Any]:
    """Return the summary lines a copy-reading check adds, only those that say something."""
    counts: dict[str, Any] = {"claims_unchanged": claims_unchanged} if claims_unchanged else {}
    return counts | ({"stopped_for_time": True} if stopped_for_time else {})


@dataclass(frozen=True)
class Found:
    """One check's outcome for one claim, and the digest of what it read (None: check every run)."""

    claim_id: UUID
    result: CheckResult
    digest: str | None = None


def _row(found: Found, at: datetime) -> dict[str, Any]:
    result = found.result
    return ClaimCheck(
        claim_id=found.claim_id,
        check_name=result.check,
        outcome=result.outcome,
        reason=result.reason[:300] if result.reason else None,
        checked_at=at,
        rule_version=check_version(result.check),
        input_digest=found.digest,
    ).model_dump()


def write_checks(
    session: Session, found: Iterable[Found], at: datetime, stamps: Mapping[tuple[UUID, str], Stamp] | None
) -> int:
    """Store each outcome that differs from its stored row; return how many rows were written.

    With `stamps` None every outcome is written. `checked_at` moves only when the outcome or its
    reason changes, so it says since when the claim has had this outcome: a review approval
    excuses a failure only when it is no newer than the approval (ADR 0042).
    """
    rows: dict[tuple[UUID, str], dict[str, Any]] = {}
    for item in found:
        row = _row(item, at)
        key = (item.claim_id, item.result.check)
        now = Stamp(row["outcome"], row["reason"], row["rule_version"], row["input_digest"])
        if stamps is None or stamps.get(key) != now:
            rows[key] = row  # the last outcome for a claim and check wins, as one-by-one writes did
    values = list(rows.values())
    for start in range(0, len(values), BATCH):
        statement = insert(_CHECK).values(values[start : start + BATCH])
        changed = or_(
            _CHECK.c.outcome.is_distinct_from(statement.excluded.outcome),
            _CHECK.c.reason.is_distinct_from(statement.excluded.reason),
        )
        changes: dict[str, Any] = {
            "outcome": statement.excluded.outcome,
            "reason": statement.excluded.reason,
            "checked_at": case((changed, statement.excluded.checked_at), else_=_CHECK.c.checked_at),
            "rule_version": statement.excluded.rule_version,
            "input_digest": statement.excluded.input_digest,
        }
        session.execute(
            statement.on_conflict_do_update(index_elements=["claim_id", "check_name"], set_=changes)
        )
    return len(values)
