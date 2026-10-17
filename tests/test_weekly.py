"""The weekly summary counts the week's work in short plain sentences, from stored rows only."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.cli import main
from catalyst.db.repository import save_source_document
from catalyst.db.tables import TABLES
from catalyst.llm.config import load_config
from catalyst.schemas import (
    CheckOutcome,
    Claim,
    ClaimCheck,
    ClaimSubject,
    ModelCall,
    ModelCallStatus,
    SourceDocument,
    SourceTier,
)
from catalyst.weekly import build_weekly

NOW = datetime(2026, 10, 24, 22, 0, tzinfo=UTC)  # Saturday, 6pm in New York
IN_WEEK = NOW - timedelta(days=2)
LAST_WEEK = NOW - timedelta(days=9)


def _document(session_factory: sessionmaker[Session], fill: str, retrieved: datetime) -> UUID:
    document = SourceDocument(
        source_key=f"sec:{fill}",
        url=f"https://www.sec.gov/{fill}.htm",
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type="8-K EX-99.1",
        published_at=retrieved,
        retrieved_at=retrieved,
        sha256=fill * 64,
        snapshot_key=f"sha256/{fill * 64}",
        content_type="text/html",
    )
    with session_factory.begin() as session:
        saved, _ = save_source_document(session, document)
        return saved.id


def _claim(session_factory: sessionmaker[Session], document: UUID, n: int, at: datetime, agent: str) -> UUID:
    claim = Claim(
        source_document_id=document,
        subject_type=ClaimSubject.CATALYST,
        field=f"catalyst:{n:016d}",
        value={"catalyst_type": "topline_readout"},
        quote=f"Quote {n}.",
        quote_start=0,
        quote_end=len(f"Quote {n}."),
        agent=agent,
        prompt_id="extract_catalysts",
        prompt_version="1.0.0",
        provider="anthropic",
        model="m",
        extracted_at=at,
    )
    with session_factory.begin() as session:
        session.execute(insert(TABLES[Claim]).values(**claim.model_dump()))
    return claim.id


def _check(session_factory: sessionmaker[Session], claim: UUID, name: str, outcome: CheckOutcome) -> None:
    row = ClaimCheck(
        claim_id=claim,
        check_name=name,
        outcome=outcome,
        reason=None if outcome is CheckOutcome.PASS else "because",
        checked_at=NOW,
    )
    with session_factory.begin() as session:
        session.execute(insert(TABLES[ClaimCheck]).values(**row.model_dump()))


def _spend(session_factory: sessionmaker[Session], role: str, cost: str, at: datetime) -> None:
    call = ModelCall(
        role=role,
        provider="anthropic",
        model="claude-sonnet-5-5",
        prompt_id="demo",
        prompt_version="1.0.0",
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=Decimal(cost),
        latency_ms=1,
        status=ModelCallStatus.OK,
        called_at=at,
        config_version="test-1",
    )
    with session_factory.begin() as session:
        session.execute(insert(TABLES[ModelCall]).values(**call.model_dump()))


def _week(session_factory: sessionmaker[Session]) -> None:
    fresh = _document(session_factory, "a", IN_WEEK)
    _document(session_factory, "b", IN_WEEK)
    _document(session_factory, "c", LAST_WEEK)
    passing = _claim(session_factory, fresh, 1, IN_WEEK, "extractor")
    failing = _claim(session_factory, fresh, 2, IN_WEEK, "extractor")
    _claim(session_factory, fresh, 3, IN_WEEK, "extractor")  # not checked yet
    _claim(session_factory, fresh, 4, IN_WEEK, "verifier")  # the second reading's own claims do not count
    old = _claim(session_factory, fresh, 5, LAST_WEEK, "extractor")
    _check(session_factory, passing, "window_not_past", CheckOutcome.PASS)
    _check(session_factory, passing, "phase_matches_registry", CheckOutcome.NOT_APPLICABLE)
    _check(session_factory, failing, "window_not_past", CheckOutcome.FAIL)
    _check(session_factory, failing, "names_matched", CheckOutcome.FAIL)
    _check(session_factory, old, "names_matched", CheckOutcome.FAIL)
    _spend(session_factory, "extractor", "1.00", IN_WEEK)
    _spend(session_factory, "extractor", "0.50", IN_WEEK - timedelta(days=1))
    _spend(session_factory, "extractor", "5.00", LAST_WEEK)


def _body(plan: dict[str, Any]) -> str:
    assert plan["mode"] == "weekly"
    assert plan["label"] == "weekly-summary"
    assert len(plan["issues"]) == 1
    return plan["issues"][0]["body"]


def test_the_summary_counts_this_week_only(session_factory: sessionmaker[Session]) -> None:
    _week(session_factory)
    plan = build_weekly(session_factory, load_config(), NOW, [], [])
    issue = plan["issues"][0]
    assert issue["key"] == "weekly-summary:2026-10-24"
    assert "24 October 2026" in issue["title"]
    body = _body(plan)
    assert "Documents collected: 2." in body
    assert "Claims extracted: 3." in body
    assert "Claims passing every check so far: 1 of 3. 1 has not been checked yet." in body
    assert "- The date has not passed: failed on 1 claim." in body
    assert "- Every name matched one record: failed on 1 claim." in body
    assert "- Extractor: $1.50 this week. Most in one day: $1.00 of its $2.00 daily cap." in body
    assert "- Verifier: $0.00 this week." in body
    assert "No open alerts." in body
    assert "No sample yet" in body


def test_an_empty_week_says_so_plainly(session_factory: sessionmaker[Session]) -> None:
    body = _body(build_weekly(session_factory, load_config(), NOW, [], []))
    assert "Documents collected: 0." in body
    assert "No check failed this week." in body


def test_open_alerts_are_listed_with_their_links(session_factory: sessionmaker[Session]) -> None:
    alerts = [{"number": 7, "title": "Collection needs a look", "url": "https://github.com/o/r/issues/7"}]
    body = _body(build_weekly(session_factory, load_config(), NOW, alerts, []))
    assert "Open alerts: 1." in body
    assert "- [Collection needs a look](https://github.com/o/r/issues/7)" in body


def test_a_second_reading_that_is_not_switched_on_is_mentioned_here_only(
    session_factory: sessionmaker[Session],
) -> None:
    problems = [
        "verify_catalysts has no scores.json: run `catalyst eval-extractor --prompt verify_catalysts`"
    ]
    body = _body(build_weekly(session_factory, load_config(), NOW, [], problems))
    assert "The second reading is not switched on yet" in body
    assert "no scores.json" in body
    on = _body(build_weekly(session_factory, load_config(), NOW, [], []))
    assert "not switched on" not in on


def test_the_weekly_command_reads_the_database_and_the_open_alerts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    database_url: str,
    session_factory: sessionmaker[Session],
) -> None:
    monkeypatch.setenv("CATALYST_DATABASE_URL", database_url)
    alerts, out = tmp_path / "open.json", tmp_path / "weekly.json"
    alerts.write_text(json.dumps([{"number": 1, "title": "T", "url": "https://github.com/o/r/issues/1"}]))
    assert main(["weekly-summary", "--open-alerts", str(alerts), "--out", str(out)]) == 0
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert plan["issues"][0]["key"].startswith("weekly-summary:")
    assert "Open alerts: 1." in capsys.readouterr().out
