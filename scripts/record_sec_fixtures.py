"""Record the SEC responses the walking skeleton needs, as test fixtures.

Runs the real collector once against live EDGAR and saves every response it received to
`tests/fixtures/sec/<cik>/`, with `responses.json` mapping each URL to its saved file.
Needs SEC_CONTACT_EMAIL and a local database (scripts/local_db.sh). Usage:

    uv run python scripts/record_sec_fixtures.py --cik 875320
"""

import argparse
import json
import re
import tempfile
from pathlib import Path

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from catalyst import settings
from catalyst.collectors.sec.edgar import TIMEOUT_SECONDS, EdgarClient
from catalyst.collectors.sec.press_release import collect_latest_press_release
from catalyst.snapshots import LocalSnapshotStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sec"


class RecordingTransport(httpx.BaseTransport):
    """Pass requests to the network and keep a copy of each response."""

    def __init__(self) -> None:
        """Wrap the default network transport."""
        self._inner = httpx.HTTPTransport()
        self.responses: list[tuple[str, int, str, bytes]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Send the request and record the decoded response body."""
        response = self._inner.handle_request(request)
        response.read()
        content_type = response.headers.get("content-type", "")
        self.responses.append((str(request.url), response.status_code, content_type, response.content))
        return response


def _file_name(url: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", url.split("://", 1)[1].split("/", 1)[1])[-120:]


def main() -> None:
    """Record fixtures for one company."""
    parser = argparse.ArgumentParser(description="Record SEC fixtures for the walking skeleton.")
    parser.add_argument("--cik", type=int, required=True)
    args = parser.parse_args()

    transport = RecordingTransport()
    client = EdgarClient(
        settings.sec_contact_email(), httpx.Client(transport=transport, timeout=TIMEOUT_SECONDS)
    )
    engine = create_engine(settings.database_url())
    with tempfile.TemporaryDirectory() as snapshots:
        collect_latest_press_release(
            client, LocalSnapshotStore(Path(snapshots)), sessionmaker(engine), args.cik
        )
    engine.dispose()

    folder = FIXTURE_ROOT / str(args.cik)
    folder.mkdir(parents=True, exist_ok=True)
    index: dict[str, dict[str, str | int]] = {}
    for url, status, content_type, body in transport.responses:
        name = _file_name(url)
        (folder / name).write_bytes(body)
        index[url] = {"file": name, "status": status, "content_type": content_type}
    (folder / "responses.json").write_text(json.dumps(index, indent=2) + "\n")
    print(f"Recorded {len(index)} responses in {folder}")


if __name__ == "__main__":
    main()
