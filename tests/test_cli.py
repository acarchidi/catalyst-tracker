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
