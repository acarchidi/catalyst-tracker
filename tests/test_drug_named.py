"""A claim's drug must be named in its quote or just before it (spec: Verification; ADR 0018, part 3e).

The planted-error test swapped quotes for a sentence about another event with the same date
words ("Topline proof-of-concept data are expected in September 2026." for a claim about SPY003),
and every check passed them. These tests are judged against ADR 0018 part 3e: the drug, by any
name it goes by, is written in the quote, or within the 1,000 characters before it with no other
drug of the company named after it. Every correct gold-set claim must pass or not apply.
"""

from dataclasses import replace
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy import insert, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.repository import link_company_document, save_source_document, upsert_company
from catalyst.db.tables import TABLES
from catalyst.extract.text import document_text
from catalyst.resolve import DrugRef
from catalyst.schemas import (
    Claim,
    ClaimCheck,
    ClaimSubject,
    DocumentMatch,
    Drug,
    DrugName,
    DrugNameKind,
    EntityMatch,
    MatchedEntity,
    MatchStatus,
    SourceDocument,
    SourceTier,
)
from catalyst.snapshots import LocalSnapshotStore, SnapshotStore
from catalyst.verify.drug import (
    CHECK,
    LOOK_BACK,
    DrugNames,
    DrugReport,
    check_drug_named,
    drug_named,
    drug_names,
    other_drugs,
)
from catalyst.verify.planted import ERRORS, Corpus, Record, load_corpus, plant
from catalyst.verify.planted_report import run, scores
from catalyst.verify.rules import CheckResult, Outcome
from conftest import REPO_ROOT

GOLD = REPO_ROOT / "evals" / "extract_catalysts"
# Spyre's second-quarter release, with a paragraph per SPY072 sub-study.
SPYRE_RELEASE = GOLD / "documents" / "eea1cc3d39f555b23a5d28e67a7d18463dfba6963b474af0dda42fa36178ffb3.htm"


@cache
def _corpus() -> Corpus:
    return load_corpus()


def _refs(record: Record, corpus: Corpus) -> list[DrugRef]:
    """Return the record's own drug in the reference tables, as the matching job would link it."""
    return [
        d
        for d in corpus.drug_records
        if d.company_id == record.company_id and record.value["drug"] in dict(d.names)
    ]


def _check(
    record: Record, corpus: Corpus, *, drug: str | None = None, extra: tuple[str, ...] = ()
) -> CheckResult:
    """Run the check on a gold record as the job does: its company's other drugs, and its release's."""
    names = drug_names(record.value["drug"] if drug is None else drug, _refs(record, corpus))
    if names is not None and extra:
        names = DrugNames(names.shown, (*names.names, *extra))
    release = [r for r in (*corpus.records, *corpus.left_out) if r.document == record.document]
    beside = [r.value["drug"] for r in release if r.id != record.id]
    others = other_drugs(corpus.drug_records, {record.company_id}, names, beside)
    text = corpus.texts[record.document]
    return drug_named(text, record.quote, record.quote_start, record.quote_end, names, others)


def _every_drug(corpus: Corpus) -> Corpus:
    """Put every gold drug on file, brand and generic names too, which production does not."""
    return replace(corpus, drug_records=corpus.drug_refs)


def _record(drug: str | None, date_text: str) -> Record:
    return next(r for r in _corpus().records if r.value["drug"] == drug and r.value["date_text"] == date_text)


def _names(*names: str) -> DrugNames:
    return DrugNames(names[0], names)


def _alone(text: str, quote: str, drug: DrugNames | None, *others: DrugNames) -> CheckResult:
    start = text.index(quote)
    return drug_named(text, quote, start, start + len(quote), drug, others)


# --- Every correct claim in the gold set ------------------------------------------------------


def test_every_correct_gold_claim_passes_or_names_no_drug() -> None:
    corpus = _corpus()
    for record in (*corpus.records, *corpus.left_out):
        result = _check(record, corpus)
        assert result.check == CHECK
        expected = Outcome.NOT_APPLICABLE if record.value["drug"] is None else Outcome.PASS
        assert result.outcome is expected, (record.value["drug"], record.value["date_text"], result.reason)


def test_a_drug_named_only_in_the_lines_just_before_the_quote_passes() -> None:
    # AEON's quote names no drug; the headline and the line under it name ABP-450 (146 characters back).
    assert "ABP-450" not in _record("ABP-450", "fourth quarter of 2026").quote
    assert _check(_record("ABP-450", "fourth quarter of 2026"), _corpus()).outcome is Outcome.PASS
    # Acadia's quote names its RADIANT study; the two quotations just before it name remlifanserin.
    acadia = _record("remlifanserin", "November 16-19, 2026")
    assert "remlifanserin" not in acadia.quote
    assert _check(acadia, _corpus()).outcome is Outcome.PASS


def test_a_combination_is_named_when_each_of_its_drugs_is() -> None:
    # "SPY072 + IL-17A/F" is written "SPY072 in combination with IL17-A/F".
    record = _record("SPY072 + IL-17A/F", "late 2027 or early 2028")
    assert _check(record, _corpus()).outcome is Outcome.PASS
    text = "Topline data for SPY072 in hidradenitis suppurativa in late 2027."
    assert _alone(text, text, _names("SPY072 + IL-17A/F"), _names("SPY072")).outcome is Outcome.FAIL


# --- The planted quotes from another event ----------------------------------------------------


def _same_date(drug: str, date_text: str) -> list[Record]:
    error = next(e for e in ERRORS if e.name == "quote_same_date")
    return list(plant(error, _record(drug, date_text), _corpus()))


def test_a_sentence_about_another_drugs_event_fails() -> None:
    # SPY072's sub-study sentence, 61 characters after SPY072; SPY003 was last named 1,689 back.
    (copy,) = _same_date("SPY003", "September 2026")
    assert copy.quote == "Topline proof-of-concept data are expected in September 2026."
    result = _check(copy, _corpus())
    assert (result.outcome, result.reason) == (
        Outcome.FAIL,
        "the quote and the text just before it do not name SPY003",
    )


@pytest.mark.parametrize(
    ("drug", "date_text", "other"),
    [
        ("SPY072", "September 2026", "SPY003"),
        ("Brelovitug", "Q4 2026", "LIVMARLI"),
        ("LIVMARLI", "Q4 2026", "Brelovitug"),
    ],
)
def test_a_quote_that_names_only_another_of_the_companys_drugs_fails(
    drug: str, date_text: str, other: str
) -> None:
    # Mirum's bullets sit next to each other: Brelovitug is named 144 characters before the
    # LIVMARLI bullet, so only the drug the quote itself names may decide.
    (copy,) = _same_date(drug, date_text)
    result = _check(copy, _corpus())
    assert (result.outcome, result.reason) == (Outcome.FAIL, f"the quote names {other}, not {drug}")


def test_the_sister_sub_study_of_the_same_drug_still_passes() -> None:
    """The limit of this check: the PsA and axSpA sub-studies of SPY072 end in the same sentence.

    The swapped sentence comes 61 characters after "SPY072" in the other sub-study's first
    sentence, so the drug is named just before it, as it is for a correct quote.
    """
    axspa = next(r for r in _corpus().records if r.quote.startswith("axSpA sub-study"))
    error = next(e for e in ERRORS if e.name == "quote_same_date")
    (copy,) = plant(error, axspa, _corpus())  # the PsA sub-study's second sentence
    assert copy.quote == "Topline proof-of-concept data are expected in the fourth quarter of 2026."
    assert _check(copy, _corpus()).outcome is Outcome.PASS


# --- How names are found ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "written",
    ["SPY003", "SPY-003", "SPY 003", "spy003", "Spy\u2011003", "SPY003-treated", "(SPY003)", "SPY003®"],
)
def test_a_code_name_counts_with_a_dash_or_a_space_in_any_case(written: str) -> None:
    text = f"Topline data for {written} are expected in September 2026."
    assert _alone(text, text, _names("SPY003")).outcome is Outcome.PASS


@pytest.mark.parametrize("written", ["SPY0031", "XSPY003", "SPY00", "SPY003A", "SPY. 003"])
def test_a_name_inside_a_longer_name_or_split_by_punctuation_does_not_count(written: str) -> None:
    text = f"Topline data for {written} are expected in September 2026."
    assert _alone(text, text, _names("SPY003")).outcome is Outcome.FAIL


@pytest.mark.parametrize(
    ("claimed", "written"), [("Cemdisiran\u2122", "cemdisiran"), ("cemdisiran", "Cemdisiran\u2122")]
)
def test_a_trademark_sign_on_either_side_does_not_hide_the_name(claimed: str, written: str) -> None:
    """Unicode spells "\u2122" as the letters "TM"; it must not join the name (review of PR #77)."""
    text = f"Topline data for {written} are expected in 2027."
    assert _alone(text, text, _names(claimed)).outcome is Outcome.PASS


def test_any_name_the_reference_tables_give_the_drug_counts() -> None:
    text = "Topline data for ACP-204 in Alzheimer's disease psychosis are expected in 2027."
    drug = drug_names("remlifanserin", [DrugRef(uuid4(), uuid4(), (("ACP-204", DrugNameKind.CODE),))])
    assert drug is not None
    assert drug.names == ("remlifanserin", "ACP-204")
    assert _alone(text, text, drug).outcome is Outcome.PASS
    assert _alone(text, text, _names("remlifanserin")).outcome is Outcome.FAIL


def test_a_claim_with_no_drug_does_not_apply() -> None:
    assert drug_names(None, []) is None
    assert drug_names("  ", []) is None
    result = _alone("Induction data are expected in 2027.", "Induction data are expected in 2027.", None)
    assert (result.outcome, result.reason) == (Outcome.NOT_APPLICABLE, "the claim names no drug")


# --- The window before the quote --------------------------------------------------------------

QUOTE = "Topline data are expected in the fourth quarter of 2026."


def test_the_drug_may_be_named_up_to_the_window_before_the_quote() -> None:
    lead = "SPY072 is in development."
    inside = lead + " " * (LOOK_BACK - len(lead)) + QUOTE  # "SPY072" starts exactly LOOK_BACK back
    assert _alone(inside, QUOTE, _names("SPY072")).outcome is Outcome.PASS
    outside = lead + " " * (LOOK_BACK - len(lead) + 1) + QUOTE
    result = _alone(outside, QUOTE, _names("SPY072"))
    assert (result.outcome, result.reason) == (
        Outcome.FAIL,
        "the quote and the text just before it do not name SPY072",
    )


def test_another_of_the_companys_drugs_named_after_the_drug_fails() -> None:
    text = f"SPY072 completed enrollment. SPY003 data were presented. {QUOTE}"
    result = _alone(text, QUOTE, _names("SPY072"), _names("SPY003"))
    assert (result.outcome, result.reason) == (
        Outcome.FAIL,
        "the text just before the quote names SPY003 after SPY072",
    )
    later = f"SPY003 data were presented. SPY072 completed enrollment. {QUOTE}"
    assert _alone(later, QUOTE, _names("SPY072"), _names("SPY003")).outcome is Outcome.PASS


def test_a_drug_the_company_does_not_own_does_not_count_as_another() -> None:
    # AEON's line under the headline names ABP-450 and then BOTOX, another company's drug.
    text = f"Earlier ABP-450 studies versus BOTOX may be submitted as supportive evidence\n{QUOTE}"
    abp = DrugRef(uuid4(), UUID(int=1), (("ABP-450", DrugNameKind.CODE),))
    botox = DrugRef(uuid4(), UUID(int=2), (("BOTOX", DrugNameKind.BRAND),))
    drug = drug_names("ABP-450", [abp])
    others = other_drugs([abp, botox], {UUID(int=1)}, drug)
    assert others == ()
    assert _alone(text, QUOTE, drug, *others).outcome is Outcome.PASS
    assert _alone(
        text, QUOTE, drug, *other_drugs([abp, botox], {UUID(int=1), UUID(int=2)}, drug)
    ).outcome is (Outcome.FAIL)


def test_the_claims_own_drug_is_never_counted_as_another() -> None:
    mine = DrugRef(
        uuid4(), UUID(int=1), (("LIVMARLI", DrugNameKind.BRAND), ("maralixibat", DrugNameKind.INN))
    )
    twin = DrugRef(uuid4(), UUID(int=1), (("Livmarli", DrugNameKind.BRAND),))  # a second record, same name
    other = DrugRef(uuid4(), UUID(int=1), (("volixibat", DrugNameKind.INN),))
    drug = drug_names("LIVMARLI", [mine])
    assert other_drugs([mine, twin, other], {UUID(int=1)}, drug) == (_names("volixibat"),)


def test_a_quote_not_at_its_stored_place_fails() -> None:
    result = drug_named(f"SPY072 {QUOTE}", QUOTE, 0, len(QUOTE), _names("SPY072"), ())
    assert (result.outcome, result.reason) == (
        Outcome.FAIL,
        "the quote is not at its stored place in the stored copy",
    )


# --- The job: every extractor claim, against its stored copy ----------------------------------

PUBLISHED = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 18, 18, 0, tzinfo=UTC)
SPYRE_TEXT = document_text(SPYRE_RELEASE.read_bytes(), "text/html")
OWN = (
    "RA sub-study: Double-blind, placebo-controlled safety and efficacy study of two dose levels of SPY072 "
    "at Week 12 with open-label follow-up through Week 36."
)
NAMES_SPY003 = (
    "Enrollment for Part A has completed, with SPY001 and SPY002 Part A topline induction data announced in "
    "Q2 2026, and topline data expected for SPY003 in September 2026."
)
SWAPPED = "Topline proof-of-concept data are expected in September 2026."


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
    snapshot = store.put(SPYRE_RELEASE.read_bytes() + extra)
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
    return document.id


def _claim(
    session: Session,
    document: UUID,
    drug: str | None,
    quote: str,
    n: int = 1,
    agent: str = "extractor",
    start: int | None = None,
) -> UUID:
    at = SPYRE_TEXT.index(quote) if start is None else start
    claim = Claim(
        source_document_id=document,
        subject_type=ClaimSubject.CATALYST,
        field=f"catalyst:{n:016d}",
        value={"catalyst_type": "topline_readout", "drug": drug, "problems": []},
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


def _drug(session: Session, company: UUID, *names: tuple[str, DrugNameKind]) -> UUID:
    drug = Drug(
        id=uuid5(NAMESPACE_URL, f"drug:{names[0][0]}"), company_id=company, preferred_name=names[0][0]
    )
    session.execute(insert(TABLES[Drug]).values(**drug.model_dump()))
    for name, kind in names:
        row = DrugName(drug_id=drug.id, name=name, kind=kind)
        session.execute(insert(TABLES[DrugName]).values(**row.model_dump()))
    return drug.id


def _matched(session: Session, claim: UUID, drug: UUID, name: str) -> None:
    row = EntityMatch(
        claim_id=claim,
        entity_type=MatchedEntity.DRUG,
        name=name,
        status=MatchStatus.RESOLVED,
        matched_id=drug,
        matched_by="code_name",
        candidates=[str(drug)],
        resolved_at=PUBLISHED,
    )
    session.execute(insert(TABLES[EntityMatch]).values(**row.model_dump()))


def _outcomes(session_factory: sessionmaker[Session]) -> dict[UUID, tuple[str, str | None]]:
    table = TABLES[ClaimCheck]
    with session_factory() as session:
        rows = session.execute(select(table).where(table.c.check_name == CHECK)).mappings()
        return {r["claim_id"]: (r["outcome"], r["reason"]) for r in rows}


@pytest.fixture
def spyre(session_factory: sessionmaker[Session]) -> UUID:
    with session_factory.begin() as session:
        return upsert_company(session, 1636282, "Spyre Therapeutics, Inc.", "2834").id


def test_the_job_passes_a_claim_whose_quote_names_its_drug_and_fails_a_swapped_quote(
    session_factory: sessionmaker[Session], store: _CountingStore, spyre: UUID
) -> None:
    with session_factory.begin() as session:
        document = _document(session, store)
        link_company_document(session, spyre, document, DocumentMatch.SEC_CIK)
        _drug(session, spyre, ("SPY072", DrugNameKind.CODE))
        _drug(session, spyre, ("SPY003", DrugNameKind.CODE))
        good = _claim(session, document, "SPY072", OWN, 1)
        bad = _claim(session, document, "SPY003", SWAPPED, 2)
        none = _claim(session, document, None, SWAPPED, 3)
        verifier = _claim(session, document, "SPY003", SWAPPED, 4, agent="verifier")
    report = check_drug_named(session_factory, store, lambda: NOW)
    outcomes = _outcomes(session_factory)
    assert outcomes == {
        good: ("pass", None),
        bad: ("fail", "the quote and the text just before it do not name SPY003"),
        none: ("not_applicable", "the claim names no drug"),
    }
    assert verifier not in outcomes
    assert len(store.reads) == 1  # one document, read once
    assert report.summary() == {
        "claims": 3,
        "documents_read": 1,
        "documents_unreadable": 0,
        "outcomes": {f"{CHECK}:pass": 1, f"{CHECK}:fail": 1, f"{CHECK}:not_applicable": 1},
    }


def test_the_job_finds_the_matched_drugs_other_names_and_the_companys_other_drugs(
    session_factory: sessionmaker[Session], store: _CountingStore, spyre: UUID
) -> None:
    with session_factory.begin() as session:
        document = _document(session, store)
        link_company_document(session, spyre, document, DocumentMatch.SEC_CIK)
        renamed = _drug(session, spyre, ("SPY-9072", DrugNameKind.CODE), ("SPY072", DrugNameKind.CODE))
        _drug(session, spyre, ("SPY003", DrugNameKind.CODE))
        by_other_name = _claim(session, document, "SPY-9072", OWN, 1)
        _matched(session, by_other_name, renamed, "SPY-9072")
        another = _claim(session, document, "SPY072", NAMES_SPY003, 2)
    check_drug_named(session_factory, store, lambda: NOW)
    assert _outcomes(session_factory) == {
        by_other_name: ("pass", None),
        another: ("fail", "the quote names SPY003, not SPY072"),
    }


def test_a_claim_with_no_drug_is_not_applicable_without_reading_the_copy(
    session_factory: sessionmaker[Session], store: _CountingStore, spyre: UUID
) -> None:
    with session_factory.begin() as session:
        document = _document(session, store)
        link_company_document(session, spyre, document, DocumentMatch.SEC_CIK)
        claim = _claim(session, document, None, SWAPPED)
    report = check_drug_named(session_factory, store, lambda: NOW)
    assert _outcomes(session_factory) == {claim: ("not_applicable", "the claim names no drug")}
    assert store.reads == []
    assert report.documents_read == 0


def test_an_unreadable_copy_fails_its_claims_and_the_run_goes_on(
    session_factory: sessionmaker[Session], store: _CountingStore, spyre: UUID, tmp_path: Path
) -> None:
    elsewhere = LocalSnapshotStore(tmp_path / "elsewhere")  # stored where the job's store cannot see it
    with session_factory.begin() as session:
        missing = _document(session, elsewhere, "sec:missing", extra=b" ")  # another file, another hash
        readable = _document(session, store, "sec:spyre-again")
        for document in (missing, readable):
            link_company_document(session, spyre, document, DocumentMatch.SEC_CIK)
        lost, kept = _claim(session, missing, "SPY072", OWN, 1), _claim(session, readable, "SPY072", OWN, 2)
    report = check_drug_named(session_factory, store, lambda: NOW)
    assert _outcomes(session_factory) == {
        lost: ("fail", "the stored copy of the source could not be read (SnapshotNotFoundError)"),
        kept: ("pass", None),
    }
    assert (report.documents_read, report.documents_unreadable) == (1, 1)


def test_a_re_run_keeps_one_row_per_claim(
    session_factory: sessionmaker[Session], store: _CountingStore, spyre: UUID
) -> None:
    with session_factory.begin() as session:
        document = _document(session, store)
        link_company_document(session, spyre, document, DocumentMatch.SEC_CIK)
        claim = _claim(session, document, "SPY072", OWN)
    check_drug_named(session_factory, store, lambda: NOW)
    check_drug_named(session_factory, store, lambda: NOW)
    assert _outcomes(session_factory) == {claim: ("pass", None)}
    with session_factory() as session:
        assert len(session.execute(select(TABLES[ClaimCheck])).all()) == 1


def test_an_empty_database_stores_nothing(
    session_factory: sessionmaker[Session], store: _CountingStore
) -> None:
    assert check_drug_named(session_factory, store, lambda: NOW) == DrugReport()


# --- Production-like reference tables (Opus review of PR #61) ---------------------------------
#
# Production stores a drug record only for a company's new code name (`resolve_claims`), so
# brand and generic names such as LIVMARLI and Brelovitug are not on file. The drugs the
# release's other claims name stand in for them.


def test_the_planted_test_holds_only_the_drug_records_production_holds() -> None:
    names = {name for d in _corpus().drug_records for name, _ in d.names}
    assert names == {"ABP-450", "SPY003", "SPY072"}


def test_another_claims_drug_in_the_same_release_counts_as_another_drug() -> None:
    drug = drug_names("Brelovitug", [])
    assert other_drugs([], set(), drug, ["LIVMARLI", "brelovitug", None, "volixibat", "LIVMARLI"]) == (
        _names("LIVMARLI"),
        _names("volixibat"),
    )
    text = "LIVMARLI EXPAND Phase 3 study topline results expected in Q4 2026."
    result = _alone(text, text, drug, *other_drugs([], set(), drug, ["LIVMARLI"]))
    assert (result.outcome, result.reason) == (Outcome.FAIL, "the quote names LIVMARLI, not Brelovitug")


def test_with_every_drug_on_file_every_correct_gold_claim_still_passes() -> None:
    corpus = _every_drug(_corpus())
    for record in corpus.records:
        expected = Outcome.NOT_APPLICABLE if record.value["drug"] is None else Outcome.PASS
        assert _check(record, corpus).outcome is expected, record.value["drug"]


def test_with_production_like_tables_the_same_date_swaps_are_still_caught() -> None:
    """The page's drug-check numbers rest only on what production holds (Opus review of PR #61).

    With code names alone on file, 4 of 5 same-date swaps are caught, as with every drug on file.
    A wrong drug from the same release is caught a little less often: when the copy replaced the
    only claim naming the true drug, and that drug is a brand or generic name (TG's BRIUMVI and
    azer-cel, Mirum's LIVMARLI and zilurgisertib), nothing on file names it.
    """
    like, full = scores(run(_corpus())), scores(run(_every_drug(_corpus())))
    assert like["errors"]["quote_same_date"]["caught"] == full["errors"]["quote_same_date"]["caught"] == 4
    assert like["errors"]["drug_in_release"]["caught_by_rules"] == 22
    assert full["errors"]["drug_in_release"]["caught_by_rules"] == 26
    assert like["clean"] == full["clean"]


def test_the_job_counts_the_drugs_its_releases_other_claims_name(
    session_factory: sessionmaker[Session], store: _CountingStore, spyre: UUID
) -> None:
    """No drug is on file; SPY003 is known only because another claim of the release names it."""
    with session_factory.begin() as session:
        document = _document(session, store)
        link_company_document(session, spyre, document, DocumentMatch.SEC_CIK)
        _claim(session, document, "SPY003", NAMES_SPY003, 1)
        swapped = _claim(session, document, "SPY072", NAMES_SPY003, 2)
        own = _claim(session, document, "SPY072", OWN, 3)
    check_drug_named(session_factory, store, lambda: NOW)
    outcomes = _outcomes(session_factory)
    assert outcomes[swapped] == ("fail", "the quote names SPY003, not SPY072")
    assert outcomes[own] == ("pass", None)


# --- Hidden characters, combinations, and the window's size (Opus review of PR #61) -----------


@pytest.mark.parametrize("hidden", ["­", "​", "‌", "‍", "⁠", "﻿"])
def test_a_hidden_character_inside_a_name_does_not_hide_it(hidden: str) -> None:
    text = f"Topline data for SPY{hidden}002 are expected in 2027."
    assert _alone(text, text, _names("SPY002")).outcome is Outcome.PASS
    assert _alone(
        "Topline data for SPY002.", "Topline data for SPY002.", _names(f"SPY{hidden}002")
    ).outcome is (Outcome.PASS)


@pytest.mark.parametrize("hidden", ["­", "‍", "⁠", "﻿"])
def test_a_hidden_character_does_not_make_a_longer_name_a_whole_word(hidden: str) -> None:
    for text in (f"Data for X{hidden}SPY002 in 2027.", f"Data for SPY002{hidden}A in 2027."):
        assert _alone(text, text, _names("SPY002")).outcome is Outcome.FAIL, repr(text)


COMBO = "SPY072 + IL-17A/F"


def test_a_single_drug_claim_last_named_inside_a_combination_is_held() -> None:
    """The quote may be about the combination, not the drug alone, so it goes to review."""
    before = f"{COMBO} data were presented. {QUOTE}"
    result = _alone(before, QUOTE, _names("SPY072"), _names(COMBO))
    assert (result.outcome, result.reason) == (
        Outcome.FAIL,
        "the quote and the text just before it do not name SPY072",
    )
    inside = f"Topline data for {COMBO} are expected in 2027."
    result = _alone(inside, inside, _names("SPY072"), _names(COMBO))
    assert (result.outcome, result.reason) == (Outcome.FAIL, f"the quote names {COMBO}, not SPY072")
    alone = f"{COMBO} data were presented. SPY072 completed enrollment. {QUOTE}"
    assert _alone(alone, QUOTE, _names("SPY072"), _names(COMBO)).outcome is Outcome.PASS


def test_a_drug_inside_the_combinations_own_name_is_not_another_drug() -> None:
    text = f"{COMBO} data were presented. {QUOTE}"
    assert _alone(text, QUOTE, _names(COMBO), _names("SPY072")).outcome is Outcome.PASS


def test_a_combinations_drugs_must_be_named_near_each_other() -> None:
    far = "SPY072 completed enrollment." + " " * 300 + f"IL-17A/F is approved. {QUOTE}"
    result = _alone(far, QUOTE, _names(COMBO))
    assert (result.outcome, result.reason) == (
        Outcome.FAIL,
        f"the quote and the text just before it do not name {COMBO}",
    )
    near = "SPY072 in combination with IL-17A/F is planned." + " " * 600 + QUOTE
    assert _alone(near, QUOTE, _names(COMBO)).outcome is Outcome.PASS
    # Parts named at different distances: the drug alone, named after the pair, is another drug.
    later = "SPY072 in combination with IL-17A/F is planned." + " " * 600 + f"SPY072 alone. {QUOTE}"
    result = _alone(later, QUOTE, _names(COMBO), _names("SPY072"))
    assert (result.outcome, result.reason) == (
        Outcome.FAIL,
        f"the text just before the quote names SPY072 after {COMBO}",
    )
    assert _alone(later, QUOTE, _names(COMBO)).outcome is Outcome.PASS


def test_the_window_is_one_thousand_characters() -> None:
    assert LOOK_BACK == 1_000
    at_edge = "SPY072" + " " * 994 + QUOTE  # "SPY072" starts 1,000 characters before the quote
    assert _alone(at_edge, QUOTE, _names("SPY072")).outcome is Outcome.PASS
    beyond = "SPY072" + " " * 995 + QUOTE
    assert _alone(beyond, QUOTE, _names("SPY072")).outcome is Outcome.FAIL
