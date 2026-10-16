"""The command line stops with a plain message when the SEC contact email is missing."""

from typing import Any

import httpx
import pytest
from sqlalchemy.exc import OperationalError

from catalyst import cli
from catalyst.cli import main
from catalyst.collectors.sec.press_release import NoPressReleaseError


def test_missing_sec_contact_email_exits_with_setup_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SEC_CONTACT_EMAIL", raising=False)
    assert main(["sec-press-release", "--cik", "875320"]) == 2
    assert "docs/setup.md Part 4" in capsys.readouterr().err


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
