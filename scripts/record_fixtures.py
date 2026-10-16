"""Record live responses from one source as test fixtures, so tests never touch live websites.

Every response the collector receives is saved under tests/fixtures/<label>/, with
responses.json mapping each URL to its file. Needs SEC_CONTACT_EMAIL. Examples:

    uv run python scripts/record_fixtures.py ctgov-insmed
    uv run python scripts/record_fixtures.py all
"""

import argparse
import json
import re
from collections.abc import Callable, Iterable
from datetime import date
from pathlib import Path

import httpx

from catalyst import settings
from catalyst.collectors.coverage import Override, build_coverage
from catalyst.collectors.run import Clients, build_clients
from catalyst.collectors.sec.edgar import SUBMISSIONS_URL, recent_filings
from catalyst.collectors.sources import ctgov, fedreg, openfda, wires

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


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
        self.responses.append(
            (
                str(request.url),
                response.status_code,
                response.headers.get("content-type", ""),
                response.content,
            )
        )
        return response


def _file_name(index: int, url: str) -> str:
    path = re.sub(r"[^A-Za-z0-9._-]", "_", url.split("://", 1)[1].split("?", 1)[0])
    return f"{index:02d}_{path[-80:]}"


def _ten_q_index(clients: Clients) -> Iterable[object]:
    submissions = clients.sec.get(SUBMISSIONS_URL.format(cik=875320)).content
    filing = recent_filings(submissions, "10-Q")[0]
    clients.sec.get(filing.index_url)
    return []


RECORDINGS: dict[str, Callable[[Clients], Iterable[object]]] = {
    "ctgov-insmed": lambda c: ctgov.collect_sponsor_studies(
        c.web, frozenset({"Insmed Incorporated"}), max_pages=2, page_size=5
    ),
    "openfda-insmed": lambda c: openfda.collect_applications(c.web, "Insmed", frozenset({"INSMED INC"})),
    "fedreg-2026-08": lambda c: fedreg.collect_advisory_committee_notices(c.web, date(2026, 8, 1)),
    "fedreg-2026-paged": lambda c: fedreg.collect_advisory_committee_notices(
        c.web, date(2026, 1, 1), max_pages=2, per_page=10
    ),
    "openfda-vertex-paged": lambda c: openfda.collect_applications(
        c.web, "Vertex", frozenset({"VERTEX PHARMS INC", "VERTEX PHARMS"}), page_size=5
    ),
    "wires-prnewswire": lambda c: wires.collect_feed(c.web, wires.FEEDS[0]),
    "sec-vertex-10q-index": _ten_q_index,
    # Absci registers its one trial under a subsidiary's name, so an override adds it.
    "coverage-8731": lambda c: build_coverage(
        c.sec,
        c.web,
        {1672688: Override(cik=1672688, note="recording", ctgov_sponsors=frozenset({"Absci Pty Ltd."}))},
        sic_codes=(8731,),
    ),
}


def record(label: str) -> None:
    """Run one recording and write its fixtures."""
    transport = RecordingTransport()
    clients = build_clients(settings.sec_contact_email(), transport)
    for _ in RECORDINGS[label](clients):
        pass
    folder = FIXTURE_ROOT / label
    folder.mkdir(parents=True, exist_ok=True)
    index: dict[str, dict[str, str | int]] = {}
    for number, (url, status, content_type, body) in enumerate(transport.responses):
        name = _file_name(number, url)
        (folder / name).write_bytes(body)
        index[url] = {"file": name, "status": status, "content_type": content_type}
    (folder / "responses.json").write_text(json.dumps(index, indent=2) + "\n")
    print(f"{label}: recorded {len(index)} responses in {folder}")


def main() -> None:
    """Record the named fixture set, or all of them."""
    parser = argparse.ArgumentParser(description="Record live responses as test fixtures.")
    parser.add_argument("label", choices=[*RECORDINGS, "all"])
    labels = list(RECORDINGS) if (args := parser.parse_args()).label == "all" else [args.label]
    for label in labels:
        record(label)


if __name__ == "__main__":
    main()
