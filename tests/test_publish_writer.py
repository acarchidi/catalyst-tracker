"""The catalyst writer on a disposable database with a local snapshot store (ADR 0018, part 5b).

Each test stores real-shaped rows: a release, its company link, an extractor claim, the
resolver's matches, stored check rows, and the stored decision. The writer must check again
before it writes, and what it writes must be what the read API shows (ADR 0025).
"""

import threading
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
from catalyst.publish.sanity import PASSED, ROLE_UNCLEAR, TIED, check_safe_to_write, drug_role
from catalyst.publish.write import LOCK, publish_claims, ranks_above
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
    DocumentReading,
    Drug,
    DrugRole,
    EntityMatch,
    GateDecision,
    MatchedEntity,
    MatchStatus,
    SourceTier,
    Trial,
    TrialPhase,
)
from catalyst.snapshots import LocalSnapshotStore
from catalyst.verify.rules import CheckResult
from catalyst.verify.second import VERIFIER_PROMPT, VERIFIER_ROLE

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
OCT_14 = datetime(2026, 10, 14, 20, tzinfo=UTC)
OCT_15 = datetime(2026, 10, 15, 20, tzinfo=UTC)
OCT_16 = datetime(2026, 10, 16, 20, tzinfo=UTC)
VERIFIER_MODEL = "claude-sonnet-5-5"


def _sentence(date_text: str, drug: str = "pegozafermin") -> str:
    return f"Topline data from the Phase 3 RAINIER trial of {drug} are expected in the {date_text}."


class World:
    """One company with one drug and one registered trial, and helpers to add releases and claims."""

    def __init__(self, factory: sessionmaker[Session], store: LocalSnapshotStore) -> None:
        self.factory, self.store = factory, store
        self.company = self.add(Company(cik=1, name="Acme Bio", sic_code="2834"))
        self.drug = self.add(Drug(company_id=self.company, preferred_name="pegozafermin"))
        self.trial = self.add(Trial(nct_id="NCT04929483", study_name="RAINIER", phase=TrialPhase.PHASE_3))
        self.other_trial = self.add(
            Trial(nct_id="NCT05000001", study_name="SYMMETRY", phase=TrialPhase.PHASE_3)
        )
        self._releases = 0
        self._last: tuple[UUID, str, datetime] = (UUID(int=0), "", OCT_14)

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
        trial: UUID | bool = True,
        catalyst_type: str = "topline_readout",
        decision: GateDecision = GateDecision.PUBLISH,
        stored: dict[str, CheckOutcome] | None = None,
        sentence: str | None = None,
        indication: str | None = "MASH",
        date_confidence: str = "company_confirmed",
        same_release: bool = False,
        read: bool = True,
    ) -> UUID:
        """Store a release stating one catalyst, its claim, matches, passing check rows, and decision.

        `same_release` adds the claim to the last release instead (its sentence must be in it).
        `read` records the verifier's current reading of the release.
        """
        sentence = sentence or _sentence(date_text, drug or "the program")
        if same_release:
            document_id, text, published = self._last
        else:
            self._releases += 1
            n = self._releases
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
            document_id, text = document.id, document_text(content, "text/html")
            self._last = (document_id, text, published)
            if read:
                self.add(
                    DocumentReading(
                        source_document_id=document_id,
                        agent=VERIFIER_ROLE,
                        prompt_id=VERIFIER_PROMPT[0],
                        prompt_version=VERIFIER_PROMPT[1],
                        model=VERIFIER_MODEL,
                        catalysts=1,
                        read_at=published,
                    )
                )
        window = parse_window(date_text, published.astimezone(NEW_YORK).date())
        assert window is not None
        value: dict[str, Any] = {
            "catalyst_type": catalyst_type,
            "title": f"Phase 3 RAINIER topline data for {drug or 'the program'} in MASH",
            "drug": drug,
            "indication": indication,
            "date_text": date_text,
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            "window_precision": window.precision.value,
            "date_confidence": date_confidence,
            "problems": [],
            "text_version": TEXT_VERSION,
        }
        start = text.index(sentence)
        claim = Claim(
            source_document_id=document_id,
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
            trial_id = self.trial if trial is True else trial
            name = "SYMMETRY" if trial_id == self.other_trial else "RAINIER"
            self.match(claim.id, MatchedEntity.TRIAL, name, trial_id)
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


def _check(world: World, claim: UUID, name: str) -> tuple[str, str | None]:
    """Return one stored check's outcome and reason."""
    (row,) = [r for r in world.rows(ClaimCheck) if r.claim_id == claim and r.check_name == name]
    return row.outcome, row.reason


def _publish(world: World, at: datetime = NOW) -> dict[str, Any]:
    return publish_claims(world.factory, world.store, lambda: at, verifier_model=VERIFIER_MODEL).summary()


# --- A publishable claim becomes a catalyst the read API shows ---


def test_a_publishable_claim_becomes_a_catalyst_the_read_api_shows_with_every_source(world: World) -> None:
    claim = world.claim("second half of 2027")
    summary = _publish(world)
    assert (summary["catalysts_created"], summary["changes_written"]) == (1, 10)
    assert (summary["drug_links_written"], summary["trial_links_written"]) == (1, 1)
    (row,) = world.rows(Catalyst)
    shown = world.shown(row.id)
    assert shown.title == "Topline data: pegozafermin, RAINIER trial"  # composed in code (part 5d)
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
        "trials",  # the sentence names RAINIER, now shown on the catalyst (ADR 0038)
    }
    assert [(t.id, t.nct_id, t.study_name) for t in shown.trials] == [(world.trial, "NCT04929483", "RAINIER")]
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
    # The fresh outcome is stored, so the next decision holds the claim with the reason.
    reason = "the window ended on 2026-12-31, before 2027-01-05"
    assert _check(world, claim, "window_not_past") == ("fail", reason)
    checks = world.rows(ClaimCheck)
    assert _publish(world, later + timedelta(hours=1))["checks_updated"] == 0
    assert world.rows(ClaimCheck) == checks  # a re-run writes nothing
    decide_claims(world.factory, world.store, lambda: later)
    (decision,) = world.rows(ClaimDecision)
    assert (decision.decision, decision.reasons) == ("review", [f"window_not_past: {reason}"])


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
    """A check added to the gate's list is read as stored until the writer recomputes it."""
    claim = world.claim("second half of 2027")
    monkeypatch.setattr(gate, "REQUIRED_CHECKS", (*gate.REQUIRED_CHECKS, "a_future_check"))
    assert _publish(world)["top_reasons"] == {"a_future_check has not run": 1}
    world.add(
        ClaimCheck(claim_id=claim, check_name="a_future_check", outcome=CheckOutcome.PASS, checked_at=NOW)
    )
    decide_claims(world.factory, world.store, lambda: NOW)  # the decision job sees the new row first
    assert _publish(world)["catalysts_created"] == 1


def test_the_company_check_is_read_as_stored(world: World) -> None:
    """`company_named_in_source` is its own job (part 3d), not a rule, so its stored row counts."""
    world.claim("second half of 2027", stored={"company_named_in_source": CheckOutcome.FAIL})
    unchecked = world.claim("second half of 2027", published=OCT_16)
    check = TABLES[ClaimCheck]
    with world.factory.begin() as session:
        session.execute(
            delete(check).where(
                check.c.claim_id == unchecked, check.c.check_name == "company_named_in_source"
            )
        )
    assert _publish(world)["top_reasons"] == {
        "company_named_in_source failed": 1,
        "company_named_in_source has not run": 1,
    }
    assert world.counts() == (0, 0, 0, 0)


def test_the_drug_check_is_recomputed(world: World) -> None:
    """Part 3e: a stored pass does not count when the quote and the text before it name no such drug.

    Its outcome rests on rows that change after it is stored (drug records, the release's other
    claims), and the writer already reads the stored copy, so it checks again.
    """
    sentence = _sentence("second half of 2027", "efruxifermin")
    claim = world.claim("second half of 2027", sentence=sentence)
    assert _publish(world)["top_reasons"] == {"drug_named_near_quote failed": 1}
    assert world.counts() == (0, 0, 0, 0)
    assert _check(world, claim, "drug_named_near_quote") == (
        "fail",
        "the quote and the text just before it do not name pegozafermin",
    )


def test_a_stored_drug_check_failure_is_not_trusted_either_way(world: World) -> None:
    world.claim("second half of 2027", stored={"drug_named_near_quote": CheckOutcome.FAIL})
    assert _publish(world)["catalysts_created"] == 1


def test_the_trial_named_in_quote_rule_is_recomputed(world: World) -> None:
    """A rule (part 3d): a stored pass does not count when the quote no longer names the trial."""
    sentence = "Topline data from the Phase 3 trial of pegozafermin are expected in the second half of 2027."
    world.claim("second half of 2027", sentence=sentence)
    assert _publish(world)["top_reasons"] == {"trial_named_in_quote failed": 1}
    assert world.counts() == (0, 0, 0, 0)


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


def _publish_in_order(world: World, *first: UUID, at: datetime = NOW) -> dict[str, Any]:
    """Publish only the `first` claims, by holding every other claim for one run."""
    decision = TABLES[ClaimDecision]
    with world.factory.begin() as session:
        held = list(
            session.execute(
                select(decision.c.claim_id).where(
                    decision.c.claim_id.not_in(first), decision.c.decision == "publish"
                )
            ).scalars()
        )
        session.execute(
            update(decision).where(decision.c.claim_id.in_(held)).values(decision="review", reasons=["later"])
        )
    summary = _publish(world, at)
    with world.factory.begin() as session:
        session.execute(
            update(decision).where(decision.c.claim_id.in_(held)).values(decision="publish", reasons=[])
        )
    return summary


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
    # Stored as the writer's own check, so the decision job holds it too, and does not flip it back.
    assert _check(world, moved, "safe_to_write") == ("fail", "the date would move more than 6 months")
    for _ in range(2):
        decide_claims(world.factory, world.store, lambda: NOW)
        decisions = {row.claim_id: (row.decision, row.reasons) for row in world.rows(ClaimDecision)}
        assert decisions[moved] == ("review", ["safe_to_write: the date would move more than 6 months"])


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
        sentence=(
            "Enrollment in the Phase 3 RAINIER trial of pegozafermin "
            "should complete in the second half of 2027."
        ),
    )
    assert _publish(world)["catalysts_created"] == 2


@pytest.mark.parametrize(
    ("claim", "live", "expected"),
    [
        ((SourceTier.REGULATOR_OR_SEC, OCT_14), (SourceTier.COMPANY_SITE, OCT_16), True),
        ((SourceTier.WIRE, OCT_16), (SourceTier.COMPANY_SITE, OCT_14), False),
        ((SourceTier.COMPANY_SITE, OCT_16), (SourceTier.COMPANY_SITE, OCT_14), True),
        ((SourceTier.COMPANY_SITE, OCT_14), (SourceTier.COMPANY_SITE, OCT_16), False),
        ((SourceTier.COMPANY_SITE, OCT_14), (SourceTier.COMPANY_SITE, OCT_14), False),
    ],
    ids=["higher tier, older", "lower tier, newer", "same tier, newer", "same tier, older", "a tie"],
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
        # Once a 301-character title; titles are now composed in code (part 5d), so a value no rule
        # reads stands in for one that makes no valid catalyst.
        session.execute(
            update(table).where(table.c.id == claim).values(value=value | {"date_confidence": "a guess"})
        )
    summary = _publish(world)
    assert (summary["held_by_writer"], summary["catalysts_created"]) == (1, 0)
    assert list(summary["top_reasons"]) == ["the claim's values do not make a valid catalyst"]
    assert world.counts() == (0, 0, 0, 0)


# --- Opus review of PR #59: blocking findings ---


def _window(world: World) -> tuple[str, str, str]:
    (row,) = world.rows(Catalyst)
    return row.window_start.isoformat(), row.window_end.isoformat(), row.window_raw_text


def test_b1_the_window_source_is_the_claim_behind_any_date_field(world: World) -> None:
    """A move that changes only the end writes no `window_start` row; its claim is still the source."""
    a = world.claim("second half of 2027", tier=SourceTier.COMPANY_SITE)
    b = world.claim("July 2027", published=OCT_15)
    world.claim("third quarter of 2027", published=OCT_16, tier=SourceTier.COMPANY_SITE)
    _publish_in_order(world, a)
    assert _publish_in_order(world, a, b)["catalysts_updated"] == 1
    assert {c.field for c in world.rows(CatalystChange) if c.claim_id == b} == {
        "window_end",
        "window_precision",
        "window_raw_text",
    }
    summary = _publish(world)  # C, from the company site, does not outrank B's SEC filing
    assert (summary["catalysts_updated"], summary["not_written_lower_rank"]) == (0, 1)
    assert _window(world) == ("2027-07-01", "2027-07-31", "July 2027")


def test_b2_a_window_that_has_ended_never_replaces_one_that_has_not(world: World) -> None:
    live = world.claim("second half of 2026", published=datetime(2026, 7, 1, 20, tzinfo=UTC))
    late = world.claim("third quarter of 2026", published=datetime(2026, 8, 10, 20, tzinfo=UTC))
    _publish_in_order(world, live)
    summary = _publish(world)
    assert (summary["held_by_writer"], summary["catalysts_updated"]) == (1, 0)
    assert list(summary["top_reasons"]) == ["the new window has already passed"]
    assert _window(world) == ("2026-07-01", "2026-12-31", "second half of 2026")  # still on the calendar
    check_safe_to_write(world.factory, lambda: NOW)
    decide_claims(world.factory, world.store, lambda: NOW)
    decisions = {row.claim_id: (row.decision, row.reasons) for row in world.rows(ClaimDecision)}
    assert decisions[late] == ("review", ["safe_to_write: the new window has already passed"])


def test_b3_another_indication_for_the_same_drug_waits_instead_of_moving_the_date(world: World) -> None:
    world.claim("second half of 2027")
    _publish(world)
    shtg = world.claim(
        "fourth quarter of 2027",
        published=OCT_16,
        trial=False,
        indication="SHTG",
        sentence="Phase 2 data for pegozafermin in SHTG are expected in the fourth quarter of 2027.",
    )
    summary = _publish(world)
    assert (summary["held_by_writer"], summary["catalysts_updated"]) == (1, 0)
    assert _check(world, shtg, "safe_to_write") == ("fail", "the indication differs from the published one")
    assert _window(world)[2] == "second half of 2027"


def test_b3_two_indications_in_one_release_are_never_merged_or_dropped(world: World) -> None:
    sentence = (
        "Phase 3 RAINIER data for pegozafermin in MASH and SHTG are expected in the second half of 2027."
    )
    world.claim("second half of 2027", sentence=sentence)
    world.claim("second half of 2027", sentence=sentence, indication="SHTG", same_release=True)
    summary = _publish(world)
    assert (summary["catalysts_created"], summary["held_by_writer"], summary["not_written_lower_rank"]) == (
        1,
        1,
        0,
    )
    assert list(summary["top_reasons"]) == ["the indication differs from the published one"]


def test_b3_the_indication_is_compared_without_case_or_punctuation(world: World) -> None:
    world.claim("second half of 2027", indication="NASH / MASH")
    _publish(world)
    world.claim("fourth quarter of 2027", published=OCT_16, indication="nash mash")
    assert _publish(world)["catalysts_updated"] == 1


def test_b4_a_release_that_names_no_trial_is_the_same_event(world: World) -> None:
    world.claim("second half of 2027")
    _publish(world)
    world.claim(
        "fourth quarter of 2027",
        published=OCT_16,
        trial=False,
        sentence="Topline data for pegozafermin in MASH are expected in the fourth quarter of 2027.",
    )
    summary = _publish(world)
    assert (summary["catalysts_created"], summary["catalysts_updated"]) == (0, 1)
    assert _window(world)[2] == "fourth quarter of 2027"


def test_b4_a_release_that_names_no_trial_waits_when_two_trials_are_published(world: World) -> None:
    world.claim("second half of 2027")
    world.claim(
        "second half of 2027",
        trial=world.other_trial,
        indication="SHTG",  # one fits by indication, but two fit by trial: still a person's call
        sentence=(
            "Topline data from the Phase 3 SYMMETRY trial of pegozafermin "
            "are expected in the second half of 2027."
        ),
    )
    assert _publish(world)["catalysts_created"] == 2  # two trials, two events
    world.claim(
        "fourth quarter of 2027",
        published=OCT_16,
        trial=False,
        sentence="Topline data for pegozafermin in MASH are expected in the fourth quarter of 2027.",
    )
    summary = _publish(world)
    assert (summary["catalysts_created"], summary["held_by_writer"]) == (0, 1)
    assert list(summary["top_reasons"]) == ["the claim matches more than one published catalyst"]


def test_a_claim_matching_two_identical_published_catalysts_waits(world: World) -> None:
    """Two open catalysts with one event (only a hand edit makes them) leave the choice to a person."""
    world.claim("second half of 2027")
    _publish(world)
    (original,) = world.rows(Catalyst)
    copy_id = UUID(int=7)
    with world.factory.begin() as session:
        session.execute(insert(TABLES[Catalyst]).values(**(original._asdict() | {"id": copy_id})))
        for model in (CatalystChange, CatalystDrug, CatalystTrial):
            for row in world.rows(model):
                fresh = row._asdict() | {
                    "id": UUID(int=hash((model.__name__, row.id)) % 2**64),
                    "catalyst_id": copy_id,
                }
                session.execute(insert(TABLES[model]).values(**fresh))
    world.claim("fourth quarter of 2027", published=OCT_16)
    summary = _publish(world)
    assert (summary["held_by_writer"], summary["catalysts_updated"]) == (1, 0)
    assert list(summary["top_reasons"]) == ["the claim matches more than one published catalyst"]


@pytest.mark.parametrize(
    ("quote", "drug", "expected"),
    [
        ("Pegozafermin data are expected in 2H27.", "pegozafermin", DrugRole.ALONE),
        ("Pegozafermin monotherapy data are expected in 2H27.", "pegozafermin", DrugRole.ALONE),
        (
            "Data for pegozafermin plus pembrolizumab are expected in 2H27.",
            "pegozafermin",
            DrugRole.IN_COMBINATION,
        ),
        (
            "Data for pembrolizumab plus pegozafermin are expected in 2H27.",
            "pegozafermin",
            DrugRole.IN_COMBINATION,
        ),
        ("Data for X + pegozafermin are expected in 2H27.", "pegozafermin", DrugRole.IN_COMBINATION),
        (
            "Pegozafermin in combination with semaglutide reads out in 2H27.",
            "pegozafermin",
            DrugRole.IN_COMBINATION,
        ),
        ("SPY001 alone and in pairwise combinations reads out in 2027.", "SPY001", None),
        ("A combination study is ongoing; pegozafermin data come in 2H27.", "pegozafermin", None),
        (
            "Pegozafermin added to standard of care reads out in 2H27.",
            "pegozafermin",
            DrugRole.IN_COMBINATION,
        ),
        (
            "Data for pegozafermin co-administered with statins come in 2H27.",
            "pegozafermin",
            DrugRole.IN_COMBINATION,
        ),
        ("Data with drugs combined in one arm come in 2H27 for pegozafermin.", "pegozafermin", None),
    ],
)
def test_b5_the_drug_role_is_read_from_the_quote_or_left_to_a_person(
    quote: str, drug: str, expected: DrugRole | None
) -> None:
    """`alone` means the quote names no combination; a combination must join the drug itself."""
    assert drug_role(quote, drug, frozenset()) is expected


def test_b5_a_quote_whose_drug_role_is_unclear_waits(world: World) -> None:
    sentence = (
        "Topline data from the Phase 3 RAINIER trial of pegozafermin alone and in combination "
        "are expected in the second half of 2027."
    )
    claim = world.claim("second half of 2027", sentence=sentence)
    summary = _publish(world)
    assert (summary["held_by_writer"], summary["catalysts_created"]) == (1, 0)
    assert _check(world, claim, "safe_to_write") == ("fail", ROLE_UNCLEAR)


def test_b5_a_drug_joined_by_plus_is_linked_in_combination(world: World) -> None:
    sentence = (
        "Topline data from the Phase 3 RAINIER trial of pegozafermin plus semaglutide "
        "are expected in the second half of 2027."
    )
    world.claim("second half of 2027", sentence=sentence)
    _publish(world)
    (link,) = world.rows(CatalystDrug)
    assert link.role == "in_combination"


# --- Opus review of PR #59: optional findings ---


def test_o2_a_second_reading_counts_only_with_a_current_reading(world: World) -> None:
    world.claim("second half of 2027", read=False)
    assert _publish(world)["top_reasons"] == {"second_reading_agrees has not run": 1}
    assert world.counts() == (0, 0, 0, 0)


def test_o3_the_check_job_stores_safe_to_write_for_every_claim(world: World) -> None:
    first = world.claim("second half of 2027")
    _publish(world)
    later = world.claim("first half of 2028", published=OCT_16)
    new = world.claim(
        "second half of 2027",
        catalyst_type="enrollment_completion",
        sentence=(
            "Enrollment in the Phase 3 RAINIER trial of pegozafermin "
            "should complete in the second half of 2027."
        ),
    )
    report = check_safe_to_write(world.factory, lambda: NOW)
    assert report.summary() == {"claims": 3, "outcomes": {"safe_to_write:pass": 2, "safe_to_write:fail": 1}}
    assert _check(world, first, "safe_to_write") == ("pass", None)
    assert _check(world, new, "safe_to_write") == ("pass", None)
    assert _check(world, later, "safe_to_write") == ("fail", "the window does not overlap the published one")


def test_o5_a_better_source_with_the_same_dates_updates_the_confidence(world: World) -> None:
    first = world.claim("second half of 2027")
    world.claim("second half of 2027", published=OCT_16, date_confidence="regulator_scheduled")
    _publish_in_order(world, first)
    summary = _publish(world)
    assert (summary["catalysts_updated"], summary["changes_written"]) == (1, 1)
    (row,) = world.rows(Catalyst)
    assert row.date_confidence == "regulator_scheduled"
    world.shown(row.id)  # the read API still shows it, every value with its source


# --- Mutation gaps ---


def test_a_narrowing_may_move_the_start_more_than_six_months(world: World) -> None:
    world.claim("2027")
    _publish(world)
    world.claim("fourth quarter of 2027", published=OCT_16)
    assert _publish(world)["catalysts_updated"] == 1


def test_a_start_moved_more_than_six_months_earlier_waits(world: World) -> None:
    world.claim("October 2027")
    _publish(world)
    world.claim(
        "by year-end 2027",
        published=OCT_16,
        sentence="Topline data from the Phase 3 RAINIER trial of pegozafermin are expected by year-end 2027.",
    )
    summary = _publish(world)
    assert (summary["held_by_writer"], list(summary["top_reasons"])) == (
        1,
        ["the date would move more than 6 months"],
    )


def test_a_losing_first_seen_claim_whose_window_does_not_overlap_waits(world: World) -> None:
    world.claim("second half of 2027", published=OCT_16)
    world.claim("first half of 2028")
    summary = _publish(world)
    assert (summary["catalysts_created"], summary["held_by_writer"]) == (1, 1)
    assert list(summary["top_reasons"]) == ["the window does not overlap the published one"]


def test_one_run_moves_a_catalyst_once_and_compares_the_rest_with_the_move(world: World) -> None:
    first = world.claim("second half of 2027")
    _publish_in_order(world, first)
    world.claim("fourth quarter of 2027", published=OCT_16)
    world.claim("third quarter of 2027", published=OCT_15)
    summary = _publish(world)
    assert summary["catalysts_updated"] == 1
    assert _window(world)[2] == "fourth quarter of 2027"
    assert list(summary["top_reasons"]) == ["the window does not overlap the published one"]


def test_two_writers_never_run_at_once(world: World) -> None:
    """The advisory lock: a second run waits until the first one's transaction ends."""
    world.claim("second half of 2027")
    found: dict[str, Any] = {}
    with world.factory() as holder:
        holder.execute(select(func.pg_advisory_xact_lock(LOCK)))
        runner = threading.Thread(target=lambda: found.update(_publish(world)))
        runner.start()
        runner.join(timeout=1.5)
        assert runner.is_alive()  # waiting for the lock
        assert world.counts() == (0, 0, 0, 0)
        holder.commit()
    runner.join(timeout=30)
    assert not runner.is_alive()
    assert found["catalysts_created"] == 1


# --- Opus review of PR #59, round 2 ---

# Other words than A's, so B is a second source for A, not a copy of it.
SEC_2H27 = "The Phase 3 RAINIER readout for pegozafermin remains on track for the second half of 2027."


def _q4_with_wire_support(world: World, published: datetime = OCT_16) -> UUID:
    """Store a company-site Q4 2027 claim, and the wire copy that gives it a second source."""
    claim = world.claim("fourth quarter of 2027", published=published, tier=SourceTier.COMPANY_SITE)
    wire = "Pegozafermin RAINIER topline data are now due in the fourth quarter of 2027."
    world.claim("fourth quarter of 2027", published=published, tier=SourceTier.WIRE, sentence=wire)
    return claim


def test_n1_a_better_source_confirming_the_dates_protects_them_in_the_same_run(world: World) -> None:
    a = world.claim("second half of 2027", tier=SourceTier.COMPANY_SITE)
    world.claim("second half of 2027", published=OCT_15)  # B: an SEC filing, the same window
    _q4_with_wire_support(world)
    _publish_in_order(world, a)
    summary = _publish(world)
    assert (summary["catalysts_updated"], summary["not_written_same_dates"]) == (0, 1)
    assert _window(world)[2] == "second half of 2027"


def test_n1_the_confirmation_still_protects_the_dates_in_later_runs(world: World) -> None:
    a = world.claim("second half of 2027", tier=SourceTier.COMPANY_SITE)
    b = world.claim("second half of 2027", published=OCT_15, sentence=SEC_2H27)
    _publish_in_order(world, a)
    assert _publish_in_order(world, a, b)["not_written_same_dates"] == 1  # nothing to write, nothing cited
    _q4_with_wire_support(world)
    assert _publish(world)["catalysts_updated"] == 0
    assert _window(world)[2] == "second half of 2027"


def test_n1_a_confidence_change_makes_its_claim_the_window_source_in_the_same_run(world: World) -> None:
    a = world.claim("second half of 2027", tier=SourceTier.COMPANY_SITE)
    world.claim("second half of 2027", published=OCT_15, date_confidence="regulator_scheduled")
    _q4_with_wire_support(world)
    _publish_in_order(world, a)
    assert _publish(world)["catalysts_updated"] == 1  # the confidence only
    assert _window(world)[2] == "second half of 2027"


def test_n1_a_confidence_change_makes_its_claim_the_window_source_in_later_runs(world: World) -> None:
    a = world.claim("second half of 2027", tier=SourceTier.COMPANY_SITE)
    b = world.claim(
        "second half of 2027", published=OCT_15, date_confidence="regulator_scheduled", sentence=SEC_2H27
    )
    _publish_in_order(world, a)
    _publish_in_order(world, a, b)
    assert {c.field for c in world.rows(CatalystChange) if c.claim_id == b} == {"date_confidence"}
    _q4_with_wire_support(world)
    assert _publish(world)["catalysts_updated"] == 0
    assert _window(world)[2] == "second half of 2027"


def test_n1_the_window_source_is_updated_after_an_in_run_move(world: World) -> None:
    a = world.claim("second half of 2027", tier=SourceTier.COMPANY_SITE)
    world.claim("fourth quarter of 2027", published=OCT_15)  # B: SEC, narrows the window
    world.claim("October 2027", published=OCT_16, tier=SourceTier.COMPANY_SITE)  # C: loses to B
    wire = "Pegozafermin RAINIER topline data are now due in October 2027."
    world.claim("October 2027", published=OCT_16, tier=SourceTier.WIRE, sentence=wire)
    _publish_in_order(world, a)
    assert _publish(world)["catalysts_updated"] == 1
    assert _window(world)[2] == "fourth quarter of 2027"


def test_n2_a_trial_written_into_a_catalyst_with_none_is_linked_and_keeps_other_trials_out(
    world: World,
) -> None:
    program = world.claim(
        "2027", trial=False, sentence="Topline data for pegozafermin in MASH are expected in 2027."
    )
    rainier = world.claim("second half of 2027", published=OCT_15)
    symmetry = world.claim(
        "fourth quarter of 2027",
        published=OCT_16,
        trial=world.other_trial,
        sentence=(
            "Topline data from the Phase 3 SYMMETRY trial of pegozafermin "
            "are expected in the fourth quarter of 2027."
        ),
    )
    _publish_in_order(world, program)
    assert _publish_in_order(world, program, rainier)["catalysts_updated"] == 1
    (link,) = world.rows(CatalystTrial)
    assert (link.trial_id, link.claim_id) == (world.trial, rainier)
    summary = _publish(world)
    assert (summary["catalysts_created"], summary["catalysts_updated"]) == (1, 0)
    by_trial = {r.trial_id: r.catalyst_id for r in world.rows(CatalystTrial)}
    windows = {r.id: r.window_raw_text for r in world.rows(Catalyst)}
    assert windows[by_trial[world.trial]] == "second half of 2027"
    assert windows[by_trial[world.other_trial]] == "fourth quarter of 2027"
    assert {r.claim_id for r in world.rows(CatalystTrial)} == {rainier, symmetry}


def test_n2_in_one_run_the_first_trial_takes_the_catalyst_and_the_other_is_a_new_event(world: World) -> None:
    program = world.claim(
        "2027", trial=False, sentence="Topline data for pegozafermin in MASH are expected in 2027."
    )
    world.claim("second half of 2027", published=OCT_15)
    world.claim(
        "fourth quarter of 2027",
        published=OCT_16,
        trial=world.other_trial,
        sentence=(
            "Topline data from the Phase 3 SYMMETRY trial of pegozafermin "
            "are expected in the fourth quarter of 2027."
        ),
    )
    _publish_in_order(world, program)
    summary = _publish(world)
    assert (summary["catalysts_created"], summary["catalysts_updated"]) == (1, 1)
    assert len(world.rows(CatalystTrial)) == 2
    assert len({r.catalyst_id for r in world.rows(CatalystTrial)}) == 2  # never two trials on one


def test_a_claim_excused_as_an_update_never_becomes_a_new_catalyst_with_a_passed_date(world: World) -> None:
    """Claude review of PR #59: a claim's passed window is excused because it fits the live program catalyst.

    Then A, written first, links RAINIER to that catalyst, so B (SYMMETRY) no longer fits and would
    be created as a new, upcoming catalyst whose window has already ended.
    """
    program = world.claim(
        "2026", trial=False, sentence="Topline data for pegozafermin in MASH are expected in 2026."
    )
    late = world.claim(
        "third quarter of 2026",
        published=datetime(2026, 9, 1, 20, tzinfo=UTC),
        trial=world.other_trial,
        sentence=(
            "Topline data from the Phase 3 SYMMETRY trial of pegozafermin "
            "are expected in the third quarter of 2026."
        ),
    )
    world.claim("fourth quarter of 2026", published=OCT_16)
    _publish_in_order(world, program)
    summary = _publish(world)
    assert (summary["catalysts_created"], summary["catalysts_updated"]) == (0, 1)
    (row,) = world.rows(Catalyst)
    assert row.window_start.isoformat() == "2026-10-01"
    assert _check(world, late, "safe_to_write") == ("fail", PASSED)


@pytest.mark.parametrize(
    "quote",
    [
        "Data for pegozafermin with semaglutide are expected in 2H27.",
        "Data for pegozafermin with AB-123 are expected in 2H27.",
        "A pegozafermin-based doublet regimen reads out in 2H27.",
        "A pegozafermin triplet reads out in 2H27.",
        "Pegozafermin on a statin backbone reads out in 2H27.",
        "Pegozafermin in addition to standard care reads out in 2H27.",
        "Pegozafermin on top of statins reads out in 2H27.",
        "Data for pegozafermin plus semaglutide, and pegozafermin monotherapy, come in 2H27.",
    ],
)
def test_o1_other_combination_wording_leaves_the_role_to_a_person(quote: str) -> None:
    assert drug_role(quote, "pegozafermin", frozenset({"semaglutide"})) is None


def test_o1_with_and_a_word_that_is_no_drug_is_still_alone() -> None:
    assert (
        drug_role(
            "Pegozafermin data with 12-month follow-up come in 2H27.",
            "pegozafermin",
            frozenset({"semaglutide"}),
        )
        is DrugRole.ALONE
    )


def test_o1_with_another_drug_on_file_waits_in_the_writer(world: World) -> None:
    world.add(Drug(company_id=world.company, preferred_name="semaglutide"))
    sentence = (
        "Topline data from the Phase 3 RAINIER trial of pegozafermin with semaglutide "
        "are expected in the second half of 2027."
    )
    claim = world.claim("second half of 2027", sentence=sentence)
    assert _publish(world)["held_by_writer"] == 1
    assert _check(world, claim, "safe_to_write") == ("fail", ROLE_UNCLEAR)


def test_an_unclear_role_that_slips_past_the_hold_stops_the_run_and_writes_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guessed role must never be stored as if a quote said it (Claude review of PR #59)."""
    from catalyst.publish import write

    def always_safe(*_args: object) -> CheckResult:
        return CheckResult(gate.WRITER_CHECK, CheckOutcome.PASS)

    monkeypatch.setattr(write, "safe_to_write", always_safe)
    world.claim(
        "second half of 2027",
        trial=False,
        sentence="Topline data for pegozafermin with semaglutide are expected in the second half of 2027.",
    )
    world.add(Drug(company_id=world.company, preferred_name="semaglutide"))
    with pytest.raises(RuntimeError, match="the drug role is unclear"):
        _publish(world)
    assert world.counts() == (0, 0, 0, 0)


# --- CP4 review: titles and types from checked fields, and equal-rank claims (ADR 0018, part 5d) ---


def _set_value(world: World, claim: UUID, **changes: Any) -> None:
    """Change the stored claim's value, as a model's own words would set it."""
    table = TABLES[Claim]
    with world.factory.begin() as session:
        value = session.execute(select(table.c.value).where(table.c.id == claim)).scalar_one()
        session.execute(update(table).where(table.c.id == claim).values(value=value | changes))


@pytest.mark.parametrize(
    "written",
    [
        "FDA approves pegozafermin; Acme Bio shares set to double",
        "Suggested headline for news aggregators: FDA Approves Pegozafermin",
        "x" * 301,  # once a reason to hold the claim; the model's title is no longer published at all
    ],
)
def test_b1_the_title_is_composed_from_checked_fields_never_the_models_words(
    world: World, written: str
) -> None:
    claim = world.claim("second half of 2027")
    _set_value(world, claim, title=written)
    assert _publish(world)["catalysts_created"] == 1
    (row,) = world.rows(Catalyst)
    assert row.title == "Topline data: pegozafermin, RAINIER trial"
    shown = world.shown(row.id)  # the read API's check: the title matches its latest change row
    assert shown.title == row.title
    (title,) = [c for c in world.rows(CatalystChange) if c.field == "title"]
    assert (title.new_value, title.claim_id) == (row.title, claim)
    (source,) = shown.sources
    assert "title" in source.fields
    assert source.quote == _sentence("second half of 2027")


def test_b1_the_title_names_the_reference_tables_preferred_name(world: World) -> None:
    efx = world.add(Drug(company_id=world.company, preferred_name="efruxifermin"))
    sentence = _sentence("second half of 2027", "EFX")
    claim = world.claim("second half of 2027", drug="EFX", drug_id=efx, sentence=sentence)
    _set_value(world, claim, title="EFX wins")
    _publish(world)
    (row,) = world.rows(Catalyst)
    assert row.title == "Topline data: efruxifermin, RAINIER trial"


def test_b1_a_trial_without_a_study_name_is_named_by_its_registry_number(world: World) -> None:
    trial = world.add(Trial(nct_id="NCT06000002", phase=TrialPhase.PHASE_3))
    sentence = (
        "Topline data from the Phase 3 NCT06000002 trial of pegozafermin are expected in the "
        "second half of 2027."
    )
    world.claim("second half of 2027", trial=trial, sentence=sentence)
    _publish(world)
    (row,) = world.rows(Catalyst)
    assert row.title == "Topline data: pegozafermin, NCT06000002 trial"


def test_b1_with_no_drug_or_trial_the_title_is_the_type_label_alone(world: World) -> None:
    world.claim(
        "second half of 2027",
        drug=None,
        trial=False,
        sentence="Topline data are expected in the second half of 2027.",
    )
    _publish(world)
    (row,) = world.rows(Catalyst)
    assert row.title == "Topline data"


def test_b1_the_indication_still_comes_from_the_claim_behind_the_title_row(world: World) -> None:
    """The live index reads a catalyst's indication from its title row's claim (B3); it still does."""
    world.claim("second half of 2027")
    _publish(world)
    shtg = world.claim("fourth quarter of 2027", published=OCT_16, indication="SHTG")
    assert _publish(world)["held_by_writer"] == 1
    assert _check(world, shtg, "safe_to_write") == ("fail", "the indication differs from the published one")


def test_o1_a_topline_sentence_typed_as_an_fda_decision_date_is_held(world: World) -> None:
    claim = world.claim("second half of 2027", catalyst_type="pdufa_date")
    summary = _publish(world)
    assert summary["catalysts_created"] == 0
    assert summary["top_reasons"] == {"type_named_near_quote failed": 1}
    assert _check(world, claim, "type_named_near_quote") == (
        "fail",
        "the quote has words for topline data, not for an FDA decision date",
    )


BOTH_DATES = (
    "Topline data from the Phase 3 RAINIER trial of pegozafermin are expected in the second half of 2027, "
    "possibly as early as the third quarter of 2027."
)


def test_o2_equal_rank_claims_about_one_event_with_other_dates_both_wait(world: World) -> None:
    a = world.claim("second half of 2027", sentence=BOTH_DATES)
    b = world.claim("third quarter of 2027", sentence=BOTH_DATES, same_release=True)
    summary = _publish(world)
    assert (summary["catalysts_created"], summary["held_by_writer"], summary["not_written_lower_rank"]) == (
        0,
        2,
        0,
    )
    assert summary["top_reasons"] == {TIED: 2}
    assert _check(world, a, "safe_to_write") == ("fail", TIED)
    assert _check(world, b, "safe_to_write") == ("fail", TIED)
    check_safe_to_write(world.factory, lambda: NOW)  # the check job holds both too, so nothing flips back
    decide_claims(world.factory, world.store, lambda: NOW)
    decisions = {row.claim_id: (row.decision, row.reasons) for row in world.rows(ClaimDecision)}
    assert decisions[a] == decisions[b] == ("review", [f"safe_to_write: {TIED}"])


def test_o2_equal_rank_claims_with_the_same_dates_make_one_catalyst(world: World) -> None:
    sentence = (
        "Topline data from the Phase 3 RAINIER trial of pegozafermin are expected in the second half "
        "of 2027 (2H 2027)."
    )
    world.claim("second half of 2027", sentence=sentence)
    world.claim("2H 2027", sentence=sentence, same_release=True)
    summary = _publish(world)
    assert (summary["catalysts_created"], summary["held_by_writer"]) == (1, 0)


def test_o2_an_equal_rank_claim_with_other_dates_than_the_live_source_waits(world: World) -> None:
    world.claim("second half of 2027", sentence=BOTH_DATES)
    _publish(world)
    late = world.claim("third quarter of 2027", sentence=BOTH_DATES, same_release=True)
    summary = _publish(world)
    assert (summary["held_by_writer"], summary["catalysts_updated"], summary["not_written_lower_rank"]) == (
        1,
        0,
        0,
    )
    assert _check(world, late, "safe_to_write") == ("fail", TIED)
    assert _window(world)[2] == "second half of 2027"


def test_o2_a_claim_from_an_older_prompt_never_ties(world: World) -> None:
    """A superseded reading is held by the gate already; it must not hold the current one too."""
    old = world.claim("third quarter of 2027", sentence=BOTH_DATES, decision=GateDecision.REVIEW)
    table = TABLES[Claim]
    with world.factory.begin() as session:
        session.execute(update(table).where(table.c.id == old).values(prompt_version="0.9.0"))
    world.claim("second half of 2027", sentence=BOTH_DATES, same_release=True)
    assert _publish(world)["catalysts_created"] == 1


def test_b1_a_matched_drug_with_no_record_to_name_it_waits_and_no_title_is_guessed(world: World) -> None:
    world.claim("second half of 2027", drug_id=UUID(int=7))  # matched to a record that is not on file
    summary = _publish(world)
    assert (summary["held_by_writer"], summary["catalysts_created"]) == (1, 0)
    assert list(summary["top_reasons"]) == ["the claim's values do not make a valid catalyst"]
    assert world.counts() == (0, 0, 0, 0)
