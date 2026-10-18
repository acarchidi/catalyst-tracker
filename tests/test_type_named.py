"""A claim's catalyst type must be stated by its quote (CP4 review, O1; ADR 0018, part 5d).

CP4 found a topline sentence published as an FDA decision date, titled "FDA decision on
pegozafermin": no check read the type against the quote. These tests are judged against part
5d: the quote must hold words for the claimed type, from a fixed table per type. A quote that
names no kind of event at all (a table row) may rest on the `LOOK_BACK` characters before it. A
missing word holds the claim. Every correct gold-set claim must pass.
"""

from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import insert, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.repository import link_company_document, save_source_document, upsert_company
from catalyst.db.tables import TABLES
from catalyst.extract.text import document_text
from catalyst.schemas import (
    CatalystType,
    Claim,
    ClaimCheck,
    ClaimSubject,
    DocumentMatch,
    SourceDocument,
    SourceTier,
)
from catalyst.snapshots import LocalSnapshotStore, SnapshotStore
from catalyst.verify.event_type import (
    CHECK,
    LABELS,
    LOOK_BACK,
    WORDS,
    check_type_named,
    type_named,
    types_named,
)
from catalyst.verify.planted import ERRORS, Corpus, Record, load_corpus, plant
from catalyst.verify.rules import Outcome
from conftest import REPO_ROOT

GOLD = REPO_ROOT / "evals" / "extract_catalysts"
# Spyre's second-quarter release: its milestone table rows name no kind of event.
SPYRE_TABLE = GOLD / "documents" / "b1c8060030d379c487d6fda3bc3199902418affc1ca18ad3305b7f2618648e65.htm"
TOPLINE = (
    "Topline data from the Phase 3 RAINIER trial of pegozafermin are expected in the second half of 2027."
)


@cache
def _corpus() -> Corpus:
    return load_corpus()


def _check(record: Record, catalyst_type: str | None = None) -> Any:
    text = _corpus().texts[record.document]
    kind = record.value["catalyst_type"] if catalyst_type is None else catalyst_type
    return type_named(text, record.quote, record.quote_start, record.quote_end, kind)


def _alone(quote: str, catalyst_type: str, before: str = "") -> Any:
    text = f"{before}{quote}"
    return type_named(text, quote, len(before), len(text), catalyst_type)


# --- The table ----------------------------------------------------------------------------------


def test_every_catalyst_type_has_words_and_a_label() -> None:
    assert set(WORDS) == set(CatalystType) == set(LABELS)
    assert all(WORDS[t] for t in CatalystType)
    assert all(LABELS[t] and len(LABELS[t]) <= 40 for t in CatalystType)


# --- Every correct claim in the gold set ------------------------------------------------------


def test_every_correct_gold_claim_passes() -> None:
    corpus = _corpus()
    for record in (*corpus.records, *corpus.left_out):
        result = _check(record)
        assert result.check == CHECK
        assert result.outcome is Outcome.PASS, (record.value["date_text"], record.quote[:60], result.reason)


def test_a_table_row_rests_on_the_lead_in_just_before_it() -> None:
    # "SKYLINE Part A Ulcerative Colitis SPY003 Sept 2026" names no event; the line before the
    # table says "numerous expected topline readouts over the next 12-18 months".
    row = next(r for r in _corpus().records if r.quote.startswith("SKYLINE Part A"))
    assert types_named(row.quote) == frozenset()
    assert _check(row).outcome is Outcome.PASS
    result = _check(row, "pdufa_date")
    assert (result.outcome, result.reason) == (
        Outcome.FAIL,
        f"the quote names no kind of event, and the {LOOK_BACK} characters before it have no words "
        "for an FDA decision date",
    )


# --- The type swap CP4 found --------------------------------------------------------------------


def test_a_topline_sentence_typed_as_an_fda_decision_date_fails() -> None:
    result = _alone(TOPLINE, "pdufa_date")
    assert (result.outcome, result.reason) == (
        Outcome.FAIL,
        "the quote has words for topline data, not for an FDA decision date",
    )


def test_words_for_the_type_only_before_a_quote_that_names_another_event_do_not_count() -> None:
    # The lead-in is read only when the quote names no kind of event.
    before = "The FDA set a PDUFA target action date for pegozafermin. "
    assert _alone(TOPLINE, "pdufa_date", before).outcome is Outcome.FAIL


@pytest.mark.parametrize(
    ("quote", "catalyst_type"),
    [
        ("The FDA set a PDUFA target action date of June 30, 2027.", "pdufa_date"),
        ("An FDA advisory committee meeting is planned for the first half of 2027.", "advisory_committee"),
        ("Topline results are expected in 2027.", "topline_readout"),
        ("We will present the data at the AASLD annual meeting in November 2026.", "conference_presentation"),
        ("We plan to submit a BLA in the first half of 2027.", "regulatory_submission"),
        ("We expect FDA acceptance of the NDA filing in the first quarter of 2027.", "regulatory_acceptance"),
        ("A CHMP opinion is expected in mid-2027.", "chmp_opinion"),
        ("We expect to respond to the complete response letter in 2027.", "complete_response_letter"),
        ("We plan to file an sNDA for a label expansion in 2027.", "label_expansion"),
        ("Enrollment is expected to complete in the fourth quarter of 2026.", "enrollment_completion"),
        ("The public offering is expected to close on October 20, 2026.", "offering"),
        ("We intend to file a shelf registration statement on Form S-3 in 2026.", "shelf_filing"),
        ("The merger is expected to close in the first quarter of 2027.", "merger_acquisition"),
        ("We will report third-quarter financial results in November 2026.", "earnings"),
    ],
)
def test_each_type_passes_on_its_own_words(quote: str, catalyst_type: str) -> None:
    assert _alone(quote, catalyst_type).outcome is Outcome.PASS


def test_words_are_whole_words_and_case_does_not_matter_except_for_short_acronyms() -> None:
    assert _alone("TOPLINE DATA are expected in 2027.", "topline_readout").outcome is Outcome.PASS
    assert _alone("The database will lock in 2027.", "topline_readout").outcome is Outcome.FAIL
    assert _alone("Our index of sites grows in 2027.", "regulatory_submission").outcome is Outcome.FAIL
    assert _alone("We plan to submit an IND in 2027.", "regulatory_submission").outcome is Outcome.PASS


def test_an_open_label_study_is_not_a_label_expansion() -> None:
    # Spyre's sub-study sentences describe "open-label follow-up"; that names a trial design.
    quote = (
        "A study of SPY072 at Week 12 with open-label follow-up through Week 36. Topline data are expected."
    )
    assert types_named(quote) == frozenset({CatalystType.TOPLINE_READOUT})
    assert _alone("We plan to expand the label in 2027.", "label_expansion").outcome is Outcome.PASS


def test_an_unknown_type_fails() -> None:
    result = _alone(TOPLINE, "price_target")
    assert (result.outcome, result.reason) == (Outcome.FAIL, "'price_target' is not a catalyst type")


def test_a_table_row_not_at_its_stored_place_fails() -> None:
    row = "SKYLINE Part A Ulcerative Colitis SPY003 Sept 2026"
    text = f"Expected topline readouts: {row}"
    result = type_named(text, row, 0, len(row), "topline_readout")
    assert (result.outcome, result.reason) == (
        Outcome.FAIL,
        "the quote is not at its stored place in the stored copy",
    )


def test_a_table_row_without_its_copy_fails() -> None:
    row = "SKYLINE Part A Ulcerative Colitis SPY003 Sept 2026"
    result = type_named(None, row, 0, len(row), "topline_readout")
    assert result.outcome is Outcome.FAIL


# --- The planted wrong types --------------------------------------------------------------------


def test_most_planted_wrong_types_now_fail_without_the_second_reading() -> None:
    error = next(e for e in ERRORS if e.name == "wrong_type")
    corpus = _corpus()
    copies = [c for r in corpus.records if _check(r).outcome is Outcome.PASS for c in plant(error, r, corpus)]
    caught = [c for c in copies if _check(c).outcome is Outcome.FAIL]
    assert len(copies) >= 221
    assert len(caught) / len(copies) >= 0.95


# --- The job: every extractor claim; a copy read only for a quote that names no event ------------

PUBLISHED = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 18, 18, 0, tzinfo=UTC)
SPYRE_TEXT = document_text(SPYRE_TABLE.read_bytes(), "text/html")
ROW = "SKYLINE Part A Ulcerative Colitis SPY003 Sept 2026"
SENTENCE = (
    "Topline data for SPY072 in psoriatic arthritis (PsA) and axial spondyloarthritis (axSpA) "
    "on track for Q4 2026"
)


class _CountingStore:
    def __init__(self, store: SnapshotStore) -> None:
        self.store = store
        self.reads: list[str] = []

    def put(self, content: bytes) -> Any:
        return self.store.put(content)

    def get(self, key: str) -> bytes:
        self.reads.append(key)
        return self.store.get(key)


@pytest.fixture
def store(tmp_path: Path) -> _CountingStore:
    return _CountingStore(LocalSnapshotStore(tmp_path / "snapshots"))


def _document(session: Session, store: SnapshotStore, key: str = "sec:spyre", extra: bytes = b"") -> UUID:
    snapshot = store.put(SPYRE_TABLE.read_bytes() + extra)
    document, _ = save_source_document(
        session,
        SourceDocument(
            source_key=key,
            url=f"https://www.sec.gov/{key}.htm",
            publisher="SEC EDGAR",
            source_tier=SourceTier.REGULATOR_OR_SEC,
            document_type="8-K EX-99.1",
            published_at=PUBLISHED,
            retrieved_at=PUBLISHED,
            sha256=snapshot.sha256,
            snapshot_key=snapshot.key,
            content_type="text/html",
        ),
    )
    company = upsert_company(session, 1636282, "Spyre Therapeutics, Inc.", "2834").id
    link_company_document(session, company, document.id, DocumentMatch.SEC_CIK)
    return document.id


def _claim(
    session: Session, document: UUID, catalyst_type: str, quote: str, n: int, agent: str = "extractor"
) -> UUID:
    at = SPYRE_TEXT.index(quote)
    claim = Claim(
        source_document_id=document,
        subject_type=ClaimSubject.CATALYST,
        field=f"catalyst:{n:016d}",
        value={"catalyst_type": catalyst_type, "drug": "SPY003", "problems": []},
        quote=quote,
        quote_start=at,
        quote_end=at + len(quote),
        agent=agent,
        prompt_id="extract_catalysts",
        prompt_version="1.0.0",
        provider="anthropic",
        model="m",
        extracted_at=PUBLISHED,
    )
    session.execute(insert(TABLES[Claim]).values(**claim.model_dump()))
    return claim.id


def _outcomes(session_factory: sessionmaker[Session]) -> dict[UUID, tuple[str, str | None]]:
    table = TABLES[ClaimCheck]
    with session_factory() as session:
        rows = session.execute(select(table).where(table.c.check_name == CHECK)).mappings()
        return {r["claim_id"]: (r["outcome"], r["reason"]) for r in rows}


def test_the_job_stores_the_check_and_reads_a_copy_only_for_a_row_that_names_no_event(
    session_factory: sessionmaker[Session], store: _CountingStore
) -> None:
    with session_factory.begin() as session:
        document = _document(session, store)
        topline = _claim(session, document, "topline_readout", SENTENCE, 1)
        swapped = _claim(session, document, "pdufa_date", SENTENCE, 2)
        verifier = _claim(session, document, "pdufa_date", SENTENCE, 3, agent="verifier")
    report = check_type_named(session_factory, store, lambda: NOW)
    outcomes = _outcomes(session_factory)
    assert outcomes[topline] == ("pass", None)
    assert outcomes[swapped][0] == "fail"
    assert verifier not in outcomes
    assert store.reads == []  # both quotes name an event, so no copy was read
    assert report.summary() == {
        "claims": 2,
        "documents_read": 0,
        "documents_unreadable": 0,
        "outcomes": {f"{CHECK}:pass": 1, f"{CHECK}:fail": 1},
    }
    with session_factory.begin() as session:
        row = _claim(session, document, "topline_readout", ROW, 4)
    report = check_type_named(session_factory, store, lambda: NOW)
    assert _outcomes(session_factory)[row] == ("pass", None)
    assert (len(store.reads), report.documents_read) == (1, 1)


def test_an_unreadable_copy_fails_a_row_and_the_run_goes_on(
    session_factory: sessionmaker[Session], store: _CountingStore, tmp_path: Path
) -> None:
    elsewhere = LocalSnapshotStore(tmp_path / "elsewhere")
    with session_factory.begin() as session:
        missing = _document(session, elsewhere, "sec:missing", extra=b" ")
        lost = _claim(session, missing, "topline_readout", ROW, 1)
        kept = _claim(session, missing, "topline_readout", SENTENCE, 2)
    report = check_type_named(session_factory, store, lambda: NOW)
    assert _outcomes(session_factory) == {
        lost: ("fail", "the stored copy of the source could not be read (SnapshotNotFoundError)"),
        kept: ("pass", None),
    }
    assert (report.documents_read, report.documents_unreadable) == (0, 1)
