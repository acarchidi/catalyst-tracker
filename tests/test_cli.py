"""The command line stops with a plain message when the SEC contact email is missing."""

import json
from collections import Counter
from datetime import date
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
from catalyst.snapshots import SnapshotStoreError


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
    out = tmp_path / "found.json"
    assert main(["extract-catalysts", "--limit", "3", "--since", "2026-10-01", "--out", str(out)]) == 0
    assert calls == [{"limit": 3, "since": date(2026, 10, 1)}]
    assert json.loads(capsys.readouterr().out)["claims_stored"] == 2
    (written,) = json.loads(out.read_text())
    assert written["company"] == "Acme Bio"
    assert written["published"] == "2026-10-14"


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
    monkeypatch.setattr(cli, "evaluate", lambda *_args: (scores, []))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    out = tmp_path / "scores.json"
    assert main(["eval-extractor", "--out", str(out)]) == 0
    record = json.loads(out.read_text())
    assert record["prompt_id"] == "extract_catalysts"
    assert record["scores"]["recall"] == 0.9
    assert json.loads(capsys.readouterr().out) == record
