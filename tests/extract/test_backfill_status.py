"""Whether the backfill's model reads may run, and the extractor's time limit (ADR 0065)."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.alerts import JobRun, build_alerts, load_thresholds
from catalyst.collectors.documents import FetchedDocument, store_fetched
from catalyst.db.tables import TABLES
from catalyst.extract.backfill import CREDIT_FIRST_DAY, CREDIT_USD, DAY_MOST_USD, backfill_status
from catalyst.extract.run import TIME_LIMIT_STOP, extract_documents, waiting_documents
from catalyst.llm.client import Completion, ModelClient
from catalyst.llm.config import ModelConfig, RoleConfig
from catalyst.llm.ledger import InMemoryLedger
from catalyst.llm.pricing import Price, TokenUsage
from catalyst.llm.prompts import Prompt
from catalyst.schemas import ModelCall, ModelCallStatus, SourceTier
from catalyst.snapshots import LocalSnapshotStore

NOW = datetime(2026, 11, 25, 2, tzinfo=UTC)
TODAY = date(2026, 11, 24)  # New York
WORDS = b" ".join([b"word"] * 60)


def _release(
    store: LocalSnapshotStore, factory: sessionmaker[Session], key: str, published: datetime
) -> None:
    fetched = FetchedDocument(
        source_key=f"sec:{key}",
        url=f"https://www.sec.gov/Archives/edgar/data/1/{key}.htm",
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type="8-K EX-99.1",
        published_at=published,
        content=b"<html><body><p>" + key.encode() + b" " + WORDS + b"</p></body></html>",
        content_type="text/html",
    )
    store_fetched(fetched, store, factory)


def _spend(factory: sessionmaker[Session], amount: str, at: datetime, role: str = "extractor") -> None:
    call = ModelCall(
        role=role,
        provider="anthropic",
        model="claude-sonnet-5-5",
        prompt_id="extract_catalysts",
        prompt_version="1.0.0",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=Decimal(amount),
        latency_ms=1,
        request_id=None,
        status=ModelCallStatus.OK,
        called_at=at,
        config_version="test",
    )
    with factory.begin() as session:
        session.execute(insert(TABLES[ModelCall]).values(**call.model_dump()))


def _status(factory: sessionmaker[Session], today: date = TODAY) -> dict[str, object]:
    with factory() as session:
        return backfill_status(session, today).summary()


def test_it_runs_while_releases_wait_the_credit_lasts_and_the_day_is_not_past(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _release(store, session_factory, "a", datetime(2025, 3, 3, 21, tzinfo=UTC))
    _release(store, session_factory, "old", datetime(2024, 12, 30, 21, tzinfo=UTC))  # before 2025
    status = _status(session_factory)
    assert status["active"] is True
    assert status["documents_waiting"] == 1
    assert status["spent_since_start_usd"] == "0.00"


def test_nothing_left_to_read_stops_it(session_factory: sessionmaker[Session]) -> None:
    status = _status(session_factory)
    assert status["active"] is False
    assert status["reason"] == "nothing is left to read"


def test_it_stops_after_the_last_day(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    _release(LocalSnapshotStore(tmp_path), session_factory, "a", datetime(2025, 3, 3, 21, tzinfo=UTC))
    assert _status(session_factory, date(2026, 12, 8))["active"] is True
    late = _status(session_factory, date(2026, 12, 9))
    assert late["active"] is False
    assert "ended on 2026-12-08" in str(late["reason"])


def test_it_stops_before_a_day_that_could_pass_the_credit(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _release(LocalSnapshotStore(tmp_path), session_factory, "a", datetime(2025, 3, 3, 21, tzinfo=UTC))
    start = datetime(2026, 11, 24, 5, 30, tzinfo=UTC)  # 00:30 in New York on the first day
    _spend(session_factory, "500", start - timedelta(hours=1))  # the day before: not the credit's
    fits = CREDIT_USD - DAY_MOST_USD
    _spend(session_factory, str(fits - Decimal("10")), start, role="verifier")
    _spend(session_factory, "10", start + timedelta(days=1))  # every role and job counts
    assert _status(session_factory)["active"] is True
    _spend(session_factory, "0.01", start + timedelta(days=2))
    status = _status(session_factory)
    assert status["active"] is False
    assert status["spent_since_start_usd"] == str(fits + Decimal("0.01"))
    assert "credit" in str(status["reason"])
    assert date(2026, 11, 24) == CREDIT_FIRST_DAY


def test_a_release_read_or_waiting_only_for_a_recheck_is_not_waiting(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _release(store, session_factory, "a", datetime(2025, 3, 3, 21, tzinfo=UTC))
    short = FetchedDocument(
        source_key="sec:short",
        url="https://www.sec.gov/Archives/edgar/data/1/short.htm",
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type="8-K EX-99.1",
        published_at=datetime(2025, 4, 1, 21, tzinfo=UTC),
        content=b"<html><body><p>Too short.</p></body></html>",
        content_type="text/html",
    )
    store_fetched(short, store, session_factory)
    with session_factory() as session:
        assert waiting_documents(session) == 2
    client = ModelClient(_config(), {"fake": _Fake()}, InMemoryLedger(), now=lambda: NOW)
    extract_documents(session_factory, store, client, limit=10, now=lambda: NOW)
    with session_factory() as session:
        assert waiting_documents(session) == 0  # one read, one kept as "no readable text"


@dataclass
class _Fake:
    name: str = "fake"
    calls: list[str] = field(default_factory=list[str])

    def complete(
        self, *, model: str, prompt: Prompt, document: str, max_tokens: int, effort: str | None
    ) -> Completion:
        self.calls.append(document)
        return Completion(
            json.dumps({"catalysts": []}), model, "req", "end_turn", TokenUsage(input=50, output=10)
        )


def _config() -> ModelConfig:
    price = Price(input=Decimal(2), cache_write=Decimal(3), cache_read=Decimal(0), output=Decimal(10))
    role = RoleConfig(provider="fake", model="claude-sonnet-5-5", max_tokens=1000, daily_cap_usd=Decimal(5))
    return ModelConfig(version="test", pricing={"claude-sonnet-5-5": price}, roles={"extractor": role})


def test_the_extractor_starts_no_document_after_its_time_limit(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    for day in (1, 2, 3):
        _release(store, session_factory, f"r{day}", datetime(2025, 3, day, 21, tzinfo=UTC))
    fake = _Fake()
    client = ModelClient(_config(), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    clock = iter([NOW, NOW, NOW, NOW, NOW + timedelta(minutes=30)])
    report = extract_documents(
        session_factory,
        store,
        client,
        limit=10,
        now=lambda: next(clock, NOW + timedelta(hours=1)),
        deadline=NOW + timedelta(minutes=20),
    )
    assert len(fake.calls) == 1  # the newest, 2025-03-03
    assert "r3" in fake.calls[0]
    assert str(report.summary()["stopped_early"]).startswith(TIME_LIMIT_STOP)
    with session_factory() as session:
        assert waiting_documents(session) == 2  # the rest wait for the next run


def test_a_backfill_stop_at_a_cap_is_not_an_alert_but_a_failed_read_is() -> None:
    limits = load_thresholds()

    def alerts(summary: Mapping[str, Any], name: str) -> list[str]:
        run = JobRun(name, "success", summary)
        return [issue["key"] for issue in build_alerts([run], None, limits, "https://x")["issues"]]

    capped = {
        "documents_failed": 0,
        "stopped_early": "extractor has spent",
        "second_reading": {"stopped_early": "x"},
    }
    assert alerts(capped, "backfill-extract") == []
    assert alerts(capped, "extract-catalysts") == [
        "job-failed:extract-catalysts"
    ]  # the nightly job is unchanged
    assert alerts({"documents_failed": 2, "second_reading": {}}, "backfill-extract") == [
        "job-failed:backfill-extract"
    ]
    failed_collection = {"active": True, "sources": [{"source": "sec-backfill:X", "error": "HTTPError"}]}
    assert alerts(failed_collection, "backfill-collect") == ["job-failed:backfill-collect"]
    assert alerts({"active": False, "sources": []}, "backfill-collect") == []
