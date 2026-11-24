"""The backfill's commands (ADR 0065): a stop at a cap or the time limit ends a backfill run cleanly."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from catalyst import cli
from catalyst.cli import main
from catalyst.collectors.documents import CollectionStats
from catalyst.collectors.sec.backfill import BackfillReport
from catalyst.extract.filings import FilingReport
from catalyst.extract.indicate import IndicationReport
from catalyst.extract.run import ExtractionReport
from catalyst.publish.indication import IndicationWrites
from catalyst.publish.run import DecisionReport
from catalyst.publish.sanity import SafeReport
from catalyst.publish.write import PublishReport
from catalyst.resolve_claims import MatchReport
from catalyst.store_cleanup import CleanupReport
from catalyst.verify.advisory import AdvisoryReport
from catalyst.verify.company import CompanyReport
from catalyst.verify.drug import DrugReport
from catalyst.verify.event_type import TypeReport
from catalyst.verify.run import CheckReport
from catalyst.verify.second_run import SecondReadingReport

NOW = datetime(2026, 11, 25, 2, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _filing_jobs_stand_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the filing reader and the store clean-up (ADR 0066), which need a database."""
    monkeypatch.setattr(cli, "read_filings", lambda *_args, **_kw: FilingReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "clean_store", lambda *_args, **_kw: CleanupReport(deleting=True))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]


class _FakeEngine:
    def dispose(self) -> None:
        pass

    def connect(self) -> None:
        """Refuse to connect: the copy prefetch (ADR 0064) finds no database in these tests."""
        raise OperationalError("connect", {}, Exception("no database in this test"))


def _stand_in_for_the_pipeline(monkeypatch: pytest.MonkeyPatch, stopped: str | None) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def extract(*_args: object, **options: Any) -> ExtractionReport:
        calls.append(options)
        return ExtractionReport(stopped=stopped)

    def second(*_args: object, **_options: Any) -> SecondReadingReport:
        return SecondReadingReport(stopped=stopped)

    stand_ins: dict[str, Any] = {
        "create_engine": lambda *_a: _FakeEngine(),  # pyright: ignore[reportUnknownLambdaType]
        "extract_documents": extract,
        "resolve_claims": lambda *_a: MatchReport(),  # pyright: ignore[reportUnknownLambdaType]
        "run_checks": lambda *_a: CheckReport(),  # pyright: ignore[reportUnknownLambdaType]
        "check_advisory_meetings": lambda *_a: AdvisoryReport(),  # pyright: ignore[reportUnknownLambdaType]
        "check_company_named": lambda *_a: CompanyReport(),  # pyright: ignore[reportUnknownLambdaType]
        "check_drug_named": lambda *_a: DrugReport(),  # pyright: ignore[reportUnknownLambdaType]
        "check_type_named": lambda *_a: TypeReport(),  # pyright: ignore[reportUnknownLambdaType]
        "check_safe_to_write": lambda *_a: SafeReport(),  # pyright: ignore[reportUnknownLambdaType]
        "run_second_reading": second,
        "decide_claims": lambda *_a: DecisionReport(),  # pyright: ignore[reportUnknownLambdaType]
        "publish_claims": lambda *_a, **_k: PublishReport(),  # pyright: ignore[reportUnknownLambdaType]
        "indicate_catalysts": lambda *_a, **_k: IndicationReport(),  # pyright: ignore[reportUnknownLambdaType]
        "publish_indications": lambda *_a, **_k: IndicationWrites(),  # pyright: ignore[reportUnknownLambdaType]
        "_utc_now": lambda: NOW,
    }
    for name, value in stand_ins.items():
        monkeypatch.setattr(cli, name, value)
    monkeypatch.setenv("CATALYST_ANTHROPIC_API_KEY", "test-key")
    return calls


def test_a_backfill_run_stopped_by_a_cap_ends_cleanly_and_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _stand_in_for_the_pipeline(monkeypatch, "extractor has spent $7.90 of its $8.00 daily cap")
    argv = ["extract-catalysts", "--since", "2025-01-01", "--limit", "400", "--max-minutes", "170"]
    assert main([*argv, "--stop-is-expected"]) == 0
    assert calls[0]["deadline"] == NOW + timedelta(minutes=170)
    printed = json.loads(capsys.readouterr().out)
    assert printed["stopped_early"].startswith("extractor has spent")  # still reported
    assert main(argv) == 1  # without the flag a stop is a failure, as for the nightly job


def test_a_backfill_run_with_a_failed_read_still_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _stand_in_for_the_pipeline(monkeypatch, None)

    def failing(*_args: object, **_options: Any) -> SecondReadingReport:
        return SecondReadingReport(documents_failed=1)

    monkeypatch.setattr(cli, "run_second_reading", failing)
    assert main(["extract-catalysts", "--stop-is-expected"]) == 1


def test_backfill_collect_prints_its_counts_and_fails_on_a_company_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asked: dict[str, Any] = {}

    def backfill(companies: list[Any], *_args: object, **options: Any) -> BackfillReport:
        asked.update(options, companies=len(companies))
        return BackfillReport(
            active=True, sources=[CollectionStats("sec-backfill:X", error="HTTPError: 500")]
        )

    monkeypatch.setenv("SEC_CONTACT_EMAIL", "data@example.org")
    monkeypatch.setattr(cli, "create_engine", lambda *_a: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "backfill_press_releases", backfill)
    assert main(["backfill-collect", "--max-minutes", "90"]) == 1
    assert asked["companies"] == 466  # every covered company by default
    assert asked["max_minutes"] == 90
    assert json.loads(capsys.readouterr().out)["sources"][0]["error"] == "HTTPError: 500"
