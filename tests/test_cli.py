"""The command line stops with a plain message when the SEC contact email is missing."""

import json
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import OperationalError

from catalyst import cli
from catalyst.cli import main
from catalyst.collectors.coverage import CoverageEntry, Listing
from catalyst.collectors.sec.press_release import NoPressReleaseError
from catalyst.extract.evaluate import Scores
from catalyst.extract.run import DocumentResult, ExtractionReport
from catalyst.links import LinkReport, LinkResult, LinkStatus
from catalyst.publish.run import DecisionReport
from catalyst.publish.sanity import SafeReport
from catalyst.publish.write import PublishReport
from catalyst.resolve_claims import MatchReport
from catalyst.snapshots import SnapshotStoreError
from catalyst.verify.advisory import AdvisoryReport
from catalyst.verify.company import CompanyReport
from catalyst.verify.run import CheckReport
from catalyst.verify.second_run import SecondReadingReport


def test_missing_sec_contact_email_exits_with_setup_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SEC_CONTACT_EMAIL", raising=False)
    assert main(["sec-press-release", "--cik", "875320"]) == 2
    assert "docs/setup.md Part 5" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (NoPressReleaseError("no 8-K with a press-release exhibit"), "Nothing stored: no 8-K"),
        (httpx.ConnectError("refused"), "the SEC request failed"),
        (OperationalError("SELECT 1", {}, Exception("down")), "cannot reach the database"),
    ],
)
def test_expected_failures_print_one_plain_sentence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: Exception, message: str
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    monkeypatch.setenv("SEC_CONTACT_EMAIL", "data@example.org")
    monkeypatch.setattr(cli, "collect_latest_press_release", fail)
    assert main(["sec-press-release", "--cik", "875320"]) == 1
    err = capsys.readouterr().err
    assert message in err
    assert "Traceback" not in err


@pytest.mark.parametrize("missing", ["SUPABASE_URL", "SUPABASE_SECRET_KEY"])
def test_supabase_store_without_its_settings_names_the_setup_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "data@example.org")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test-only")
    monkeypatch.delenv(missing)
    assert main(["snapshots-verify", "--snapshot-store", "supabase"]) == 2
    err = capsys.readouterr().err
    assert missing in err
    assert "docs/setup.md Part 3" in err
    assert "sb_secret_test-only" not in err


def test_snapshots_verify_prints_the_report_and_fails_on_a_bad_copy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report = cli.VerifyReport(checked=2, missing=["sec:x:gone.htm"], changed=[])
    monkeypatch.setattr(cli, "verify_snapshots", lambda *_args: report)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    assert main(["snapshots-verify"]) == 1
    out = capsys.readouterr().out
    assert '"missing": [\n    "sec:x:gone.htm"' in out


@pytest.fixture(autouse=True)
def _advisory_job_stands_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the advisory-notice job, which needs a database; its own tests use one."""
    monkeypatch.setattr(cli, "check_advisory_meetings", lambda *_args: AdvisoryReport(claims=1))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]


@pytest.fixture(autouse=True)
def _company_job_stands_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the company-named job, which needs a database; its own tests use one."""
    monkeypatch.setattr(cli, "check_company_named", lambda *_args: CompanyReport(claims=2, documents_read=1))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]


@pytest.fixture(autouse=True)
def _safe_to_write_job_stands_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the writer's own check, which needs a database; its own tests use one."""
    monkeypatch.setattr(cli, "check_safe_to_write", lambda *_args: SafeReport(claims=1))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]


class _FakeEngine:
    def dispose(self) -> None:
        pass


def test_coverage_build_needs_the_sec_contact_email(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SEC_CONTACT_EMAIL", raising=False)
    assert main(["coverage-build"]) == 2
    assert "docs/setup.md Part 5" in capsys.readouterr().err


def test_coverage_build_writes_the_list_and_the_review_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    listing = Listing(cik=879169, name="INCYTE CORP", tickers=("INCY",), exchange="Nasdaq")
    entries = [
        CoverageEntry(listing, 8731, sponsors=Counter({"Incyte Corporation": 3})),
        CoverageEntry(Listing(70487, "NRC HEALTH", ("NRC",), "Nasdaq"), 8731),
    ]
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "data@example.org")
    monkeypatch.setattr(cli, "build_coverage", lambda *_args: entries)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    out, review = tmp_path / "coverage.toml", tmp_path / "review.md"
    code = main(
        ["coverage-build", "--out", str(out), "--review", str(review), "--overrides", str(tmp_path / "none")]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"listed": 2, "included": 1, "review": 1}
    assert 'ticker = "INCY"' in out.read_text()
    assert "NRC HEALTH" in review.read_text()


def test_an_unusable_snapshot_store_is_a_plain_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise SnapshotStoreError("the source-snapshots bucket is public")

    monkeypatch.setenv("SEC_CONTACT_EMAIL", "data@example.org")
    monkeypatch.setattr(cli, "_open_store", refuse)
    assert main(["sec-press-release", "--cik", "875320"]) == 1
    err = capsys.readouterr().err
    assert "snapshot store is not usable" in err
    assert "Traceback" not in err


def test_gate1_select_refuses_an_impossible_count(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gate1-select", "--count", "3"]) == 2
    assert "count must be between" in capsys.readouterr().err


def test_gate1_select_writes_fifty_companies(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    out = tmp_path / "gate1.toml"
    assert main(["gate1-select", "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out) == {"companies": 50, "out": str(out)}
    assert out.read_text().count("[[company]]") == 50


def test_check_links_needs_the_sec_contact_email(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SEC_CONTACT_EMAIL", raising=False)
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    assert main(["check-links"]) == 2
    assert "docs/setup.md Part 5" in capsys.readouterr().err


def test_check_links_fails_when_any_link_does_not_work(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report = LinkReport([LinkResult("https://www.sec.gov/x", LinkStatus.BROKEN, "HTTP 404")])
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "data@example.org")
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "check_links", lambda *_args: report)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    assert main(["check-links", "--sample", "5"]) == 1
    assert "0 of 1 links work" in capsys.readouterr().out


def test_sample_records_prints_the_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "sample_records", lambda *_args: [])  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    assert main(["sample-records"]) == 0
    assert capsys.readouterr().out.startswith("| Company | Source |")


def test_coverage_build_reports_an_unreadable_answer_plainly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def garbled(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    monkeypatch.setenv("SEC_CONTACT_EMAIL", "data@example.org")
    monkeypatch.setattr(cli, "build_coverage", garbled)
    out = tmp_path / "coverage.toml"
    assert main(["coverage-build", "--out", str(out), "--overrides", str(tmp_path / "none")]) == 1
    err = capsys.readouterr().err
    assert "a source sent an answer that could not be read" in err
    assert "Traceback" not in err
    assert not out.exists()


def test_extract_catalysts_needs_the_product_key_never_the_coding_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CATALYST_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "coding-session-key")
    assert main(["extract-catalysts"]) == 2
    err = capsys.readouterr().err
    assert "CATALYST_ANTHROPIC_API_KEY" in err
    assert "coding-session-key" not in err


def test_extract_catalysts_writes_the_results_and_prints_the_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    found = DocumentResult(
        "sec:a", "https://www.sec.gov/a.htm", "Acme Bio", date(2026, 10, 14), claims_stored=2
    )
    calls: list[dict[str, Any]] = []

    def fake_extract(*_args: object, **options: Any) -> ExtractionReport:
        calls.append(options)
        return ExtractionReport(documents=[found])

    monkeypatch.setenv("CATALYST_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "extract_documents", fake_extract)
    monkeypatch.setattr(cli, "resolve_claims", lambda *_args: MatchReport(claims=2))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "run_checks", lambda *_args: CheckReport(claims=2, claims_passing=1))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "run_second_reading", lambda *_args, **_options: SecondReadingReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "decide_claims", lambda *_args: DecisionReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "publish_claims", lambda *_args, **_kw: PublishReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    out = tmp_path / "found.json"
    assert main(["extract-catalysts", "--limit", "3", "--since", "2026-10-01", "--out", str(out)]) == 0
    assert calls == [{"limit": 3, "since": date(2026, 10, 1)}]
    printed = json.loads(capsys.readouterr().out)
    assert printed["claims_stored"] == 2
    assert printed["matches"]["claims"] == 2  # names in new claims are matched right after extraction
    assert printed["checks"]["claims_passing"] == 1  # and then checked
    (written,) = json.loads(out.read_text())
    assert written["company"] == "Acme Bio"
    assert written["published"] == "2026-10-14"


def test_extract_catalysts_runs_the_second_reading_last_with_the_verifier_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR 0018, part 2: the second reading runs after matching and the rule checks."""
    order: list[str] = []
    asked: dict[str, Any] = {}

    def second(*args: object, **options: Any) -> SecondReadingReport:
        order.append("second")
        asked.update(options, config=args[3])
        return SecondReadingReport(skipped=["no scores yet"])

    def checks(*_args: object) -> CheckReport:
        order.append("checks")
        return CheckReport()

    monkeypatch.setenv("CATALYST_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "extract_documents", lambda *_args, **_options: ExtractionReport(documents=[]))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "run_second_reading", second)
    monkeypatch.setattr(cli, "decide_claims", lambda *_args: DecisionReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "publish_claims", lambda *_args, **_kw: PublishReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "resolve_claims", lambda *_args: MatchReport(claims=0))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "run_checks", checks)
    assert main(["extract-catalysts", "--limit", "4"]) == 0  # skipped is not a failure
    assert order == ["checks", "second"]
    assert asked["limit"] == 4
    assert asked["config"].roles["verifier"].model == "claude-sonnet-5-5"
    assert json.loads(capsys.readouterr().out)["second_reading"]["skipped"] == ["no scores yet"]


@pytest.mark.parametrize(
    ("second", "expected"),
    [
        ({"documents_failed": 1}, 1),
        ({"stopped": "verifier at its daily cap"}, 1),
        ({}, 0),
    ],
    ids=["a failed release", "the spending cap", "a clean run"],
)
def test_the_second_readings_failures_fail_the_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], second: dict[str, Any], expected: int
) -> None:
    monkeypatch.setenv("CATALYST_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "extract_documents", lambda *_args, **_options: ExtractionReport(documents=[]))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "run_second_reading", lambda *_args, **_options: SecondReadingReport(**second))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "decide_claims", lambda *_args: DecisionReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "publish_claims", lambda *_args, **_kw: PublishReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "resolve_claims", lambda *_args: MatchReport(claims=0))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "run_checks", lambda *_args: CheckReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    assert main(["extract-catalysts"]) == expected
    capsys.readouterr()


def test_a_bad_scores_file_stops_only_the_second_reading(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review of PR #34: matching and the rule checks still run and print; the command fails."""

    def broken(*_args: object, **_options: object) -> SecondReadingReport:
        raise json.JSONDecodeError("Expecting value", "", 0)

    monkeypatch.setenv("CATALYST_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "extract_documents", lambda *_args, **_options: ExtractionReport(documents=[]))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "run_second_reading", broken)
    monkeypatch.setattr(cli, "decide_claims", lambda *_args: DecisionReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "publish_claims", lambda *_args, **_kw: PublishReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "resolve_claims", lambda *_args: MatchReport(claims=3))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "run_checks", lambda *_args: CheckReport(claims=3))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    assert main(["extract-catalysts"]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert (summary["matches"]["claims"], summary["checks"]["claims"]) == (3, 3)
    assert summary["second_reading"] == {"error": "JSONDecodeError: the second reading did not finish"}


def test_extract_catalysts_decides_last_and_a_failed_decision_fails_only_that_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR 0018, part 5a: decisions read every check, so they run after the second reading."""
    order: list[str] = []

    def step(name: str, result: object) -> Any:
        def run(*_args: object, **_options: object) -> object:
            order.append(name)
            return result

        return run

    def broken(*_args: object) -> DecisionReport:
        order.append("decide")
        raise OperationalError("SELECT", {}, Exception("connection lost"))

    monkeypatch.setenv("CATALYST_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "extract_documents", lambda *_args, **_options: ExtractionReport(documents=[]))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "resolve_claims", step("match", MatchReport(claims=2)))
    monkeypatch.setattr(cli, "run_checks", step("checks", CheckReport(claims=2)))
    monkeypatch.setattr(cli, "check_advisory_meetings", step("advisory", AdvisoryReport(claims=2)))
    monkeypatch.setattr(cli, "run_second_reading", step("second", SecondReadingReport()))
    monkeypatch.setattr(cli, "decide_claims", step("decide", DecisionReport(claims=2, to_review=2)))
    monkeypatch.setattr(cli, "publish_claims", step("publish", PublishReport()))
    assert main(["extract-catalysts"]) == 0
    assert order == ["match", "checks", "advisory", "second", "decide", "publish"]
    assert json.loads(capsys.readouterr().out)["decisions"]["to_review"] == 2
    monkeypatch.setattr(cli, "decide_claims", broken)
    assert main(["extract-catalysts"]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["checks"]["claims"] == 2  # earlier steps stand
    assert summary["decisions"] == {"error": "OperationalError: the publish decisions did not finish"}
    assert summary["published"] == {
        "skipped": "the publish decisions did not finish, so nothing was published"
    }


@pytest.mark.parametrize(
    "failing", ["resolve_claims", "run_checks", "check_advisory_meetings", "check_company_named"]
)
def test_extract_catalysts_decides_nothing_when_a_check_step_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failing: str
) -> None:
    """Opus review of PR #52: a decision never rests on check rows this run did not renew."""
    decided: list[object] = []

    def broken(*_args: object) -> object:
        raise OperationalError("SELECT", {}, Exception("connection lost"))

    monkeypatch.setenv("CATALYST_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "extract_documents", lambda *_args, **_options: ExtractionReport(documents=[]))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "resolve_claims", lambda *_args: MatchReport(claims=1))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "run_checks", lambda *_args: CheckReport(claims=1))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "check_advisory_meetings", lambda *_args: AdvisoryReport(claims=1))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "run_second_reading", lambda *_args, **_options: SecondReadingReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "decide_claims", lambda *args: decided.append(args) or DecisionReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType, reportUnknownMemberType]
    monkeypatch.setattr(cli, failing, broken)
    main(["extract-catalysts"])
    assert decided == []
    summary = json.loads(capsys.readouterr().out)
    assert summary["decisions"] == {"skipped": "the checks did not finish, so nothing was decided"}


def test_decide_claims_prints_the_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[object] = []

    def decide(_factory: object, store: object, _now: object) -> DecisionReport:
        seen.append(store)
        report = DecisionReport(claims=3, to_publish=1, to_review=2)
        report.reasons.update({"advisory_meeting_noticed has not run": 2})
        return report

    monkeypatch.setenv("CATALYST_DATABASE_URL", "postgresql+psycopg://localhost/unused_test")
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "decide_claims", decide)
    assert main(["decide-claims", "--snapshot-dir", "var/test-snapshots"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (printed["claims"], printed["to_publish"], printed["to_review"]) == (3, 1, 2)
    assert printed["top_reasons"] == {"advisory_meeting_noticed has not run": 2}
    assert len(seen) == 1


def test_extract_catalysts_publishes_last_and_a_failed_writer_fails_only_that_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR 0018, part 5b: the writer runs after the decisions, under its own key."""
    order: list[str] = []

    def decide(*_args: object) -> DecisionReport:
        order.append("decide")
        return DecisionReport(claims=2, to_publish=1, to_review=1)

    def publish(*_args: object, **_kw: object) -> PublishReport:
        order.append("publish")
        return PublishReport(claims_marked_publish=1, catalysts_created=1)

    def broken(*_args: object, **_kw: object) -> PublishReport:
        raise OperationalError("INSERT", {}, Exception("connection lost"))

    monkeypatch.setenv("CATALYST_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "extract_documents", lambda *_args, **_options: ExtractionReport(documents=[]))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "resolve_claims", lambda *_args: MatchReport(claims=2))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "run_checks", lambda *_args: CheckReport(claims=2))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "check_advisory_meetings", lambda *_args: AdvisoryReport(claims=2))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "run_second_reading", lambda *_args, **_options: SecondReadingReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "decide_claims", decide)
    monkeypatch.setattr(cli, "publish_claims", publish)
    assert main(["extract-catalysts"]) == 0
    assert order == ["decide", "publish"]
    assert json.loads(capsys.readouterr().out)["published"]["catalysts_created"] == 1
    monkeypatch.setattr(cli, "publish_claims", broken)
    assert main(["extract-catalysts"]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["decisions"]["to_publish"] == 1  # earlier summaries stand
    assert summary["published"] == {"error": "OperationalError: publishing did not finish"}


def test_publish_claims_prints_the_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    models: list[str] = []

    def publish(_factory: object, _store: object, _now: object, *, verifier_model: str) -> PublishReport:
        models.append(verifier_model)  # only the configured verifier's readings vouch (O2)
        report = PublishReport(claims_marked_publish=3, catalysts_created=1, held_on_recheck=2)
        report.reasons.update({"window_not_past failed": 2})
        return report

    monkeypatch.setenv("CATALYST_DATABASE_URL", "postgresql+psycopg://localhost/unused_test")
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "publish_claims", publish)
    assert main(["publish-claims", "--snapshot-dir", "var/test-snapshots"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (printed["claims_marked_publish"], printed["catalysts_created"], printed["held_on_recheck"]) == (
        3,
        1,
        2,
    )
    assert printed["top_reasons"] == {"window_not_past failed": 2}
    assert models == ["claude-sonnet-5-5"]


def test_publish_claims_with_the_supabase_store_names_the_missing_setting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CATALYST_DATABASE_URL", "postgresql+psycopg://localhost/unused_test")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    assert main(["publish-claims", "--snapshot-store", "supabase"]) == 2
    assert "SUPABASE_URL" in capsys.readouterr().err


def test_decide_claims_help_says_its_publish_count_is_not_final(capsys: pytest.CaptureFixture[str]) -> None:
    """Claude review of PR #52: the writer checks again, so "publish" there is not the final word."""
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "publish count is not final" in " ".join(capsys.readouterr().out.split())


def test_decide_claims_with_the_supabase_store_names_the_missing_setting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CATALYST_DATABASE_URL", "postgresql+psycopg://localhost/unused_test")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    assert main(["decide-claims", "--snapshot-store", "supabase"]) == 2
    assert "SUPABASE_URL" in capsys.readouterr().err


def test_eval_extractor_needs_the_product_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.delenv("CATALYST_ANTHROPIC_API_KEY", raising=False)
    assert main(["eval-extractor", "--out", str(tmp_path / "s.json")]) == 2
    assert "CATALYST_ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_eval_extractor_writes_a_scores_record(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    scores = Scores(recall=0.9, precision=0.8, catalyst_type=1.0, window=0.95, drug=1.0, quote_found=1.0)
    monkeypatch.setenv("CATALYST_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "evaluate", lambda *_args, **_options: (scores, []))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    out = tmp_path / "scores.json"
    assert main(["eval-extractor", "--out", str(out)]) == 0
    record = json.loads(out.read_text())
    assert record["prompt_id"] == "extract_catalysts"
    assert record["scores"]["recall"] == 0.9
    assert json.loads(capsys.readouterr().out) == record


def test_eval_extractor_scores_the_second_reading_with_its_own_role_and_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    scores = Scores(recall=0.9, precision=0.8, catalyst_type=1.0, window=0.95, drug=1.0, quote_found=1.0)
    asked: dict[str, object] = {}

    def scored(*_args: object, **options: object) -> tuple[Scores, list[object]]:
        asked.update(options)
        return scores, []

    monkeypatch.setenv("CATALYST_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "evaluate", scored)
    out = tmp_path / "scores.json"
    assert main(["eval-extractor", "--prompt", "verify_catalysts", "--out", str(out)]) == 0
    capsys.readouterr()
    assert asked == {"role": "verifier", "prompt_ref": ("verify_catalysts", "1.0.0")}
    record = json.loads(out.read_text())
    assert (record["prompt_id"], record["model"]) == ("verify_catalysts", "claude-sonnet-5-5")


def test_eval_extractor_without_a_database_setting_says_so_plainly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Review of PR #17: a missing setting gives the plain message and exit code 2, not a traceback."""
    monkeypatch.setenv("CATALYST_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("CATALYST_DATABASE_URL", raising=False)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://postgres:[YOUR-PASSWORD]@h:5432/postgres")
    monkeypatch.delenv("SUPABASE_DB_PASSWORD", raising=False)
    assert main(["eval-extractor", "--out", str(tmp_path / "s.json")]) == 2
    assert "SUPABASE_DB_PASSWORD" in capsys.readouterr().err


def test_resolve_claims_prints_the_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report = MatchReport(claims=3, drugs_created=1)
    report.statuses["drug:resolved"] = 3
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "resolve_claims", lambda *_args: report)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    assert main(["resolve-claims"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "claims": 3,
        "drugs_created": 1,
        "failed": 0,
        "matches": {"drug:resolved": 3},
    }


def test_extract_catalysts_still_prints_its_counts_when_matching_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sqlalchemy.exc import OperationalError

    def broken(*_args: object) -> MatchReport:
        raise OperationalError("select", {}, Exception("database went away"))

    monkeypatch.setenv("CATALYST_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "extract_documents", lambda *_args, **_options: ExtractionReport(documents=[]))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "resolve_claims", broken)
    monkeypatch.setattr(cli, "run_second_reading", lambda *_args, **_options: SecondReadingReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "decide_claims", lambda *_args: DecisionReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "publish_claims", lambda *_args, **_kw: PublishReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    assert main(["extract-catalysts"]) == 0
    assert "matching did not finish" in json.loads(capsys.readouterr().out)["matches"]["error"]


def test_extract_catalysts_keeps_the_match_counts_when_only_the_checks_fail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sqlalchemy.exc import OperationalError

    def broken(*_args: object) -> CheckReport:
        raise OperationalError("select", {}, Exception("database went away"))

    monkeypatch.setenv("CATALYST_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "extract_documents", lambda *_args, **_options: ExtractionReport(documents=[]))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "resolve_claims", lambda *_args: MatchReport(claims=2))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "run_checks", broken)
    monkeypatch.setattr(cli, "run_second_reading", lambda *_args, **_options: SecondReadingReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "decide_claims", lambda *_args: DecisionReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "publish_claims", lambda *_args, **_kw: PublishReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    assert main(["extract-catalysts"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["matches"]["claims"] == 2
    assert summary["checks"] == {"error": "OperationalError: checks did not finish"}


def test_verify_claims_prints_the_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "run_checks", lambda *_args: CheckReport(claims=4, claims_passing=3))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    assert main(["verify-claims"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "claims": 4,
        "claims_passing": 3,
        "outcomes": {},
        "advisory": {"claims": 1, "outcomes": {}},
        "company": {"claims": 2, "documents_read": 1, "documents_unreadable": 0, "outcomes": {}},
        "safe_to_write": {"claims": 1, "outcomes": {}},  # the writer's own check runs here too
    }


def _company_check_breaks(*_args: object) -> CompanyReport:
    raise OperationalError("select", {}, Exception("database went away"))


def test_a_failed_company_check_keeps_the_rule_check_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "run_checks", lambda *_args: CheckReport(claims=4, claims_passing=3))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "check_company_named", _company_check_breaks)
    assert main(["verify-claims"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (printed["claims"], printed["claims_passing"]) == (4, 3)
    assert printed["advisory"] == {"claims": 1, "outcomes": {}}
    assert printed["company"] == {"error": "OperationalError: the company check did not finish"}


def test_extract_catalysts_runs_the_company_check_after_the_rule_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CATALYST_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "extract_documents", lambda *_args, **_options: ExtractionReport(documents=[]))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "resolve_claims", lambda *_args: MatchReport(claims=2))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "run_checks", lambda *_args: CheckReport(claims=2, claims_passing=1))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "run_second_reading", lambda *_args, **_options: SecondReadingReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "decide_claims", lambda *_args: DecisionReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "publish_claims", lambda *_args, **_kw: PublishReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    assert main(["extract-catalysts"]) == 0
    assert json.loads(capsys.readouterr().out)["checks"]["company"]["documents_read"] == 1
    monkeypatch.setattr(cli, "check_company_named", _company_check_breaks)
    assert main(["extract-catalysts"]) == 0
    checks = json.loads(capsys.readouterr().out)["checks"]
    assert (checks["claims_passing"], checks["advisory"]["claims"]) == (1, 1)
    assert checks["company"] == {"error": "OperationalError: the company check did not finish"}


def test_prospects_check_prints_the_counts_of_the_committed_list(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["prospects-check"]) == 0
    counts = json.loads(capsys.readouterr().out)
    assert counts["rows"] == 209
    assert counts["with_email"] == 115
    assert counts["categories"]["media"] == 49
    assert counts["statuses"] == {
        "new": 209,
        "approved": 0,
        "contacted": 0,
        "replied": 0,
        "declined": 0,
        "do_not_contact": 0,
    }


_PROSPECT_HEADER = (
    "category,name,organization,role,email,contact_page_url,source_url,"
    "source_checked_on,notes,status,status_changed_on\n"
)


def test_prospects_check_fails_on_a_bad_row_and_names_its_line(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = tmp_path / "p.csv"
    path.write_text(
        _PROSPECT_HEADER + "media,Jane,Example,Reporter,jane@gmail.com,https://a.com/c,https://a.com/c,"
        "2026-10-17,,new,\n",
        encoding="utf-8",
    )
    assert main(["prospects-check", "--file", str(path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "line 2: email" in captured.err
    assert "Traceback" not in captured.err


def test_prospects_check_fails_when_a_do_not_contact_row_was_removed(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    previous = tmp_path / "before.csv"
    previous.write_text(
        _PROSPECT_HEADER + "media,Jane,Example,Reporter,,https://a.com/c,https://a.com/c,"
        "2026-10-17,,do_not_contact,2026-10-20\n",
        encoding="utf-8",
    )
    current = tmp_path / "after.csv"
    current.write_text(_PROSPECT_HEADER, encoding="utf-8")
    assert main(["prospects-check", "--file", str(current), "--previous", str(previous)]) == 1
    assert "Example, Jane: a do_not_contact row was removed" in capsys.readouterr().err


def test_alerts_solves_the_spending_could_not_be_read_alert_once_spending_reads(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    spend = {"extractor": cli.RoleSpend(Decimal("0.10"), Decimal("5.00"), Decimal("0.01"))}
    monkeypatch.setattr(cli, "_spend_today", lambda: spend)
    out = tmp_path / "plan.json"
    assert main(["alerts", "--spend", "--run-url", "https://example.org/run/1", "--out", str(out)]) == 0
    capsys.readouterr()
    plan = json.loads(out.read_text(encoding="utf-8"))
    solved = {item["key"] for item in plan["solved"]}
    assert "job-failed:spend-check" in solved
    assert "spend-cap:extractor" in solved
    assert plan["issues"] == []


def test_alerts_raises_the_spending_could_not_be_read_alert_when_the_read_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def fail() -> dict[str, Any]:
        raise OperationalError("SELECT 1", {}, Exception("no database"))

    monkeypatch.setattr(cli, "_spend_today", fail)
    out = tmp_path / "plan.json"
    assert main(["alerts", "--spend", "--run-url", "https://example.org/run/1", "--out", str(out)]) == 0
    capsys.readouterr()
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert [issue["key"] for issue in plan["issues"]] == ["job-failed:spend-check"]
    assert "job-failed:spend-check" not in {item["key"] for item in plan["solved"]}


def test_extract_catalysts_runs_the_writers_check_before_deciding_and_decides_nothing_if_it_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR 0018, part 5b: decisions read `safe_to_write`, so they wait for it and skip if it failed."""
    order: list[str] = []

    def safe(*_args: object) -> SafeReport:
        order.append("safe")
        return SafeReport(claims=2)

    def decide(*_args: object) -> DecisionReport:
        order.append("decide")
        return DecisionReport(claims=2)

    def broken(*_args: object) -> SafeReport:
        raise OperationalError("SELECT", {}, Exception("connection lost"))

    monkeypatch.setenv("CATALYST_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "create_engine", lambda *_args: _FakeEngine())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "extract_documents", lambda *_args, **_options: ExtractionReport(documents=[]))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "resolve_claims", lambda *_args: MatchReport(claims=2))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "run_checks", lambda *_args: CheckReport(claims=2))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "run_second_reading", lambda *_args, **_options: SecondReadingReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "publish_claims", lambda *_args, **_kw: PublishReport())  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(cli, "check_safe_to_write", safe)
    monkeypatch.setattr(cli, "decide_claims", decide)
    assert main(["extract-catalysts"]) == 0
    assert order == ["safe", "decide"]
    assert json.loads(capsys.readouterr().out)["checks"]["safe_to_write"] == {"claims": 2, "outcomes": {}}
    monkeypatch.setattr(cli, "check_safe_to_write", broken)
    order.clear()
    main(["extract-catalysts"])
    summary = json.loads(capsys.readouterr().out)
    assert order == []
    assert summary["checks"]["safe_to_write"] == {
        "error": "OperationalError: the safe-to-write check did not finish"
    }
    assert summary["decisions"] == {"skipped": "the checks did not finish, so nothing was decided"}
