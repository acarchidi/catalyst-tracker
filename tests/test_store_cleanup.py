"""Store clean-up (ADR 0066): only copies of fully read filings that back no claim are deleted.

Written before `catalyst.store_cleanup`, from the owner's rule (2026-11-23): "we have no need to
store them once they are fully read", and ADR 0008/0046: a copy that backs a claim is evidence.
"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.documents import FetchedDocument, store_fetched
from catalyst.db.tables import TABLES
from catalyst.schemas import Claim, ClaimDecision, GateDecision, SourceDocument, SourceTier
from catalyst.schemas.enums import ClaimSubject
from catalyst.snapshots import LocalSnapshotStore, SnapshotNotFoundError, SnapshotStoreError
from catalyst.store_cleanup import clean_store

READ_AT = datetime(2026, 11, 24, 2, tzinfo=UTC)
NOW = datetime(2026, 11, 24, 3, tzinfo=UTC)
BODY = b"<html><body><p>%s quarterly report text that a reader has read in full.</p></body></html>"


def _document(
    store: LocalSnapshotStore,
    factory: sessionmaker[Session],
    name: str,
    kind: str,
    *,
    read: bool = True,
    content: bytes | None = None,
) -> SourceDocument:
    fetched = FetchedDocument(
        source_key=f"sec:0000000001-26-{name}:{name}.htm",
        url=f"https://www.sec.gov/Archives/edgar/data/1/{name}.htm",
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type=kind,
        published_at=datetime(2026, 11, 5, 21, tzinfo=UTC),
        content=content if content is not None else BODY % name.encode(),
        content_type="text/html",
    )
    document, _ = store_fetched(fetched, store, factory)
    if read:
        table = TABLES[SourceDocument]
        with factory.begin() as session:
            session.execute(update(table).where(table.c.id == document.id).values(fully_read_at=READ_AT))
    return document


def _claim(factory: sessionmaker[Session], document: SourceDocument, *, held: bool = False) -> UUID:
    claim = Claim(
        source_document_id=document.id,
        subject_type=ClaimSubject.CATALYST,
        field="catalyst:0123456789abcdef",
        value={"catalyst_type": "topline_readout"},
        quote="quarterly report text",
        quote_start=10,
        quote_end=31,
        agent="extractor",
        prompt_id="extract_catalysts",
        prompt_version="1.0.0",
        provider="anthropic",
        model="claude-sonnet-5-5",
        extracted_at=READ_AT,
    )
    with factory.begin() as session:
        session.execute(insert(TABLES[Claim]).values(**claim.model_dump()))
        if held:
            decision = ClaimDecision(
                claim_id=claim.id,
                decision=GateDecision.REVIEW,
                reasons=["a person must read it"],
                decided_at=NOW,
            )
            session.execute(insert(TABLES[ClaimDecision]).values(**decision.model_dump()))
    return claim.id


def _deleted_at(factory: sessionmaker[Session], document: SourceDocument) -> datetime | None:
    table = TABLES[SourceDocument]
    with factory() as session:
        return session.execute(select(table.c.copy_deleted_at).where(table.c.id == document.id)).scalar_one()


def _kept(store: LocalSnapshotStore, document: SourceDocument) -> bool:
    try:
        store.get(document.snapshot_key)
    except SnapshotNotFoundError:
        return False
    return True


def test_the_default_is_a_dry_run_that_counts_by_document_type_and_deletes_nothing(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    quarterly = _document(store, session_factory, "q", "10-Q")
    _document(store, session_factory, "k", "10-K")
    _document(store, session_factory, "x", "8-K EX-99.2", read=False)
    report = clean_store(session_factory, store, now=lambda: NOW)
    assert report.summary() == {
        "deleting": False,
        "copies_to_delete": 2,
        "copies_deleted": 0,
        "copies_failed": 0,
        "by_document_type": {
            "10-K": {"to_delete": 1},
            "10-Q": {"to_delete": 1},
            "8-K EX-99.2": {"kept_not_fully_read": 1},
        },
    }
    assert _kept(store, quarterly)
    assert _deleted_at(session_factory, quarterly) is None


def test_delete_removes_only_fully_read_copies_that_back_no_claim_and_is_idempotent(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    done = _document(store, session_factory, "q", "10-Q")
    unread = _document(store, session_factory, "u", "10-Q", read=False)
    report = clean_store(session_factory, store, delete=True, now=lambda: NOW)
    assert report.summary()["copies_deleted"] == 1
    assert not _kept(store, done)
    assert _deleted_at(session_factory, done) == NOW
    assert _kept(store, unread)  # never a document not fully read
    assert _deleted_at(session_factory, unread) is None
    again = clean_store(session_factory, store, delete=True, now=lambda: NOW)
    assert again.summary()["copies_deleted"] == 0
    assert again.summary()["by_document_type"]["10-Q"] == {"already_deleted": 1, "kept_not_fully_read": 1}
    assert _deleted_at(session_factory, done) == NOW  # the first time stays the record


@pytest.mark.parametrize("held", [False, True])
def test_a_copy_that_backs_a_claim_is_never_deleted_even_when_the_claim_is_held(
    session_factory: sessionmaker[Session], tmp_path: Path, held: bool
) -> None:
    store = LocalSnapshotStore(tmp_path)
    evidence = _document(store, session_factory, "q", "10-Q")
    _claim(session_factory, evidence, held=held)
    report = clean_store(session_factory, store, delete=True, now=lambda: NOW)
    assert report.summary()["copies_deleted"] == 0
    assert report.summary()["by_document_type"] == {"10-Q": {"kept_backs_a_claim": 1}}
    assert _kept(store, evidence)
    assert _deleted_at(session_factory, evidence) is None


def test_a_copy_shared_with_a_document_that_must_keep_it_stays(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Copies are stored by their hash, so two documents with the same bytes share one copy."""
    store = LocalSnapshotStore(tmp_path)
    same = (
        b"<html><body><p>The same words, filed twice, as an exhibit and as a press release.</p></body></html>"
    )
    exhibit = _document(store, session_factory, "x", "8-K EX-99.2", content=same)
    release = _document(store, session_factory, "r", "8-K EX-99.1", read=False, content=same)
    _claim(session_factory, release)
    report = clean_store(session_factory, store, delete=True, now=lambda: NOW)
    assert report.summary()["by_document_type"] == {"8-K EX-99.2": {"kept_copy_shared": 1}}
    assert _kept(store, exhibit)
    assert _deleted_at(session_factory, exhibit) is None


def test_press_releases_and_other_sources_are_never_touched(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    release = _document(store, session_factory, "r", "8-K EX-99.1")
    registry = _document(store, session_factory, "t", "Registry record")
    report = clean_store(session_factory, store, delete=True, now=lambda: NOW)
    assert report.summary()["by_document_type"] == {}
    assert _kept(store, release)
    assert _kept(store, registry)


def test_a_claim_stored_after_the_count_still_keeps_the_copy(
    session_factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each copy is checked again, under a row lock, just before it is deleted."""
    store = LocalSnapshotStore(tmp_path)
    document = _document(store, session_factory, "q", "10-Q")
    import catalyst.store_cleanup as cleanup

    counted = cleanup.classify

    def count_then_claim(*args: object, **kwargs: object) -> object:
        found = counted(*args, **kwargs)  # pyright: ignore[reportArgumentType]
        _claim(session_factory, document)
        return found

    monkeypatch.setattr(cleanup, "classify", count_then_claim)
    report = clean_store(session_factory, store, delete=True, now=lambda: NOW)
    assert report.summary()["copies_deleted"] == 0
    assert _kept(store, document)
    assert _deleted_at(session_factory, document) is None


def test_a_storage_error_leaves_the_record_and_is_counted(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    document = _document(store, session_factory, "q", "10-Q")

    class Broken(LocalSnapshotStore):
        def delete_copy(self, key: str) -> None:
            raise SnapshotStoreError("storage is down")

    report = clean_store(session_factory, Broken(tmp_path), delete=True, now=lambda: NOW)
    assert report.summary()["copies_failed"] == 1
    assert report.summary()["copies_deleted"] == 0
    assert _deleted_at(session_factory, document) is None
    assert _kept(store, document)
