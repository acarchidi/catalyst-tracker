"""Incremental checks (ADR 0064): a run checks again only what is new or changed, or ran on older code.

The store's copy-reading checks skip a claim whose stored row was found by the same code version
from the same inputs; the in-memory checks are computed every run but write only changed rows.
A run that reaches its time limit stops cleanly and the next run picks up the rest.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, event, insert, select, update
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.repository import link_company_document, save_source_document, upsert_company
from catalyst.db.tables import TABLES
from catalyst.extract.text import document_text
from catalyst.publish.gate import REQUIRED_CHECKS
from catalyst.references import CompanyIdentity, record_company_identity
from catalyst.schemas import (
    Claim,
    ClaimCheck,
    ClaimSubject,
    CompanyDocument,
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
from catalyst.verify import incremental
from catalyst.verify.company import CHECK as COMPANY_CHECK
from catalyst.verify.company import check_company_named
from catalyst.verify.drug import CHECK as DRUG_CHECK
from catalyst.verify.drug import check_drug_named
from catalyst.verify.event_type import CHECK as TYPE_CHECK
from catalyst.verify.event_type import check_type_named
from catalyst.verify.incremental import (
    Found,
    check_version,
    input_digest,
    load_stamps,
    module_closure,
    write_checks,
)
from catalyst.verify.rules import CheckResult, Outcome
from catalyst.verify.run import run_checks
from conftest import REPO_ROOT

GOLD = REPO_ROOT / "evals" / "extract_catalysts" / "documents"
SPYRE_RELEASE = GOLD / "eea1cc3d39f555b23a5d28e67a7d18463dfba6963b474af0dda42fa36178ffb3.htm"
MIRUM_RELEASE = GOLD / "24272f6faabdc564770f7d3eac45e2893d7fe25c56a04dd2ca232a5f43f66c80.htm"
SPYRE_TEXT = document_text(SPYRE_RELEASE.read_bytes(), "text/html")
OWN = (
    "RA sub-study: Double-blind, placebo-controlled safety and efficacy study of two dose levels of SPY072 "
    "at Week 12 with open-label follow-up through Week 36."
)
PUBLISHED = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 18, 18, 0, tzinfo=UTC)


class _CountingStore:
    def __init__(self, store: SnapshotStore) -> None:
        self.store = store
        self.reads: list[str] = []

    def put(self, content: bytes) -> Any:
        return self.store.put(content)

    def get(self, key: str) -> bytes:
        self.reads.append(key)
        return self.store.get(key)


def _version(value: str) -> Callable[[str], str]:
    """Stand in for new check code: every check gets version `value`."""

    def version(_name: str) -> str:
        return value

    return version


@pytest.fixture
def store(tmp_path: Path) -> _CountingStore:
    return _CountingStore(LocalSnapshotStore(tmp_path / "snapshots"))


@pytest.fixture
def writes(engine: Engine) -> Iterator[list[str]]:
    """Every INSERT, UPDATE, or DELETE statement sent to the database while the test runs."""
    seen: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine, "before_cursor_execute", record)


def _company(session: Session, cik: int, name: str, ticker: str) -> UUID:
    company = upsert_company(session, cik, name, "2834").id
    identity = CompanyIdentity(name=name, former_names=(), tickers=((ticker, "Nasdaq"),))
    record_company_identity(session, company, identity, date(2026, 10, 1))
    return company


def _document(session: Session, store: SnapshotStore, key: str, content: bytes) -> UUID:
    snapshot = store.put(content)
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
    session: Session, document: UUID, n: int = 1, drug: str | None = "SPY072", quote: str = OWN
) -> UUID:
    start = SPYRE_TEXT.find(quote)
    start = max(start, 0)
    claim = Claim(
        source_document_id=document,
        subject_type=ClaimSubject.CATALYST,
        field=f"catalyst:{n:016d}",
        value={"catalyst_type": "topline_readout", "drug": drug, "problems": []},
        quote=quote,
        quote_start=start,
        quote_end=start + len(quote),
        agent="extractor",
        prompt_id="extract_catalysts",
        prompt_version="1.0.0",
        provider="anthropic",
        model="m",
        extracted_at=PUBLISHED,
    )
    session.execute(insert(TABLES[Claim]).values(**claim.model_dump()))
    return claim.id


def _rows(session_factory: sessionmaker[Session], check: str) -> dict[UUID, dict[str, Any]]:
    table = TABLES[ClaimCheck]
    with session_factory() as session:
        found = session.execute(select(table).where(table.c.check_name == check)).mappings()
        return {row["claim_id"]: dict(row) for row in found}


@pytest.fixture
def companies(session_factory: sessionmaker[Session]) -> dict[str, UUID]:
    with session_factory.begin() as session:
        return {
            "spyre": _company(session, 1636282, "Spyre Therapeutics, Inc.", "SYRE"),
            "mirum": _company(session, 1759425, "Mirum Pharmaceuticals, Inc.", "MIRM"),
        }


# --- Versions and digests -----------------------------------------------------------------------


def test_a_check_version_covers_every_module_of_ours_its_code_imports() -> None:
    closure = module_closure("catalyst.verify.company")
    assert {"catalyst.verify.company", "catalyst.collectors.coverage", "catalyst.extract.text"} <= closure
    assert "catalyst.verify.rules" in closure  # imported by the check, so a change to it counts too
    assert "catalyst.cli" not in closure


def test_a_new_python_is_a_new_check_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new interpreter can read the same code differently, so every claim is checked again once."""
    module = "catalyst.verify.company"
    today = incremental.code_version(module)
    incremental.code_version.cache_clear()
    monkeypatch.setattr(incremental.platform, "python_version", lambda: "3.99.0")
    try:
        assert incremental.code_version(module) != today
    finally:
        incremental.code_version.cache_clear()
    monkeypatch.undo()
    assert incremental.code_version(module) == today


def test_every_required_check_has_a_version() -> None:
    for name in REQUIRED_CHECKS:
        version = check_version(name)
        assert len(version) == 16
        assert int(version, 16) >= 0


def test_the_copy_checks_are_versioned_by_their_own_modules() -> None:
    assert incremental.CHECK_MODULES[COMPANY_CHECK] == "catalyst.verify.company"
    assert incremental.CHECK_MODULES[DRUG_CHECK] == "catalyst.verify.drug"
    assert incremental.CHECK_MODULES[TYPE_CHECK] == "catalyst.verify.event_type"


def test_an_input_digest_ignores_set_order_and_sees_every_value() -> None:
    a, b = uuid4(), uuid4()
    assert input_digest(frozenset({a, b}), "x") == input_digest(frozenset({b, a}), "x")
    assert input_digest(frozenset({a, b}), "x") != input_digest(frozenset({a, b}), "y")
    assert input_digest(("x", None)) != input_digest(("x", ""))
    with pytest.raises(TypeError):
        input_digest(object())


# --- Writing only what changed ------------------------------------------------------------------


def test_write_checks_writes_only_changed_rows_and_keeps_checked_at_for_an_unchanged_outcome(
    session_factory: sessionmaker[Session], store: _CountingStore, writes: list[str]
) -> None:
    with session_factory.begin() as session:
        document = _document(session, store, "sec:spyre", SPYRE_RELEASE.read_bytes())
        claims = [_claim(session, document, n) for n in range(1, 4)]
    passed = CheckResult("some_check", Outcome.PASS)
    writes.clear()
    with session_factory.begin() as session:
        assert write_checks(session, [Found(c, passed) for c in claims], NOW, load_stamps(session)) == 3
    assert len(writes) == 1  # one statement for every row
    writes.clear()
    later = NOW + timedelta(days=1)
    failed = CheckResult("some_check", Outcome.FAIL, "now it fails")
    with session_factory.begin() as session:
        found = [Found(claims[0], failed), Found(claims[1], passed), Found(claims[2], passed)]
        assert write_checks(session, found, later, load_stamps(session)) == 1
    rows = _rows(session_factory, "some_check")
    assert (rows[claims[0]]["outcome"], rows[claims[0]]["checked_at"]) == ("fail", later)
    assert rows[claims[1]]["checked_at"] == NOW
    assert {row["rule_version"] for row in rows.values()} == {check_version("some_check")}
    writes.clear()
    with session_factory.begin() as session:
        found = [Found(claims[0], failed), Found(claims[1], passed), Found(claims[2], passed)]
        assert write_checks(session, found, later, load_stamps(session)) == 0
    assert writes == []


def test_a_new_code_version_rewrites_the_row_but_keeps_since_when_it_had_its_outcome(
    session_factory: sessionmaker[Session], store: _CountingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_factory.begin() as session:
        claim = _claim(session, _document(session, store, "sec:spyre", SPYRE_RELEASE.read_bytes()))
    passed = CheckResult("some_check", Outcome.PASS)
    with session_factory.begin() as session:
        write_checks(session, [Found(claim, passed)], NOW, load_stamps(session))
    monkeypatch.setattr(incremental, "check_version", _version("0123456789abcdef"))
    with session_factory.begin() as session:
        assert (
            write_checks(session, [Found(claim, passed)], NOW + timedelta(days=1), load_stamps(session)) == 1
        )
    row = _rows(session_factory, "some_check")[claim]
    assert (row["rule_version"], row["checked_at"]) == ("0123456789abcdef", NOW)


def test_the_rule_checks_write_nothing_on_a_run_that_changes_nothing(
    session_factory: sessionmaker[Session],
    store: _CountingStore,
    companies: dict[str, UUID],
    writes: list[str],
) -> None:
    with session_factory.begin() as session:
        document = _document(session, store, "sec:spyre", SPYRE_RELEASE.read_bytes())
        link_company_document(session, companies["spyre"], document, DocumentMatch.SEC_CIK)
        _claim(session, document)
    first = run_checks(session_factory, lambda: NOW)
    assert first.rows_written == first.claims * len(_names(session_factory))
    writes.clear()
    again = run_checks(session_factory, lambda: NOW)
    assert writes == []
    assert again.rows_written == 0
    assert again.summary()["claims"] == first.summary()["claims"]
    with session_factory() as session:
        for row in session.execute(select(TABLES[ClaimCheck])).mappings():
            assert row["rule_version"] == check_version(row["check_name"])


def _names(session_factory: sessionmaker[Session]) -> set[str]:
    with session_factory() as session:
        return set(session.execute(select(TABLES[ClaimCheck].c.check_name)).scalars())


# --- The copy-reading checks skip unchanged claims -----------------------------------------------


def test_the_company_check_skips_a_claim_whose_inputs_and_code_are_unchanged(
    session_factory: sessionmaker[Session],
    store: _CountingStore,
    companies: dict[str, UUID],
    writes: list[str],
) -> None:
    with session_factory.begin() as session:
        document = _document(session, store, "sec:spyre", SPYRE_RELEASE.read_bytes())
        link_company_document(session, companies["spyre"], document, DocumentMatch.SEC_CIK)
        claim = _claim(session, document)
    check_company_named(session_factory, store, lambda: NOW)
    assert len(store.reads) == 1
    row = _rows(session_factory, COMPANY_CHECK)[claim]
    assert row["outcome"] == "pass"
    assert row["input_digest"] is not None
    store.reads.clear()
    writes.clear()
    again = check_company_named(session_factory, store, lambda: NOW + timedelta(days=1))
    assert store.reads == []
    assert writes == []
    assert again.summary()["claims_unchanged"] == 1
    assert _rows(session_factory, COMPANY_CHECK)[claim] == row


def test_a_new_claim_is_checked_and_the_unchanged_ones_are_not_read_again(
    session_factory: sessionmaker[Session], store: _CountingStore, companies: dict[str, UUID]
) -> None:
    with session_factory.begin() as session:
        spyre = _document(session, store, "sec:spyre", SPYRE_RELEASE.read_bytes())
        link_company_document(session, companies["spyre"], spyre, DocumentMatch.SEC_CIK)
        _claim(session, spyre)
    check_company_named(session_factory, store, lambda: NOW)
    store.reads.clear()
    with session_factory.begin() as session:
        mirum = _document(session, store, "sec:mirum", MIRUM_RELEASE.read_bytes())
        link_company_document(session, companies["mirum"], mirum, DocumentMatch.SEC_CIK)
        new = _claim(session, mirum, quote="Mirum")
        key = session.execute(
            select(TABLES[SourceDocument].c.snapshot_key).where(TABLES[SourceDocument].c.id == mirum)
        ).scalar_one()
    report = check_company_named(session_factory, store, lambda: NOW)
    assert store.reads == [key]
    assert _rows(session_factory, COMPANY_CHECK)[new]["outcome"] == "pass"
    assert report.claims == 2
    assert report.claims_unchanged == 1


def test_a_claim_linked_to_another_company_is_checked_again(
    session_factory: sessionmaker[Session], store: _CountingStore, companies: dict[str, UUID]
) -> None:
    """A published claim's inputs changed: its document now belongs to another company."""
    with session_factory.begin() as session:
        document = _document(session, store, "sec:spyre", SPYRE_RELEASE.read_bytes())
        link_company_document(session, companies["spyre"], document, DocumentMatch.SEC_CIK)
        claim = _claim(session, document)
    check_company_named(session_factory, store, lambda: NOW)
    with session_factory.begin() as session:
        session.execute(update(TABLES[CompanyDocument]).values(company_id=companies["mirum"]))
    store.reads.clear()
    check_company_named(session_factory, store, lambda: NOW)
    assert len(store.reads) == 1
    reason = _rows(session_factory, COMPANY_CHECK)[claim]["reason"]
    assert reason == "the source does not name Mirum Pharmaceuticals, Inc. or its ticker"


def test_new_check_code_checks_every_claim_again_once_held_and_passing_alike(
    session_factory: sessionmaker[Session],
    store: _CountingStore,
    companies: dict[str, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with session_factory.begin() as session:
        right = _document(session, store, "sec:spyre", SPYRE_RELEASE.read_bytes())
        link_company_document(session, companies["spyre"], right, DocumentMatch.SEC_CIK)
        wrong = _document(session, store, "sec:mirum-as-spyre", MIRUM_RELEASE.read_bytes())
        link_company_document(session, companies["spyre"], wrong, DocumentMatch.SEC_CIK)
        passing, held = _claim(session, right), _claim(session, wrong, quote="Mirum")
    check_company_named(session_factory, store, lambda: NOW)
    assert {c: r["outcome"] for c, r in _rows(session_factory, COMPANY_CHECK).items()} == {
        passing: "pass",
        held: "fail",
    }
    store.reads.clear()
    monkeypatch.setattr(incremental, "check_version", _version("fedcba9876543210"))
    check_company_named(session_factory, store, lambda: NOW)
    assert len(store.reads) == 2  # both documents read again, once
    assert {r["rule_version"] for r in _rows(session_factory, COMPANY_CHECK).values()} == {"fedcba9876543210"}
    store.reads.clear()
    check_company_named(session_factory, store, lambda: NOW)
    assert store.reads == []


def test_a_copy_that_could_not_be_read_is_tried_again_next_run(
    session_factory: sessionmaker[Session], store: _CountingStore, companies: dict[str, UUID], tmp_path: Path
) -> None:
    elsewhere = LocalSnapshotStore(tmp_path / "elsewhere")
    with session_factory.begin() as session:
        document = _document(session, elsewhere, "sec:missing", SPYRE_RELEASE.read_bytes())
        link_company_document(session, companies["spyre"], document, DocumentMatch.SEC_CIK)
        claim = _claim(session, document)
    check_company_named(session_factory, store, lambda: NOW)
    assert _rows(session_factory, COMPANY_CHECK)[claim]["input_digest"] is None
    store.store.put(SPYRE_RELEASE.read_bytes())  # the copy is back
    check_company_named(session_factory, store, lambda: NOW)
    assert _rows(session_factory, COMPANY_CHECK)[claim]["outcome"] == "pass"


def test_a_check_past_its_deadline_stops_and_the_next_run_goes_on(
    session_factory: sessionmaker[Session], store: _CountingStore, companies: dict[str, UUID]
) -> None:
    with session_factory.begin() as session:
        document = _document(session, store, "sec:spyre", SPYRE_RELEASE.read_bytes())
        link_company_document(session, companies["spyre"], document, DocumentMatch.SEC_CIK)
        claim = _claim(session, document)
    report = check_company_named(session_factory, store, lambda: NOW, deadline=0.0, clock=lambda: 1.0)
    assert report.stopped_for_time
    assert report.summary()["stopped_for_time"] is True
    assert store.reads == []
    assert _rows(session_factory, COMPANY_CHECK) == {}
    check_company_named(session_factory, store, lambda: NOW, deadline=2.0, clock=lambda: 1.0)
    assert _rows(session_factory, COMPANY_CHECK)[claim]["outcome"] == "pass"


def test_the_drug_check_skips_unchanged_claims_and_sees_a_new_name_on_file(
    session_factory: sessionmaker[Session], store: _CountingStore, companies: dict[str, UUID]
) -> None:
    with session_factory.begin() as session:
        document = _document(session, store, "sec:spyre", SPYRE_RELEASE.read_bytes())
        link_company_document(session, companies["spyre"], document, DocumentMatch.SEC_CIK)
        claim = _claim(session, document, drug="SPY-9072")
        drug = Drug(company_id=companies["spyre"], preferred_name="SPY-9072")
        session.execute(insert(TABLES[Drug]).values(**drug.model_dump()))
        name = DrugName(drug_id=drug.id, name="SPY-9072", kind=DrugNameKind.CODE)
        session.execute(insert(TABLES[DrugName]).values(**name.model_dump()))
        match = EntityMatch(
            claim_id=claim,
            entity_type=MatchedEntity.DRUG,
            name="SPY-9072",
            status=MatchStatus.RESOLVED,
            matched_id=drug.id,
            matched_by="code_name",
            candidates=[str(drug.id)],
            resolved_at=PUBLISHED,
        )
        session.execute(insert(TABLES[EntityMatch]).values(**match.model_dump()))
    check_drug_named(session_factory, store, lambda: NOW)
    assert _rows(session_factory, DRUG_CHECK)[claim]["outcome"] == "fail"
    store.reads.clear()
    again = check_drug_named(session_factory, store, lambda: NOW)
    assert store.reads == []
    assert again.claims_unchanged == 1
    with session_factory.begin() as session:  # the drug list learns the drug's other name
        also = DrugName(drug_id=drug.id, name="SPY072", kind=DrugNameKind.CODE)
        session.execute(insert(TABLES[DrugName]).values(**also.model_dump()))
    check_drug_named(session_factory, store, lambda: NOW)
    assert len(store.reads) == 1
    assert _rows(session_factory, DRUG_CHECK)[claim]["outcome"] == "pass"


def test_the_type_check_skips_unchanged_claims(
    session_factory: sessionmaker[Session], store: _CountingStore, companies: dict[str, UUID]
) -> None:
    with session_factory.begin() as session:
        document = _document(session, store, "sec:spyre", SPYRE_RELEASE.read_bytes())
        link_company_document(session, companies["spyre"], document, DocumentMatch.SEC_CIK)
        claim = _claim(session, document)
    check_type_named(session_factory, store, lambda: NOW)
    first = _rows(session_factory, TYPE_CHECK)[claim]
    store.reads.clear()
    again = check_type_named(session_factory, store, lambda: NOW)
    assert store.reads == []
    assert again.claims_unchanged == 1
    assert _rows(session_factory, TYPE_CHECK)[claim] == first
