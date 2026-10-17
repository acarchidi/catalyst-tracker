"""The extraction job on a disposable database: claims stored once, spend caps respected."""

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.documents import FetchedDocument, store_fetched
from catalyst.db.tables import TABLES
from catalyst.extract.run import extract_documents
from catalyst.llm.client import Completion, ModelClient
from catalyst.llm.config import ModelConfig, RoleConfig
from catalyst.llm.ledger import InMemoryLedger
from catalyst.llm.pricing import Price, TokenUsage
from catalyst.llm.prompts import Prompt
from catalyst.schemas import Claim, SourceTier
from catalyst.snapshots import LocalSnapshotStore

RELEASE = (
    b"<html><body><p>Acme Bio Reports Third Quarter 2026 Results</p>"
    b"<p>Topline data from the Phase 3 RAINIER trial are expected in the second half of 2027.</p>"
    b"</body></html>"
)
ANSWER = {
    "catalysts": [
        {
            "catalyst_type": "topline_readout",
            "title": "Phase 3 RAINIER topline data",
            "drug": None,
            "indication": None,
            "date_text": "second half of 2027",
            "quote": "Topline data from the Phase 3 RAINIER trial are expected in the second half of 2027.",
        }
    ]
}
NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


def _config(cap: str = "5") -> ModelConfig:
    price = Price(input=Decimal(2), cache_write=Decimal(3), cache_read=Decimal(0), output=Decimal(10))
    role = RoleConfig(provider="fake", model="claude-sonnet-5-5", max_tokens=1000, daily_cap_usd=Decimal(cap))
    return ModelConfig(version="test", pricing={"claude-sonnet-5-5": price}, roles={"extractor": role})


@dataclass
class _Fake:
    name: str = "fake"
    calls: list[str] = field(default_factory=list[str])

    def complete(
        self, *, model: str, prompt: Prompt, document: str, max_tokens: int, effort: str | None
    ) -> Completion:
        self.calls.append(document)
        return Completion(json.dumps(ANSWER), model, "req", "end_turn", TokenUsage(input=500, output=100))


def _document(store: LocalSnapshotStore, factory: sessionmaker[Session], key: str, kind: str) -> None:
    fetched = FetchedDocument(
        source_key=f"sec:{key}",
        url=f"https://www.sec.gov/Archives/edgar/data/1/{key}.htm",
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type=kind,
        published_at=datetime(2026, 10, 14, 20, tzinfo=UTC),
        content=RELEASE.replace(b"Acme", key.encode()),
        content_type="text/html",
    )
    store_fetched(fetched, store, factory)


def _claims(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return session.execute(select(func.count()).select_from(TABLES[Claim])).scalar_one()


def test_press_releases_become_checked_claims_once(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _document(store, session_factory, "a", "8-K EX-99.1")
    _document(store, session_factory, "b", "10-Q")
    fake = _Fake()
    client = ModelClient(_config(), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    report = extract_documents(session_factory, store, client, limit=10, now=lambda: NOW)
    assert len(fake.calls) == 1  # the 10-Q is not a press release
    assert fake.calls[0].startswith("Publication date: 2026-10-14\n\n")
    assert report.summary() == {
        "documents_read": 1,
        "documents_failed": 0,
        "documents_skipped_already_done": 0,
        "catalysts_proposed": 1,
        "catalysts_passing_every_check": 1,
        "claims_stored": 1,
        "stopped_early": None,
    }
    assert report.documents[0].catalysts[0]["window_start"] == "2027-07-01"
    again = extract_documents(session_factory, store, client, limit=10, now=lambda: NOW)
    assert len(fake.calls) == 1
    assert again.summary()["documents_skipped_already_done"] == 1
    assert _claims(session_factory) == 1


def test_a_role_at_its_cap_stops_the_job_and_says_so(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _document(store, session_factory, "a", "8-K EX-99.1")
    fake = _Fake()
    client = ModelClient(_config(cap="0.0001"), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    report = extract_documents(session_factory, store, client, limit=10)
    assert fake.calls == []
    assert report.stopped is not None
    assert "daily cap" in report.stopped
    assert report.documents == []
    assert _claims(session_factory) == 0


def test_a_missing_copy_is_reported_and_the_job_goes_on(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _document(store, session_factory, "a", "8-K EX-99.1")
    _document(store, session_factory, "b", "8-K EX-99.1")
    for copy in tmp_path.rglob("*"):
        if copy.is_file() and b"b Bio Reports" in copy.read_bytes():
            copy.unlink()
    fake = _Fake()
    client = ModelClient(_config(), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    report = extract_documents(session_factory, store, client, limit=10, since=date(2026, 10, 1))
    errors = [d.error for d in report.documents if d.error]
    assert len(errors) == 1
    assert errors[0].startswith("SnapshotNotFoundError")
    assert report.summary()["claims_stored"] == 1


@pytest.mark.parametrize("since", [date(2026, 10, 15)])
def test_documents_before_since_are_left_alone(
    session_factory: sessionmaker[Session], tmp_path: Path, since: date
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _document(store, session_factory, "a", "8-K EX-99.1")
    fake = _Fake()
    client = ModelClient(_config(), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    assert extract_documents(session_factory, store, client, limit=10, since=since).documents == []
