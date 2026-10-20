"""Trials on each catalyst, with their registry size, in the read API and the export (ADR 0038).

A trial is shown only through a trial link whose claim this catalyst's change rows cite, so the
quote that names the trial is one of the catalyst's sources. Phase and size are registry values:
each trial points to the stored registry record they come from. A held claim lists only the
trials its names resolved to exactly; nothing is guessed.
"""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import insert, select
from sqlalchemy.orm import Session, sessionmaker

from api.conftest import FIRST, UNPUBLISHED_QUOTE, Seed
from catalyst.api.access import Access
from catalyst.api.read import catalyst, read_session
from catalyst.db.tables import TABLES
from catalyst.export import build_export
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


RAINIER = {
    "nct_id": "NCT04929483",
    "study_name": "RAINIER",
    "phase": "phase_3",
    "enrollment": 1000,
    "enrollment_type": "estimated",
    "registry_record": {
        "url": "https://clinicaltrials.gov/study/NCT04929483",
        "published_at": NEWER.isoformat().replace("+00:00", "Z"),
        "retrieved_at": NEWER.isoformat().replace("+00:00", "Z"),
    },
}
SMALL = {
    "nct_id": "NCT01111111",
    "study_name": None,
    "phase": "phase_2",
    "enrollment": None,
    "enrollment_type": None,
    "registry_record": None,
}


def _without_ids(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in trial.items() if k != "id"} for trial in trials]


def test_each_catalyst_lists_its_linked_trials_with_phase_and_size(
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
    assert _without_ids(by_id[seed.slipped]["trials"]) == [SMALL, RAINIER]
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
    assert _without_ids(held["trials"]) == [RAINIER]


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
