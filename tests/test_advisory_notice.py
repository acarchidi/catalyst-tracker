"""An advisory committee meeting needs a Federal Register notice (spec: Verification, rule checks).

A claimed advisory committee passes only when a stored notice, not replaced by a later amendment,
has its meeting inside the claim's window and names the same drug. Written before the check, on
the real recorded notices (ADR 0018, part 3).
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.documents import FetchedDocument, store_fetched
from catalyst.collectors.run import build_clients
from catalyst.collectors.sources import fedreg
from catalyst.db.tables import TABLES
from catalyst.schemas import Claim, ClaimCheck, ClaimSubject, SourceDocument
from catalyst.snapshots import LocalSnapshotStore
from catalyst.verify.advisory import CHECK, AdvisoryClaim, advisory_notice, check_advisory_meetings
from catalyst.verify.fedreg_notice import NoticesInWindow, StoredNotice, read_notice
from catalyst.verify.rules import Outcome
from conftest import RecordedServer

EMAIL = "tests@example.com"
NO_NOTICE = "no Federal Register notice for this meeting yet"


def _collected() -> dict[str, FetchedDocument]:
    clients = build_clients(EMAIL, RecordedServer("fedreg-2026-paged").transport, rate_limits=False)
    paged = fedreg.collect_advisory_committee_notices(clients.web, date(2026, 1, 1), max_pages=2, per_page=10)
    clients = build_clients(EMAIL, RecordedServer("fedreg-2026-08").transport, rate_limits=False)
    august = fedreg.collect_advisory_committee_notices(clients.web, date(2026, 8, 1))
    return {d.source_key.removeprefix("fedreg:"): d for d in [*paged, *august]}


COLLECTED = _collected()


def _stored(number: str) -> StoredNotice:
    fetched = COLLECTED[number]
    document = SourceDocument(
        source_key=fetched.source_key,
        url=fetched.url,
        publisher=fetched.publisher,
        source_tier=fetched.source_tier,
        document_type=fetched.document_type,
        published_at=fetched.published_at,
        retrieved_at=datetime(2026, 10, 18, tzinfo=UTC),
        sha256="a" * 64,
        snapshot_key="sha256/aa/" + "a" * 64,
        content_type=fetched.content_type,
    )
    return StoredNotice(document, read_notice(fetched.content))


def _found(*numbers: str, undated: tuple[str, ...] = (), superseded: tuple[str, ...] = ()) -> NoticesInWindow:
    return NoticesInWindow(
        matching=[_stored(n) for n in numbers],
        undated=[_stored(n) for n in undated],
        superseded=[_stored(n) for n in superseded],
    )


OCTOBER_30 = (date(2026, 10, 30), date(2026, 10, 30))
FOURTH_QUARTER = (date(2026, 10, 1), date(2026, 12, 31))


def _claim(
    drug: str | None = "Atropine Sulfate Ophthalmic Solution, 0.01%",
    window: tuple[date, date] | None = OCTOBER_30,
    catalyst_type: str = "advisory_committee",
) -> AdvisoryClaim:
    return AdvisoryClaim(catalyst_type=catalyst_type, drug=drug, window=window)


def _never(_a: str, _b: str) -> bool:
    return False


def _outcome(claim: AdvisoryClaim, found: NoticesInWindow) -> tuple[Outcome, str | None]:
    result = advisory_notice(claim, found, _never)
    assert result.check == CHECK
    return result.outcome, result.reason


def test_another_type_of_catalyst_is_not_applicable() -> None:
    assert _outcome(_claim(catalyst_type="topline_readout"), _found())[0] is Outcome.NOT_APPLICABLE


def test_a_notice_for_the_same_drug_and_day_passes() -> None:
    """2026-19700: the Dermatologic and Ophthalmic Drugs committee meets on October 30, 2026."""
    assert _outcome(_claim(), _found("2026-19700")) == (Outcome.PASS, None)


def test_a_wider_window_that_holds_the_meeting_passes() -> None:
    assert _outcome(_claim(window=FOURTH_QUARTER), _found("2026-19700"))[0] is Outcome.PASS


def test_a_shorter_name_for_the_drug_agrees() -> None:
    assert (
        _outcome(_claim(drug="atropine sulfate ophthalmic solution"), _found("2026-19700"))[0] is Outcome.PASS
    )


def test_a_name_shorter_than_five_letters_never_agrees_by_containment() -> None:
    assert _outcome(_claim(drug="atro"), _found("2026-19700"))[0] is Outcome.FAIL


def test_the_reference_tables_can_say_two_names_are_one_drug() -> None:
    result = advisory_notice(_claim(drug="MyopiaDrop"), _found("2026-19700"), lambda a, b: a == "MyopiaDrop")
    assert result.outcome is Outcome.PASS


def test_a_device_notice_counts_too() -> None:
    """2026-16245: the Molecular and Clinical Genetics Panel meets about Galleri on September 23."""
    claim = _claim(drug="Galleri", window=(date(2026, 9, 23), date(2026, 9, 23)))
    assert _outcome(claim, _found("2026-16245"))[0] is Outcome.PASS


def test_a_notice_for_another_drug_does_not_count() -> None:
    assert _outcome(_claim(drug="zentocel"), _found("2026-19700")) == (Outcome.FAIL, NO_NOTICE)


def test_no_notice_at_all_fails() -> None:
    assert _outcome(_claim(), _found()) == (Outcome.FAIL, NO_NOTICE)


def test_a_meeting_outside_the_claimed_window_does_not_count() -> None:
    """The lookup returns overlapping meetings; the meeting itself must sit inside the claim."""
    claim = _claim(window=(date(2026, 10, 30), date(2026, 10, 30)))
    two_day = _stored("2026-19700")
    assert two_day.notice.meeting is not None
    assert _outcome(claim, NoticesInWindow(matching=[two_day]))[0] is Outcome.PASS
    late = _claim(window=(date(2026, 10, 31), date(2026, 11, 30)))
    assert _outcome(late, _found("2026-19700"))[0] is Outcome.FAIL


def test_a_claim_with_no_window_or_no_drug_fails_with_its_reason() -> None:
    assert _outcome(_claim(window=None), _found("2026-19700")) == (
        Outcome.FAIL,
        "the claim has no date window to look up a notice",
    )
    assert _outcome(_claim(drug=None), _found("2026-19700")) == (
        Outcome.FAIL,
        "the claim names no drug, so no notice can confirm it",
    )


def test_a_notice_whose_date_cannot_be_read_says_so() -> None:
    """2026-15016 (Capricor, deramiocel) is an amendment whose DATES section is empty."""
    claim = _claim(drug="deramiocel")
    assert _outcome(claim, _found(undated=("2026-15016",))) == (
        Outcome.FAIL,
        "a notice names this drug, but its meeting date cannot be read",
    )


def test_a_replaced_notice_never_confirms_its_old_date() -> None:
    assert _outcome(_claim(), _found(superseded=("2026-19700",))) == (
        Outcome.FAIL,
        "the only notice for this drug was replaced by an amendment",
    )


def _undated_amendment_for_atropine(published: datetime) -> StoredNotice:
    """2026-15016's recorded amendment, retitled to name 2026-19700's drug (invented title, real form)."""
    stored = _stored("2026-15016")
    content = json.loads(COLLECTED["2026-15016"].content)
    content["title"] = (
        "Dermatologic and Ophthalmic Drugs Advisory Committee; Amendment of Notice-New Drug Application "
        "(NDA) 219694 From Example, Inc. for Atropine Sulfate Ophthalmic Solution, 0.01%"
    )
    content["abstract"] = ""
    notice = read_notice(json.dumps(content).encode())
    assert notice.amendment is not None
    assert notice.meeting is None
    return StoredNotice(stored.document.model_copy(update={"published_at": published}), notice)


def test_a_later_amendment_for_the_same_drug_sends_the_claim_to_review() -> None:
    """A later amendment naming the same drug never leaves the original date standing.

    An amendment is matched to its notice by title, which can miss (Claude review of PR #43).
    """
    original = _stored("2026-19700")
    later = _undated_amendment_for_atropine(original.document.published_at + timedelta(days=5))
    found = NoticesInWindow(matching=[original], undated=[later])
    assert _outcome(_claim(), found) == (
        Outcome.FAIL,
        "a later amendment names this drug, and its new date cannot be read",
    )


def test_an_earlier_amendment_does_not_block_a_later_dated_notice() -> None:
    original = _stored("2026-19700")
    earlier = _undated_amendment_for_atropine(original.document.published_at - timedelta(days=5))
    found = NoticesInWindow(matching=[original], undated=[earlier])
    assert _outcome(_claim(), found)[0] is Outcome.PASS


def _extractor_claim(document: UUID, kind: str, drug: str | None, window: tuple[date, date]) -> Claim:
    quote = f"An FDA advisory committee meeting for {drug} is scheduled for October 30, 2026."
    return Claim(
        source_document_id=document,
        subject_type=ClaimSubject.CATALYST,
        field=f"catalyst:{kind}",
        value={
            "catalyst_type": kind,
            "drug": drug,
            "window_start": window[0].isoformat(),
            "window_end": window[1].isoformat(),
            "window_precision": "day",
            "problems": [],
        },
        quote=quote,
        quote_start=0,
        quote_end=len(quote),
        agent="extractor",
        prompt_id="extract_catalysts",
        prompt_version="1.0.0",
        provider="anthropic",
        model="m",
        extracted_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def test_the_job_stores_an_outcome_for_every_extracted_claim(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    for fetched in COLLECTED.values():
        store_fetched(fetched, store, session_factory, now=lambda: datetime(2026, 10, 17, tzinfo=UTC))
    release = COLLECTED["2026-19700"]
    with session_factory.begin() as session:
        document = session.execute(
            select(TABLES[SourceDocument].c.id).where(
                TABLES[SourceDocument].c.source_key == release.source_key
            )
        ).scalar_one()
        claims = [
            _extractor_claim(
                document, "advisory_committee", "atropine sulfate ophthalmic solution", OCTOBER_30
            ),
            _extractor_claim(document, "pdufa_date", "atropine", OCTOBER_30),
        ]
        for claim in claims:
            session.execute(insert(TABLES[Claim]).values(**claim.model_dump()))
    report = check_advisory_meetings(session_factory, store, lambda: datetime(2026, 10, 18, tzinfo=UTC))
    assert report.claims == 2
    with session_factory() as session:
        rows = session.execute(
            select(TABLES[ClaimCheck]).where(TABLES[ClaimCheck].c.check_name == CHECK)
        ).all()
    outcomes = {row.claim_id: row.outcome for row in rows}
    assert outcomes == {claims[0].id: "pass", claims[1].id: "not_applicable"}
    again = check_advisory_meetings(session_factory, store, lambda: datetime(2026, 10, 18, tzinfo=UTC))
    assert again.outcomes == report.outcomes
