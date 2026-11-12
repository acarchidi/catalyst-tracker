"""The writer's indication pass (ADR 0053, decision 2): plain code, from each catalyst's latest try.

A confirmed indication is written with a change row citing its claim, so the read API shows it
with its sentence; a later refusal wins over an earlier find; anything that no longer checks out
waits, and nothing is ever unset.
"""

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.api.read import published, read_session
from catalyst.db.tables import TABLES
from catalyst.extract.disease import CHECKER_PROMPT, READER_PROMPT
from catalyst.extract.indicate import CONFIRMED_CHECK, claim_field
from catalyst.extract.text import document_text, find_quote
from catalyst.publish.indication import NO_DISEASE, NOT_CONFIRMED, WORDS_NOT_IN_QUOTE, publish_indications
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CheckOutcome,
    Claim,
    ClaimCheck,
    ClaimSubject,
    DrugRole,
    Indication,
    IndicationAttempt,
    IndicationResult,
    SourceDocument,
)
from catalyst.snapshots import LocalSnapshotStore
from extract.test_indicate import NOW, RELEASE, TARGET, FakeReaders, all_rows, run_readers, seed_catalyst


def _publish(factory: sessionmaker[Session], store: LocalSnapshotStore) -> dict[str, Any]:
    return publish_indications(factory, store, lambda: NOW).summary()


def _detail(factory: sessionmaker[Session], catalyst_id: Any) -> Any:
    with read_session(factory) as session:
        return next(d for d in published(session) if d.id == catalyst_id)


def test_a_confirmed_indication_is_written_and_shown_in_its_sources_own_words(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    catalyst_id = seed_catalyst(session_factory, store)
    run_readers(session_factory, store, FakeReaders())
    assert _publish(session_factory, store) == {"indications_written": 1, "indications_held": {}}
    detail = _detail(session_factory, catalyst_id)
    assert detail.indication is not None
    assert detail.indication.name == "melanoma"  # the sentence's word, not the MeSH name "Melanoma"
    (source,) = [s for s in detail.sources if "indication_id" in s.fields]
    assert source.quote == TARGET
    changes = [c for c in all_rows(session_factory, CatalystChange) if c.field == "indication_id"]
    assert [c.new_value for c in changes] == [str(detail.indication.id)]


def test_a_second_pass_writes_nothing_more(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    store = LocalSnapshotStore(tmp_path)
    seed_catalyst(session_factory, store)
    run_readers(session_factory, store, FakeReaders())
    _publish(session_factory, store)
    assert _publish(session_factory, store) == {"indications_written": 0, "indications_held": {}}


def test_a_later_refusal_wins_over_an_earlier_find(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    catalyst_id = seed_catalyst(session_factory, store)
    run_readers(session_factory, store, FakeReaders())
    (found,) = all_rows(session_factory, IndicationAttempt)
    later = IndicationAttempt(
        catalyst_id=catalyst_id,
        claim_id=found.claim_id,
        outcome=IndicationResult.REFUSED,
        reason="the second reader did not confirm the sentence states the disease",
        reader_prompt_version=READER_PROMPT[1],
        checker_prompt_version="9.9.9",
        reader_model=found.reader_model,
        checker_model=found.checker_model,
        attempted_at=NOW + timedelta(days=1),
    )
    with session_factory.begin() as session:
        session.execute(insert(TABLES[IndicationAttempt]).values(**later.model_dump()))
    assert _publish(session_factory, store)["indications_written"] == 0
    (row,) = [c for c in all_rows(session_factory, Catalyst) if c.id == catalyst_id]
    assert row.indication_id is None


def test_without_the_second_readers_pass_the_indication_waits(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    seed_catalyst(session_factory, store)
    run_readers(session_factory, store, FakeReaders())
    with session_factory.begin() as session:
        session.execute(delete(TABLES[ClaimCheck]).where(TABLES[ClaimCheck].c.check_name == CONFIRMED_CHECK))
    assert _publish(session_factory, store) == {
        "indications_written": 0,
        "indications_held": {NOT_CONFIRMED: 1},
    }


def test_an_unreadable_copy_holds_the_indication(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    seed_catalyst(session_factory, store)
    run_readers(session_factory, store, FakeReaders())
    with session_factory() as session:
        key = session.execute(select(TABLES[SourceDocument].c.snapshot_key)).scalar_one()
    (tmp_path / key).unlink()
    summary = _publish(session_factory, store)
    assert summary["indications_written"] == 0
    assert summary["indications_held"] == {"the stored copy is missing": 1}


def test_two_catalysts_with_one_disease_share_one_indication_row(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    seed_catalyst(session_factory, store, n=1)
    seed_catalyst(session_factory, store, n=2)
    run_readers(session_factory, store, FakeReaders())
    assert _publish(session_factory, store)["indications_written"] == 2
    assert len(all_rows(session_factory, Indication)) == 1


def test_an_attempt_pointing_at_another_catalysts_claim_waits(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    first = seed_catalyst(session_factory, store, n=1)
    second = seed_catalyst(session_factory, store, n=2, role=DrugRole.IN_COMBINATION)
    run_readers(session_factory, store, FakeReaders())
    (found,) = [a for a in all_rows(session_factory, IndicationAttempt) if a.catalyst_id == first]
    stolen = IndicationAttempt(
        catalyst_id=second,
        claim_id=found.claim_id,
        outcome=IndicationResult.FOUND,
        indication_claim_id=found.indication_claim_id,
        reader_prompt_version=READER_PROMPT[1],
        checker_prompt_version=CHECKER_PROMPT[1],
        reader_model="another",
        checker_model="another",
        attempted_at=NOW,
        id=uuid4(),
    )
    with session_factory.begin() as session:
        session.execute(insert(TABLES[IndicationAttempt]).values(**stolen.model_dump()))
    summary = _publish(session_factory, store)
    assert summary["indications_written"] == 1
    assert summary["indications_held"] == {"the claim is not this catalyst's indication claim": 1}


def _plant(factory: sessionmaker[Session], catalyst_id: Any, value: dict[str, Any]) -> None:
    """Store a found try whose claim says `value`, as no reader would: to test the writer's own checks."""
    span = find_quote(document_text(RELEASE, "text/html"), TARGET)
    assert span is not None
    with factory.begin() as session:
        document = session.execute(select(TABLES[SourceDocument].c.id).limit(1)).scalar_one()
        event = session.execute(
            select(TABLES[Claim].c.id).where(TABLES[Claim].c.field == "catalyst:1").limit(1)
        ).scalar_one()
        claim = Claim(
            source_document_id=document,
            subject_type=ClaimSubject.CATALYST,
            subject_id=catalyst_id,
            field=claim_field(catalyst_id),
            value={"text_version": "1"} | value,
            quote=span.quote,
            quote_start=span.start,
            quote_end=span.end,
            agent="extractor",
            prompt_id=READER_PROMPT[0],
            prompt_version=READER_PROMPT[1],
            provider="fake",
            model="planted",
            extracted_at=NOW,
        )
        check = ClaimCheck(
            claim_id=claim.id, check_name=CONFIRMED_CHECK, outcome=CheckOutcome.PASS, checked_at=NOW
        )
        attempt = IndicationAttempt(
            catalyst_id=catalyst_id,
            claim_id=event,
            outcome=IndicationResult.FOUND,
            indication_claim_id=claim.id,
            reader_prompt_version=READER_PROMPT[1],
            checker_prompt_version=CHECKER_PROMPT[1],
            reader_model="planted",
            checker_model="planted",
            attempted_at=NOW,
        )
        for row in (claim, check, attempt):
            session.execute(insert(TABLES[type(row)]).values(**row.model_dump()))


def test_words_the_quote_does_not_hold_are_never_written(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    catalyst_id = seed_catalyst(session_factory, store)
    _plant(
        session_factory,
        catalyst_id,
        {"mesh_id": "D008545", "mesh_name": "Melanoma", "words": "heart failure"},
    )
    summary = _publish(session_factory, store)
    assert summary == {"indications_written": 0, "indications_held": {WORDS_NOT_IN_QUOTE: 1}}


def test_one_bad_row_is_held_and_the_rest_are_written(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    good = seed_catalyst(session_factory, store, n=1)
    bad = seed_catalyst(
        session_factory, store, n=2, role=DrugRole.IN_COMBINATION
    )  # left alone by the readers
    run_readers(session_factory, store, FakeReaders())
    _plant(session_factory, bad, {"mesh_id": "not-a-mesh-id", "mesh_name": "Melanoma", "words": "melanoma"})
    summary = _publish(session_factory, store)
    assert summary["indications_written"] == 1
    assert summary["indications_held"] == {NO_DISEASE: 1}
    rows = {c.id: c.indication_id for c in all_rows(session_factory, Catalyst)}
    assert rows[good] is not None
    assert rows[bad] is None
