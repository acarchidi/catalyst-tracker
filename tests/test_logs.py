"""Logs are one JSON object per line, so a machine can read every job's history."""

import json
import logging

import pytest

from catalyst.logs import configure_logging, log_event


def test_log_event_writes_one_json_line_with_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    log_event(logging.getLogger("catalyst.test"), "fetched", url="https://www.sec.gov/x", status=200)
    line = capsys.readouterr().err.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["event"] == "fetched"
    assert record["level"] == "info"
    assert record["logger"] == "catalyst.test"
    assert record["url"] == "https://www.sec.gov/x"
    assert record["status"] == 200
    assert record["ts"].endswith("+00:00")
