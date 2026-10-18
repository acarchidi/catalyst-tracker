"""The catalyst writer on a disposable database with a local snapshot store (ADR 0018, part 5b).

Each test stores real-shaped rows: a release, its company link, an extractor claim, the
resolver's matches, stored check rows, and the stored decision. The writer must check again
before it writes, and what it writes must be what the read API shows (ADR 0025).
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session, sessionmaker

from catalyst.api.access import Access
from catalyst.api.read import catalyst as read_catalyst
from catalyst.collectors.documents import FetchedDocument, store_fetched
from catalyst.db.tables import TABLES
from catalyst.extract.agent import EXTRACTOR_PROMPT, claim_field
from catalyst.extract.dates import parse_window
from catalyst.extract.run import NEW_YORK
from catalyst.extract.text import TEXT_VERSION, document_text
from catalyst.publish import gate
from catalyst.publish.run import decide_claims
from catalyst.publish.write import publish_claims, ranks_above
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CatalystDrug,
    CatalystTrial,
    CheckOutcome,
    Claim,
    ClaimCheck,
    ClaimDecision,
    ClaimSubject,
    Company,
    DocumentMatch,
    Drug,
    EntityMatch,
    GateDecision,
    MatchedEntity,
    MatchStatus,
    SourceTier,
    Trial,
    TrialPhase,
)
from catalyst.snapshots import LocalSnapshotStore

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
OCT_14 = datetime(2026, 10, 14, 20, tzinfo=UTC)
OCT_16 = datetime(2026, 10, 16, 20, tzinfo=UTC)


def _sentence(date_text: str, drug: str = "pegozafermin") -> str:
    return f"Topline data from the Phase 3 RAINIER trial of {drug} are expected in the {date_text}."


class World:
    """One company with one drug and one registered trial, and helpers to add releases and claims."""

    def __init__(self, factory: sessionmaker[Session], store: LocalSnapshotStore) -> None:
        self.factory, self.store = factory, store
        self.company = self.add(Company(cik=1, name="Acme Bio", sic_code="2834"))
        self.drug = self.add(Drug(company_id=self.company, preferred_name="pegozafermin"))
        self.trial = self.add(Trial(nct_id="NCT04929483", study_name="RAINIER", phase=TrialPhase.PHASE_3))
        self._releases = 0

    def add(self, record: Any) -> UUID:
        with self.factory.begin() as session:
            session.execute(insert(TABLES[type(record)]).values(**record.model_dump()))
        return record.id

    def claim(
        self,
        date_text: str,
        *,
        published: datetime = OCT_14,
        tier: SourceTier = SourceTier.REGULATOR_OR_SEC,
        drug: str | None = "pegozafermin",
        drug_id: UUID | None = None,
        trial: bool = True,
        catalyst_type: str = "topline_readout",
        decision: GateDecision = GateDecision.PUBLISH,
        stored: dict[str, CheckOutcome] | None = None,
        sentence: str | None = None,
    ) -> UUID:
        """Store a release stating one catalyst, its claim, matches, passing check rows, and decision."""
        self._releases += 1
        n = self._releases
        sentence = sentence or _sentence(date_text, drug or "the program")
        content = f"<html><body><p>Release {n}</p><p>{sentence}</p></body></html>".encode()
        fetched = FetchedDocument(
            source_key=f"sec:{n}",
            url=f"https://www.sec.gov/Archives/edgar/data/1/{n}.htm",
            publisher="SEC EDGAR",
            source_tier=tier,
            document_type="8-K EX-99.1",
            published_at=published,
            content=content,
            content_type="text/html",
        )
        document, _ = store_fetched(
            fetched, self.store, self.factory, (self.company, DocumentMatch.SEC_CIK), lambda: published
        )
        text = document_text(content, "text/html")
        window = parse_window(date_text, published.astimezone(NEW_YORK).date())
        assert window is not None
        value: dict[str, Any] = {
            "catalyst_type": catalyst_type,
            "title": f"Phase 3 RAINIER topline data for {drug or 'the program'} in MASH",
            "drug": drug,
            "indication": "MASH",
            "date_text": date_text,
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            "window_precision": window.precision.value,
            "date_confidence": "company_confirmed",
            "problems": [],
            "text_version": TEXT_VERSION,
        }
        start = text.index(sentence)
        claim = Claim(
            source_document_id=document.id,
            subject_type=ClaimSubject.CATALYST,
            field=claim_field(value),
            value=value,
            quote=sentence,
            quote_start=start,
            quote_end=start + len(sentence),
            agent="extractor",
            prompt_id=EXTRACTOR_PROMPT[0],
            prompt_version=EXTRACTOR_PROMPT[1],
            provider="anthropic",
            model="claude-sonnet-5-5",
            extracted_at=published + timedelta(hours=1),
        )
        self.add(claim)
        self.match(claim.id, MatchedEntity.COMPANY, "source document", self.company)
        if drug:
            self.match(claim.id, MatchedEntity.DRUG, drug, drug_id or self.drug)
        if trial:
            self.match(claim.id, MatchedEntity.TRIAL, "RAINIER", self.trial)
        outcomes = {name: CheckOutcome.PASS for name in gate.REQUIRED_CHECKS} | (stored or {})
        for name, outcome in outcomes.items():
            reason = "stored as failed" if outcome is CheckOutcome.FAIL else None
            self.add(
                ClaimCheck(
                    claim_id=claim.id, check_name=name, outcome=outcome, reason=reason, checked_at=published
                )
            )
        reasons = [] if decision is GateDecision.PUBLISH else ["held"]
        self.add(ClaimDecision(claim_id=claim.id, decision=decision, reasons=reasons, decided_at=published))
        return claim.id

    def match(self, claim: UUID, entity: MatchedEntity, name: str, record: UUID | None) -> None:
        status = MatchStatus.RESOLVED if record else MatchStatus.AMBIGUOUS
        self.add(
            EntityMatch(
                claim_id=claim,
                entity_type=entity,
                name=name,
                status=status,
                matched_id=record,
                candidates=[str(record)] if record else [],
                resolved_at=NOW,
            )
        )

    def rows(self, model: type[Any]) -> list[Any]:
        table = TABLES[model]
        with self.factory() as session:
            return list(session.execute(select(table).order_by(*table.primary_key.columns)))

    def counts(self) -> tuple[int, ...]:
        with self.factory() as session:
            return tuple(
                session.execute(select(func.count()).select_from(TABLES[m])).scalar_one()
                for m in (Catalyst, CatalystChange, CatalystDrug, CatalystTrial)
            )

    def shown(self, catalyst_id: UUID) -> Any:
        with self.factory() as session:
            result = read_catalyst(session, catalyst_id, Access.PAID)
        assert result is not None, "the read API withholds what the writer wrote"
        return result.catalyst


@pytest.fixture
def world(session_factory: sessionmaker[Session], tmp_path: Path) -> World:
    return World(session_factory, LocalSnapshotStore(tmp_path))


def _publish(world: World, at: datetime = NOW) -> dict[str, Any]:
    return publish_claims(world.factory, world.store, lambda: at).summary()


# --- A publishable claim becomes a catalyst the read API shows ---


def test_a_publishable_claim_becomes_a_catalyst_the_read_api_shows_with_every_source(world: World) -> None:
    claim = world.claim("second half of 2027")
    summary = _publish(world)
    assert (summary["catalysts_created"], summary["changes_written"]) == (1, 10)
    assert (summary["drug_links_written"], summary["trial_links_written"]) == (1, 1)
    (row,) = world.rows(Catalyst)
    shown = world.shown(row.id)
    assert shown.title == "Phase 3 RAINIER topline data for pegozafermin in MASH"
    assert (shown.window.start.isoformat(), shown.window.end.isoformat()) == ("2027-07-01", "2027-12-31")
    assert (shown.window.precision, shown.window.raw_text) == ("half", "second half of 2027")
    assert (shown.status, shown.date_confidence, shown.catalyst_type) == (
        "upcoming",
        "company_confirmed",
        "topline_readout",
    )
    assert shown.company.id == world.company
    assert [(d.id, d.name, d.role) for d in shown.drugs] == [(world.drug, "pegozafermin", "alone")]
    (source,) = shown.sources
    assert source.quote == _sentence("second half of 2027")
    assert set(source.fields) == {
        "catalyst_type",
        "company_id",
        "title",
        "window_start",
        "window_end",
        "window_precision",
        "window_raw_text",
        "date_confidence",
        "status",
        "drugs",
    }
    assert {change.claim_id for change in world.rows(CatalystChange)} == {claim}
    assert all(change.old_value is None for change in world.rows(CatalystChange))
    assert shown.latest_date_change == []  # a first value is not a date move


def test_each_link_names_the_claim_that_states_it_for_that_catalyst_and_field(world: World) -> None:
    """PR #23 review: a link's claim is about that catalyst and that field."""
    world.claim("second half of 2027")
    other_drug = world.add(Drug(company_id=world.company, preferred_name="efruxifermin"))
    world.claim("first half of 2028", drug="efruxifermin", drug_id=other_drug, trial=False)
    _publish(world)
    changes = world.rows(CatalystChange)
    matches = world.rows(EntityMatch)
    for link in world.rows(CatalystDrug):
        drug_rows = [c for c in changes if c.catalyst_id == link.catalyst_id and c.field == "drugs"]
        assert link.claim_id in {c.claim_id for c in drug_rows}
        stated = {m.matched_id for m in matches if m.claim_id == link.claim_id and m.entity_type == "drug"}
        assert stated == {link.drug_id}
    (trial_link,) = world.rows(CatalystTrial)
    backing = {c.claim_id for c in changes if c.catalyst_id == trial_link.catalyst_id}
    assert trial_link.claim_id in backing
    stated = {m.matched_id for m in matches if m.claim_id == trial_link.claim_id and m.entity_type == "trial"}
    assert stated == {trial_link.trial_id}
    assert len(world.rows(Catalyst)) == 2


def test_a_quote_that_says_combination_links_the_drug_in_combination(world: World) -> None:
    sentence = (
        "Topline data from the Phase 3 RAINIER trial of pegozafermin in combination with "
        "semaglutide are expected in the second half of 2027."
    )
    world.claim("second half of 2027", sentence=sentence)
    _publish(world)
    (link,) = world.rows(CatalystDrug)
    assert link.role == "in_combination"


def test_a_claim_with_no_drug_writes_no_drug_link_and_is_still_shown(world: World) -> None:
    world.claim("second half of 2027", drug=None)
    summary = _publish(world)
    assert (summary["catalysts_created"], summary["changes_written"], summary["drug_links_written"]) == (
        1,
        9,
        0,
    )
    (row,) = world.rows(Catalyst)
    assert world.shown(row.id).drugs == []


# --- Idempotent and safe to retry ---


def test_a_rerun_with_nothing_new_writes_nothing(world: World) -> None:
    world.claim("second half of 2027")
    _publish(world)
    before = (world.counts(), world.rows(Catalyst))
    summary = _publish(world, NOW + timedelta(hours=6))
    assert (world.counts(), world.rows(Catalyst)) == before
    assert summary["already_published"] == 1
    assert (summary["catalysts_created"], summary["catalysts_updated"], summary["changes_written"]) == (
        0,
        0,
        0,
    )


def test_two_claims_about_one_event_make_one_catalyst(world: World) -> None:
    world.claim("second half of 2027")
    world.claim("second half of 2027", published=OCT_16)
    summary = _publish(world)
    assert summary["catalysts_created"] == 1
    assert len(world.rows(Catalyst)) == 1


def test_a_failure_while_writing_leaves_nothing_half_written(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.claim("second half of 2027")
    from catalyst.publish import write

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("connection lost")

    monkeypatch.setattr(write, "_write_links", broken)
    with pytest.raises(RuntimeError):
        _publish(world)
    assert world.counts() == (0, 0, 0, 0)
    monkeypatch.undo()
    assert _publish(world)["catalysts_created"] == 1  # the retry finishes the job


# --- Only a fresh publish writes: stored outcomes may be stale ---


def test_only_claims_the_gate_marked_to_publish_are_considered(world: World) -> None:
    world.claim("second half of 2027", decision=GateDecision.REVIEW)
    summary = _publish(world)
    assert summary["claims_marked_publish"] == 0
    assert world.counts() == (0, 0, 0, 0)


def test_a_window_that_passed_since_the_decision_is_not_published(world: World) -> None:
    claim = world.claim("fourth quarter of 2026")
    later = datetime(2027, 1, 5, 17, tzinfo=UTC)
    summary = _publish(world, later)
    assert world.counts() == (0, 0, 0, 0)
    assert summary["held_on_recheck"] == 1
    assert summary["top_reasons"] == {"window_not_past failed": 1}
    # The fresh reason replaces the stale publish decision, so the review queue shows it.
    (decision,) = world.rows(ClaimDecision)
    assert (decision.claim_id, decision.decision, decision.decided_at) == (claim, "review", later)
    assert decision.reasons == ["window_not_past: the window ended on 2026-12-31, before 2027-01-05"]
    assert _publish(world, later + timedelta(hours=1))["claims_marked_publish"] == 0
    assert world.rows(ClaimDecision) == [decision]  # a re-run writes nothing


def test_a_match_that_changed_since_the_decision_is_not_published(world: World) -> None:
    claim = world.claim("second half of 2027")
    match = TABLES[EntityMatch]
    with world.factory.begin() as session:
        session.execute(
            update(match)
            .where(match.c.claim_id == claim, match.c.entity_type == "drug")
            .values(status="ambiguous", matched_id=None, candidates=[])
        )
    summary = _publish(world)
    assert world.counts() == (0, 0, 0, 0)
    assert summary["top_reasons"] == {"names_matched failed": 1}


def test_a_copy_that_changed_since_the_decision_is_not_published(world: World, tmp_path: Path) -> None:
    world.claim("second half of 2027")
    (copy,) = [p for p in tmp_path.rglob("*") if p.is_file()]
    copy.write_bytes(b"<p>something else</p>")
    summary = _publish(world)
    assert world.counts() == (0, 0, 0, 0)
    assert summary["copies_unreadable"] == 1
    assert summary["top_reasons"] == {"quote_matches_stored_copy failed": 1}


def test_a_stored_rule_failure_is_not_trusted_either_way(world: World) -> None:
    """The rule checks are recomputed, so a stale stored failure does not hold a good claim."""
    world.claim("second half of 2027", stored={"has_window": CheckOutcome.FAIL})
    assert _publish(world)["catalysts_created"] == 1


def test_the_second_reading_is_read_as_stored(world: World) -> None:
    """It needs a model call, and this job makes none, so its stored outcome counts."""
    world.claim("second half of 2027", stored={"second_reading_agrees": CheckOutcome.FAIL})
    unread = world.claim("second half of 2027", published=OCT_16)
    check = TABLES[ClaimCheck]
    with world.factory.begin() as session:
        session.execute(
            delete(check).where(check.c.claim_id == unread, check.c.check_name == "second_reading_agrees")
        )
    assert _publish(world)["top_reasons"] == {
        "second_reading_agrees failed": 1,
        "second_reading_agrees has not run": 1,
    }
    assert world.counts() == (0, 0, 0, 0)


def test_a_required_check_the_writer_does_not_recompute_is_read_from_the_gates_list(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #55 will add checks to the gate's list; the writer reads that list, never a copy."""
    claim = world.claim("second half of 2027")
    monkeypatch.setattr(gate, "REQUIRED_CHECKS", (*gate.REQUIRED_CHECKS, "company_named_in_source"))
    assert _publish(world)["top_reasons"] == {"company_named_in_source has not run": 1}
    world.add(
        ClaimCheck(
            claim_id=claim, check_name="company_named_in_source", outcome=CheckOutcome.PASS, checked_at=NOW
        )
    )
    decide_claims(world.factory, world.store, lambda: NOW)  # the decision job sees the new row first
    assert _publish(world)["catalysts_created"] == 1


def test_the_independent_source_check_is_recomputed(world: World) -> None:
    """A wire copy with no second document fails now, whatever its stored row says."""
    world.claim("second half of 2027", tier=SourceTier.WIRE)
    assert _publish(world)["top_reasons"] == {"independent_source failed": 1}


def test_the_advisory_check_is_recomputed(world: World) -> None:
    """No Federal Register notice is stored, so a claimed advisory meeting cannot publish."""
    world.claim(
        "fourth quarter of 2027",
        catalyst_type="advisory_committee",
        trial=False,
        sentence="An FDA advisory committee on pegozafermin should meet in the fourth quarter of 2027.",
    )
    assert _publish(world)["top_reasons"] == {"advisory_meeting_noticed failed": 1}


# --- window_not_past applies only before first publication ---


def _date_passed(world: World, claim: UUID) -> None:
    """Store `window_not_past` as failed, as the check job would once the window ends."""
    check = TABLES[ClaimCheck]
    with world.factory.begin() as session:
        session.execute(
            update(check)
            .where(check.c.claim_id == claim, check.c.check_name == "window_not_past")
            .values(outcome="fail", reason="the window ended")
        )


def test_a_live_catalyst_whose_date_passes_stays_live_and_nothing_is_written(world: World) -> None:
    claim = world.claim("fourth quarter of 2026")
    _publish(world)
    before = (world.counts(), world.rows(Catalyst))
    later = datetime(2027, 1, 5, 17, tzinfo=UTC)
    summary = _publish(world, later)
    assert (world.counts(), world.rows(Catalyst)) == before
    assert summary["held_on_recheck"] == 0
    _date_passed(world, claim)
    decide_claims(world.factory, world.store, lambda: later)
    (decision,) = world.rows(ClaimDecision)
    assert (decision.claim_id, decision.decision) == (claim, "publish")  # not a review item


def test_a_new_claim_about_a_live_catalyst_publishes_even_after_its_date_passed(world: World) -> None:
    world.claim("fourth quarter of 2026")
    _publish(world)
    narrower = world.claim("December 2026", published=datetime(2026, 11, 20, 20, tzinfo=UTC))
    _date_passed(world, narrower)
    later = datetime(2027, 1, 5, 17, tzinfo=UTC)
    decide_claims(world.factory, world.store, lambda: later)
    decisions = {row.claim_id: row.decision for row in world.rows(ClaimDecision)}
    assert decisions[narrower] == "publish"
    summary = _publish(world, later)
    assert summary["catalysts_updated"] == 1
    (row,) = world.rows(Catalyst)
    assert (row.window_start.isoformat(), row.window_end.isoformat()) == ("2026-12-01", "2026-12-31")


def test_the_same_claim_with_no_live_catalyst_waits(world: World) -> None:
    claim = world.claim("December 2026", published=datetime(2026, 11, 20, 20, tzinfo=UTC))
    _date_passed(world, claim)
    later = datetime(2027, 1, 5, 17, tzinfo=UTC)
    decide_claims(world.factory, world.store, lambda: later)
    (decision,) = world.rows(ClaimDecision)
    assert (decision.decision, decision.reasons) == ("review", ["window_not_past: the window ended"])
    assert _publish(world, later)["claims_marked_publish"] == 0
    assert world.counts() == (0, 0, 0, 0)


# --- Date moves: which claim wins, and when a move waits for review ---


def test_a_newer_release_that_narrows_the_window_moves_the_date(world: World) -> None:
    first = world.claim("second half of 2027")
    newer = world.claim("fourth quarter of 2027", published=OCT_16)
    _publish_in_order(world, first)
    summary = _publish(world)
    assert (summary["catalysts_updated"], summary["changes_written"]) == (1, 3)
    (row,) = world.rows(Catalyst)
    shown = world.shown(row.id)
    assert (shown.window.start.isoformat(), shown.window.precision) == ("2027-10-01", "quarter")
    moved = {(c.field, c.old_value, c.new_value) for c in shown.latest_date_change}
    assert moved == {
        ("window_start", "2027-07-01", "2027-10-01"),
        ("window_precision", "half", "quarter"),
        ("window_raw_text", "second half of 2027", "fourth quarter of 2027"),
    }  # the end stays December 31, so it has no row
    assert {c.source.quote for c in shown.latest_date_change} == {_sentence("fourth quarter of 2027")}
    window_rows = [c for c in world.rows(CatalystChange) if c.old_value is not None]
    assert {c.claim_id for c in window_rows} == {newer}
    assert _publish(world)["changes_written"] == 0


def _publish_in_order(world: World, first: UUID) -> None:
    """Publish only `first`, by holding every other claim for one run."""
    decision = TABLES[ClaimDecision]
    with world.factory.begin() as session:
        held = list(
            session.execute(
                select(decision.c.claim_id).where(
                    decision.c.claim_id != first, decision.c.decision == "publish"
                )
            ).scalars()
        )
        session.execute(
            update(decision).where(decision.c.claim_id.in_(held)).values(decision="review", reasons=["later"])
        )
    _publish(world)
    with world.factory.begin() as session:
        session.execute(
            update(decision).where(decision.c.claim_id.in_(held)).values(decision="publish", reasons=[])
        )


def test_an_older_release_does_not_move_a_newer_ones_date(world: World) -> None:
    newer = world.claim("fourth quarter of 2027", published=OCT_16)
    world.claim("second half of 2027")
    _publish_in_order(world, newer)
    summary = _publish(world)
    assert (summary["catalysts_updated"], summary["not_written_lower_rank"]) == (0, 1)
    (row,) = world.rows(Catalyst)
    assert row.window_precision == "quarter"


def test_claims_first_seen_together_take_the_winners_values(world: World) -> None:
    world.claim("second half of 2027")
    world.claim("fourth quarter of 2027", published=OCT_16)
    _publish(world)
    (row,) = world.rows(Catalyst)
    assert row.window_raw_text == "fourth quarter of 2027"
    assert all(change.old_value is None for change in world.rows(CatalystChange))


def test_a_window_that_does_not_overlap_the_live_one_waits_for_a_person(world: World) -> None:
    world.claim("second half of 2027")
    _publish(world)
    world.claim("first half of 2028", published=OCT_16)
    summary = _publish(world)
    assert summary["held_by_writer"] == 1
    assert list(summary["top_reasons"]) == ["the window does not overlap the published one"]
    (row,) = world.rows(Catalyst)
    assert row.window_raw_text == "second half of 2027"


def test_a_move_of_more_than_six_months_waits_for_a_person(world: World) -> None:
    """Spec, Change sanity: a date that moves more than 6 months always goes to review."""
    world.claim("October 2027")
    _publish(world)
    moved = world.claim("late 2027 or early 2028", published=OCT_16)  # the end moves from October to June
    summary = _publish(world)
    assert summary["held_by_writer"] == 1
    assert list(summary["top_reasons"]) == ["the date would move more than 6 months"]
    assert len([c for c in world.rows(CatalystChange) if c.old_value is not None]) == 0
    decisions = {row.claim_id: (row.decision, row.reasons) for row in world.rows(ClaimDecision)}
    assert decisions[moved] == ("review", ["the date would move more than 6 months"])


def test_a_widening_of_up_to_six_months_is_written(world: World) -> None:
    world.claim("fourth quarter of 2027")
    _publish(world)
    world.claim("late 2027 or early 2028", published=OCT_16)
    assert _publish(world)["catalysts_updated"] == 1


def test_the_same_window_in_other_words_writes_nothing(world: World) -> None:
    world.claim("second half of 2027")
    _publish(world)
    world.claim("2H 2027", published=OCT_16, sentence=_sentence("2H 2027"))
    summary = _publish(world)
    assert (summary["catalysts_updated"], summary["changes_written"]) == (0, 0)


def test_another_drug_or_type_is_another_catalyst(world: World) -> None:
    world.claim("second half of 2027")
    world.claim(
        "second half of 2027",
        catalyst_type="enrollment_completion",
        sentence="Enrollment in the Phase 3 RAINIER trial should complete in the second half of 2027.",
    )
    assert _publish(world)["catalysts_created"] == 2


@pytest.mark.parametrize(
    ("claim", "live", "expected"),
    [
        ((SourceTier.REGULATOR_OR_SEC, OCT_14), (SourceTier.COMPANY_SITE, OCT_16), True),
        ((SourceTier.WIRE, OCT_16), (SourceTier.COMPANY_SITE, OCT_14), False),
        ((SourceTier.COMPANY_SITE, OCT_16), (SourceTier.COMPANY_SITE, OCT_14), True),
        ((SourceTier.COMPANY_SITE, OCT_14), (SourceTier.COMPANY_SITE, OCT_16), False),
    ],
    ids=["higher tier, older", "lower tier, newer", "same tier, newer", "same tier, older"],
)
def test_the_highest_ranked_source_wins_then_the_most_recent(
    claim: tuple[SourceTier, datetime], live: tuple[SourceTier, datetime], expected: bool
) -> None:
    """Spec, Which claim wins: source rank first; among equals, the most recently published."""
    assert ranks_above(claim, live) is expected


def test_a_claim_whose_values_make_no_valid_catalyst_waits(world: World) -> None:
    claim = world.claim("second half of 2027")
    table = TABLES[Claim]
    with world.factory.begin() as session:
        value = session.execute(select(table.c.value).where(table.c.id == claim)).scalar_one()
        session.execute(update(table).where(table.c.id == claim).values(value=value | {"title": "x" * 301}))
    summary = _publish(world)
    assert (summary["held_by_writer"], summary["catalysts_created"]) == (1, 0)
    assert list(summary["top_reasons"]) == ["the claim's values do not make a valid catalyst"]
    assert world.counts() == (0, 0, 0, 0)
