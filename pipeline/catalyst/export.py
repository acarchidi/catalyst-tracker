"""One JSON file of the live data for the private prototype (ADR 0034).

Published catalysts come from the read layer (`catalyst.api.read.published`), so they are
exactly what the read API shows a paid caller, under the same rule: a record with a value no
source states is left out. Held claims, when asked for, are a separate list read from the
publish gate's stored decisions; they never enter the published list. Each lists only the trials
its names resolved to exactly one of. Only here, each trial also carries its registry phase and
size, labelled as registry data with the record they come from, since no quote states them; the
read API never returns them (ADR 0039). Prices and returns are never read (spec: data license).
"""

import json
import os
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from catalyst.api.access import NOTICE
from catalyst.api.read import company_views, published, trial_views
from catalyst.api.views import CatalystDetail, CompanyView, TrialView
from catalyst.db.tables import TABLES
from catalyst.extract.agent import EXTRACTOR_PROMPT
from catalyst.publish.live import claim_value
from catalyst.schemas import (
    Claim,
    ClaimDecision,
    EntityMatch,
    GateDecision,
    MatchedEntity,
    MatchStatus,
    SourceDocument,
    SourceTier,
    Trial,
)
from catalyst.verify.second import VERIFIER_PROMPT

HELD_STATUS = "held for review"
REGISTRY_NOTE = (
    "Registry data from ClinicalTrials.gov, not a quoted claim: shown as the registry reports it, "
    "not checked by the publish gate."
)


def build_export(session: Session, now: datetime, *, include_held: bool) -> dict[str, Any]:
    """Return the export: the notice, counts, published catalysts, and, if asked, the held claims.

    `counts.held` is None when held claims were not asked for. `companies` counts the companies
    with at least one published catalyst.
    """
    # One snapshot, so the counts and both lists agree even while a job writes.
    session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
    details = sorted(published(session), key=_catalyst_order)
    held = held_claims(session) if include_held else None
    export: dict[str, Any] = {
        "notice": NOTICE,
        "generated_at": now.astimezone(UTC).isoformat(),
        "counts": {
            "published": len(details),
            "held": None if held is None else len(held),
            "companies": len({d.company.id for d in details}),
        },
        # The prompts the publish gate requires now; a claim from any other is held.
        "prompt_versions": {"extractor": "/".join(EXTRACTOR_PROMPT), "verifier": "/".join(VERIFIER_PROMPT)},
        "catalysts": _with_registry(session, [d.model_dump(mode="json") for d in details]),
    }
    if held is not None:
        export["held"] = held
    return export


def _catalyst_order(detail: CatalystDetail) -> tuple[Any, ...]:
    return (detail.window.start, detail.company.name, str(detail.id))


def held_claims(session: Session) -> list[dict[str, Any]]:
    """Return every claim the publish gate holds for review, with its source and stored reasons."""
    decision, claim, document = TABLES[ClaimDecision], TABLES[Claim], TABLES[SourceDocument]
    rows = session.execute(
        select(
            claim.c.id,
            claim.c.value,
            claim.c.quote,
            claim.c.prompt_id,
            claim.c.prompt_version,
            decision.c.reasons,
            decision.c.decided_at,
            document.c.url,
            document.c.publisher,
            document.c.document_type,
            document.c.published_at,
            document.c.retrieved_at,
        )
        .join(claim, claim.c.id == decision.c.claim_id)
        .join(document, document.c.id == claim.c.source_document_id)
        .where(decision.c.decision == GateDecision.REVIEW)
    ).all()
    companies = _held_companies(session, [row.id for row in rows])
    trials = _held_trials(session, [row.id for row in rows])
    held = [
        {
            "claim_id": str(row.id),
            "status": HELD_STATUS,
            "company": _company(companies.get(row.id)),
            "values": row.value,
            "trials": [trial.model_dump(mode="json") for trial in trials.get(row.id, [])],
            "quote": row.quote,
            "source": {
                "url": row.url,
                "publisher": row.publisher,
                "document_type": row.document_type,
                "published_at": row.published_at.isoformat(),
                "retrieved_at": row.retrieved_at.isoformat(),
            },
            "reasons": list(row.reasons),
            "decided_at": row.decided_at.isoformat(),
            "prompt": f"{row.prompt_id}/{row.prompt_version}",
        }
        for row in rows
    ]
    return _with_registry(session, sorted(held, key=_held_order))


def _company(view: CompanyView | None) -> dict[str, Any] | None:
    return None if view is None else view.model_dump(mode="json")


def _held_companies(session: Session, claim_ids: list[UUID]) -> dict[UUID, CompanyView]:
    """Return each held claim's company, when its names resolved to exactly one."""
    match = TABLES[EntityMatch]
    found: defaultdict[UUID, set[UUID]] = defaultdict(set)
    query = select(match.c.claim_id, match.c.matched_id).where(
        match.c.claim_id.in_(claim_ids),
        match.c.entity_type == MatchedEntity.COMPANY,
        match.c.status == MatchStatus.RESOLVED,
    )
    for row in session.execute(query):
        found[row.claim_id].add(row.matched_id)
    single = {claim_id: next(iter(ids)) for claim_id, ids in found.items() if len(ids) == 1}
    views = company_views(session, list(set(single.values())))
    return {claim_id: views[company_id] for claim_id, company_id in single.items()}


def _held_trials(session: Session, claim_ids: list[UUID]) -> dict[UUID, list[TrialView]]:
    """Return the trials each held claim's names resolved to, by registry number; never a guess."""
    match = TABLES[EntityMatch]
    query = select(match.c.claim_id, match.c.matched_id).where(
        match.c.claim_id.in_(claim_ids),
        match.c.entity_type == MatchedEntity.TRIAL,
        match.c.status == MatchStatus.RESOLVED,
    )
    found: defaultdict[UUID, set[UUID]] = defaultdict(set)
    for row in session.execute(query):
        found[row.claim_id].add(row.matched_id)
    views = trial_views(session, list({trial for ids in found.values() for trial in ids}))
    return {
        claim_id: sorted((views[t] for t in ids if t in views), key=lambda t: (t.nct_id, str(t.id)))
        for claim_id, ids in found.items()
    }


def _with_registry(session: Session, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add each listed trial's registry phase and size, labelled as registry data (ADR 0039)."""
    ids = [UUID(trial["id"]) for item in items for trial in item["trials"]]
    registry = registry_values(session, ids)
    for item in items:
        item["trials"] = [{**trial, "registry": registry[UUID(trial["id"])]} for trial in item["trials"]]
    return items


def registry_values(session: Session, trial_ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    """Return each trial's phase and size, with the newest stored registry record, by id.

    The trial row is updated only after its record is stored, so the newest stored record is the
    one these values were last read from (ADR 0039).
    """
    trial, document = TABLES[Trial], TABLES[SourceDocument]
    rows = session.execute(select(trial).where(trial.c.id.in_(trial_ids))).all()
    nct_ids = [row.nct_id for row in rows]
    records: dict[str, dict[str, str]] = {}
    statement = (
        select(document.c.source_key, document.c.url, document.c.published_at, document.c.retrieved_at)
        .where(
            document.c.source_tier == SourceTier.REGISTRY,
            document.c.source_key.startswith("ctgov:"),
            func.split_part(document.c.source_key, ":", 2).in_(nct_ids),
        )
        .order_by(document.c.published_at, document.c.retrieved_at, document.c.id)
    )
    for doc in session.execute(statement):  # oldest first, so the newest version wins
        records[doc.source_key.split(":")[1]] = {
            "url": doc.url,
            "published_at": doc.published_at.astimezone(UTC).isoformat(),
            "retrieved_at": doc.retrieved_at.astimezone(UTC).isoformat(),
        }
    return {
        row.id: {
            "note": REGISTRY_NOTE,
            "phase": row.phase,
            "enrollment": row.enrollment,
            "enrollment_type": row.enrollment_type,
            "record": records.get(row.nct_id),
        }
        for row in rows
    }


def _held_order(item: dict[str, Any]) -> tuple[Any, ...]:
    start = claim_value(item["values"]).get("window_start")
    start = start if isinstance(start, str) else ""
    name = item["company"]["name"] if item["company"] else ""
    return (start == "", start, name, item["claim_id"])  # undated claims last


def write_export(path: Path, export: dict[str, Any]) -> None:
    """Write the export as UTF-8 JSON, indent 1; a failed write leaves any earlier file whole."""
    body = json.dumps(export, indent=1, ensure_ascii=False) + "\n"
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(body)
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
