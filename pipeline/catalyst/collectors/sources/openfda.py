"""openFDA Drugs@FDA collector: one stored record per application version.

Supplies approvals and application history (spec: Data sources). Each application links to its
Drugs@FDA page, the FDA's own record of it.
"""

import hashlib
import json
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import httpx

from catalyst.collectors.documents import FetchedDocument, new_york_midnight
from catalyst.collectors.http import PoliteHttpClient
from catalyst.schemas import SourceTier

API_URL = "https://api.fda.gov/drug/drugsfda.json"
OVERVIEW_URL = (
    "https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={number}"
)
PUBLISHER = "FDA (Drugs@FDA via openFDA)"
PAGE_SIZE = 100
REQUESTS_PER_SECOND = 2.0  # openFDA allows 240 a minute without a key


def latest_action_date(application: dict[str, Any]) -> str | None:
    """Return the most recent submission status date (YYYYMMDD), if any."""
    dates = [
        s["submission_status_date"]
        for s in application.get("submissions", [])
        if s.get("submission_status_date")
    ]
    return max(dates) if dates else None


def to_document(application: dict[str, Any]) -> FetchedDocument | None:
    """Turn one application into a stored document keyed by its latest action; None if it has no dates."""
    latest = latest_action_date(application)
    if latest is None:
        return None
    number: str = application["application_number"]
    content = json.dumps(application, sort_keys=True, separators=(",", ":")).encode()
    # The latest action date alone misses changes with no new submission, such as a product
    # being discontinued, so a short hash of the record is part of the key.
    digest = hashlib.sha256(content).hexdigest()[:12]
    return FetchedDocument(
        source_key=f"openfda:{number}:{latest}:{digest}",
        url=OVERVIEW_URL.format(number="".join(ch for ch in number if ch.isdigit())),
        publisher=PUBLISHER,
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type=f"Drugs@FDA application {number}",
        published_at=new_york_midnight(datetime.strptime(latest, "%Y%m%d").date()),  # noqa: DTZ007 - date only
        content=content,
        content_type="application/json",
    )


def collect_applications(
    client: PoliteHttpClient,
    manufacturer: str,
    sponsor_names: frozenset[str],
    max_pages: int = 10,
    page_size: int = PAGE_SIZE,
) -> Iterator[FetchedDocument]:
    """Yield applications found by manufacturer name whose sponsor is exactly one of `sponsor_names`."""
    skip = 0
    for _ in range(max_pages):
        params: dict[str, str | int] = {
            "search": f'openfda.manufacturer_name:"{manufacturer}"',
            "limit": page_size,
            "skip": skip,
        }
        try:
            page = client.get(API_URL, params=params).json()
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:  # openFDA answers 404 when nothing matches
                return
            raise
        for application in page.get("results", []):
            if application.get("sponsor_name") in sponsor_names and (document := to_document(application)):
                yield document
        skip += page_size
        if skip >= page["meta"]["results"]["total"]:
            break
