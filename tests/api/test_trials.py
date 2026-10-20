"""Trials on each catalyst in the read API and export; registry phase and size in the export only.

ADR 0039.

A trial is shown only through a trial link whose claim this catalyst's change rows cite, so the
quote that names the trial is one of the catalyst's sources. Phase and size have no claim or
quote, so the read API and MCP tools never return them (spec: Provenance, rules 4 and 8). The
private export adds them, labelled as registry data, with the registry record they come from. A
held claim lists only the trials its names resolved to exactly; nothing is guessed.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.types import CallToolResult
from sqlalchemy import insert, select
from sqlalchemy.orm import Session, sessionmaker

from api.conftest import FIRST, TODAY, UNPUBLISHED_QUOTE, Seed
from catalyst.api.access import Access
from catalyst.api.http import create_app
from catalyst.api.mcp_server import build_server
from catalyst.api.read import catalyst, read_session
from catalyst.db.tables import TABLES
from catalyst.export import REGISTRY_NOTE, build_export
from catalyst.schemas import (
    CatalystTrial,
    Claim,
    ClaimDecision,
    EnrollmentType,
    Entity,
    EntityMatch,
    GateDecision,
    MatchedEntity,
    MatchStatus,
    SourceDocument,
    SourceTier,
    Trial,
    TrialPhase,
)

OLDER = datetime(2026, 3, 2, 5, 0, tzinfo=UTC)
NEWER = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)
FIRST_QUOTE = "topline data in the third quarter of 2026"


def _add(session: Session, record: Entity) -> None:
    session.execute(insert(TABLES[type(record)]).values(**record.model_dump()))


def _registry_record(session: Session, nct_id: str, updated: datetime, n: int) -> None:
    _add(
        session,
        SourceDocument(
            source_key=f"ctgov:{nct_id}:{updated.date().isoformat()}",
            url=f"https://clinicaltrials.gov/study/{nct_id}",
            publisher="ClinicalTrials.gov",
            source_tier=SourceTier.REGISTRY,
            document_type="Registry record",
            published_at=updated,
            retrieved_at=updated,
            sha256=str(n) * 64,
            snapshot_key=f"sha256/{n}{n}/{str(n) * 64}",
            content_type="application/json",
        ),
    )


def _claim(session: Session, quote: str) -> UUID:
    query = select(TABLES[Claim].c.id).where(TABLES[Claim].c.quote == quote)
    return session.execute(query).scalar_one()


@pytest.fixture
def trials(session_factory: sessionmaker[Session], seed: Seed) -> dict[str, UUID]:
    """Link three trials to the slipped readout; the third link cites a claim nothing published."""
    rainier = Trial(
        nct_id="NCT04929483",
        study_name="RAINIER",
        phase=TrialPhase.PHASE_3,
        enrollment=1000,
        enrollment_type=EnrollmentType.ESTIMATED,
    )
    small = Trial(nct_id="NCT01111111", phase=TrialPhase.PHASE_2)  # no size registered, no stored record
    hidden = Trial(nct_id="NCT09999999", study_name="HIDDEN", enrollment=40)
    with session_factory.begin() as session:
        for trial in (rainier, small, hidden):
            _add(session, trial)
        _registry_record(session, rainier.nct_id, OLDER, 8)
        _registry_record(session, rainier.nct_id, NEWER, 9)  # the newest version gives today's values
        first = _claim(session, FIRST_QUOTE)
        for trial, claim in ((rainier, first), (small, first), (hidden, seed.unpublished_claim)):
            _add(session, CatalystTrial(catalyst_id=seed.slipped, trial_id=trial.id, claim_id=claim))
    return {"rainier": rainier.id, "small": small.id, "hidden": hidden.id}


RAINIER = {"nct_id": "NCT04929483", "study_name": "RAINIER"}
SMALL = {"nct_id": "NCT01111111", "study_name": None}
REGISTRY_VALUES = ("phase", "enrollment", "enrollment_type")
# In the export, each trial adds its registry values, labelled as such, beside the quoted link.
RAINIER_EXPORTED = {
    **RAINIER,
    "registry": {
        "note": REGISTRY_NOTE,
        "phase": "phase_3",
        "enrollment": 1000,
        "enrollment_type": "estimated",
        "record": {
            "url": "https://clinicaltrials.gov/study/NCT04929483",
            "published_at": NEWER.isoformat(),
            "retrieved_at": NEWER.isoformat(),
        },
    },
}
SMALL_EXPORTED = {
    **SMALL,
    "registry": {
        "note": REGISTRY_NOTE,
        "phase": "phase_2",
        "enrollment": None,
        "enrollment_type": None,
        "record": None,
    },
}


def _without_ids(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in trial.items() if k != "id"} for trial in trials]


def test_each_catalyst_lists_its_linked_trials_by_registry_number(
    session_factory: sessionmaker[Session], seed: Seed, trials: dict[str, UUID]
) -> None:
    with read_session(session_factory) as session:
        result = catalyst(session, seed.slipped, Access.PAID)
    assert result is not None
    shown = result.catalyst.model_dump(mode="json")["trials"]
    assert [UUID(t["id"]) for t in shown] == [trials["small"], trials["rainier"]]  # by registry number
    assert _without_ids(shown) == [SMALL, RAINIER]


def test_the_quote_that_names_a_trial_is_one_of_the_sources(
    session_factory: sessionmaker[Session], seed: Seed, trials: dict[str, UUID]
) -> None:
    del trials
    with read_session(session_factory) as session:
        result = catalyst(session, seed.slipped, Access.PAID)
    assert result is not None
    by_quote = {s.quote: s.fields for s in result.catalyst.sources}
    assert "trials" in by_quote[FIRST_QUOTE]
    assert all("trials" not in fields for quote, fields in by_quote.items() if quote != FIRST_QUOTE)


def test_a_trial_link_citing_a_claim_this_catalyst_never_published_is_left_out(
    session_factory: sessionmaker[Session],
    seed: Seed,
    trials: dict[str, UUID],
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="catalyst"), read_session(session_factory) as session:
        result = catalyst(session, seed.slipped, Access.PAID)
    assert result is not None
    assert trials["hidden"] not in [t.id for t in result.catalyst.trials]
    assert "HIDDEN" not in result.model_dump_json()
    assert UNPUBLISHED_QUOTE not in result.model_dump_json()
    assert any(r.getMessage() == "trial_link_withheld" for r in caplog.records)


def test_free_callers_see_the_trials_too(
    session_factory: sessionmaker[Session], seed: Seed, trials: dict[str, UUID]
) -> None:
    del trials
    with read_session(session_factory) as session:
        free = catalyst(session, seed.slipped, Access.FREE)
    assert free is not None
    assert _without_ids(free.catalyst.model_dump(mode="json")["trials"]) == [SMALL, RAINIER]


def test_a_catalyst_with_no_trial_links_lists_none(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    with read_session(session_factory) as session:
        result = catalyst(session, seed.pdufa, Access.PAID)
    assert result is not None
    assert result.catalyst.trials == []


def _export(factory: sessionmaker[Session]) -> dict[str, Any]:
    with read_session(factory) as session:
        return build_export(session, FIRST, include_held=True)


def test_the_export_lists_each_published_catalysts_trials(
    session_factory: sessionmaker[Session], seed: Seed, trials: dict[str, UUID]
) -> None:
    del trials
    export = _export(session_factory)
    by_id = {UUID(item["id"]): item for item in export["catalysts"]}
    assert _without_ids(by_id[seed.slipped]["trials"]) == [SMALL_EXPORTED, RAINIER_EXPORTED]
    assert all(by_id[other]["trials"] == [] for other in (seed.pdufa, seed.adcom, seed.resolved))


def _match(session: Session, claim: UUID, name: str, status: MatchStatus, *candidates: UUID) -> None:
    _add(
        session,
        EntityMatch(
            claim_id=claim,
            entity_type=MatchedEntity.TRIAL,
            name=name,
            status=status,
            matched_id=candidates[0] if status is MatchStatus.RESOLVED else None,
            candidates=[str(c) for c in candidates],
            resolved_at=FIRST,
        ),
    )


def test_a_held_claim_lists_only_the_trials_its_names_resolved_to(
    session_factory: sessionmaker[Session], seed: Seed, trials: dict[str, UUID]
) -> None:
    with session_factory.begin() as session:
        _add(
            session,
            ClaimDecision(
                claim_id=seed.unpublished_claim, decision=GateDecision.REVIEW, reasons=["x"], decided_at=FIRST
            ),
        )
        _match(session, seed.unpublished_claim, "RAINIER", MatchStatus.RESOLVED, trials["rainier"])
        # Names that did not resolve to exactly one trial are not guessed at.
        _match(
            session,
            seed.unpublished_claim,
            "Phase 2",
            MatchStatus.AMBIGUOUS,
            trials["small"],
            trials["hidden"],
        )
        _match(session, seed.unpublished_claim, "the extension study", MatchStatus.UNKNOWN)
    [held] = _export(session_factory)["held"]
    assert _without_ids(held["trials"]) == [RAINIER_EXPORTED]


def test_a_held_claim_with_no_resolved_trial_lists_none(
    session_factory: sessionmaker[Session], seed: Seed, trials: dict[str, UUID]
) -> None:
    del trials
    with session_factory.begin() as session:
        _add(
            session,
            ClaimDecision(
                claim_id=seed.unpublished_claim, decision=GateDecision.REVIEW, reasons=["x"], decided_at=FIRST
            ),
        )
    [held] = _export(session_factory)["held"]
    assert held["trials"] == []


def _keys(node: Any) -> set[str]:
    """Every key in a JSON document, including the property names of a JSON schema."""
    if isinstance(node, dict):
        pairs: dict[str, Any] = node  # type: ignore[assignment]
        return set(pairs) | {k for value in pairs.values() for k in _keys(value)}
    if isinstance(node, list):
        items: list[Any] = node  # type: ignore[assignment]
        return {k for item in items for k in _keys(item)}
    return set()


def _registry_named(keys: set[str]) -> set[str]:
    return {k for k in keys if k in REGISTRY_VALUES or k.startswith("enrollment") or k == "registry"}


def test_the_api_and_mcp_never_return_registry_phase_or_size(
    session_factory: sessionmaker[Session], seed: Seed, trials: dict[str, UUID]
) -> None:
    del trials
    app = create_app(session_factory, today=lambda: TODAY)
    assert _registry_named(_keys(app.openapi()["components"])) == set()
    paths = ["/v1/catalysts", f"/v1/catalysts/{seed.slipped}", "/v1/companies/EXB/catalysts"]
    with TestClient(app) as client:
        bodies = [client.get(path).json() for path in paths]
    assert "RAINIER" in json.dumps(bodies[1])  # the trial itself is shown
    for body in bodies:
        assert _registry_named(_keys(body)) == set()
    server = build_server(session_factory, today=lambda: TODAY)
    for tool in anyio.run(server.list_tools):
        assert _registry_named(_keys(tool.output_schema)) == set()
    result = anyio.run(server.call_tool, "get_catalyst", {"catalyst_id": str(seed.slipped)})
    assert isinstance(result, CallToolResult)
    assert "RAINIER" in json.dumps(result.structured_content)
    assert _registry_named(_keys(result.structured_content)) == set()


def test_the_export_labels_registry_values_as_not_quoted(
    session_factory: sessionmaker[Session], seed: Seed, trials: dict[str, UUID]
) -> None:
    del trials
    [slipped] = [c for c in _export(session_factory)["catalysts"] if UUID(c["id"]) == seed.slipped]
    for trial in slipped["trials"]:
        assert trial["registry"]["note"] == REGISTRY_NOTE
        assert "not a quoted claim" in REGISTRY_NOTE
    # The rest of each catalyst is exactly what the read API shows.
    with read_session(session_factory) as session:
        shown = catalyst(session, seed.slipped, Access.PAID)
    assert shown is not None
    api = shown.catalyst.model_dump(mode="json")
    assert {k: v for k, v in slipped.items() if k != "trials"} == {
        k: v for k, v in api.items() if k != "trials"
    }
    assert [{k: v for k, v in t.items() if k != "registry"} for t in slipped["trials"]] == api["trials"]
