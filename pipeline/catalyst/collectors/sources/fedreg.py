"""Federal Register collector: FDA advisory committee meeting notices (spec: Data sources)."""

import json
from collections.abc import Iterator
from datetime import date
from typing import Any

from catalyst.collectors.documents import FetchedDocument, new_york_midnight
from catalyst.collectors.http import PoliteHttpClient
from catalyst.schemas import SourceTier

API_URL = "https://www.federalregister.gov/api/v1/documents.json"
PUBLISHER = "Federal Register"
REQUESTS_PER_SECOND = 1.0


def is_advisory_committee_meeting(document: dict[str, Any]) -> bool:
    """Return True for FDA advisory committee meeting notices, by their standard title wording."""
    title: str = document.get("title") or ""
    return "Advisory Committee" in title and "Notice of Meeting" in title


def to_document(document: dict[str, Any]) -> FetchedDocument:
    """Turn one Federal Register document into a stored document keyed by its document number."""
    return FetchedDocument(
        source_key=f"fedreg:{document['document_number']}",
        url=document["html_url"],
        publisher=PUBLISHER,
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type="Federal Register notice",
        published_at=new_york_midnight(date.fromisoformat(document["publication_date"])),
        content=json.dumps(document, sort_keys=True, separators=(",", ":")).encode(),
        content_type="application/json",
    )


def collect_advisory_committee_notices(
    client: PoliteHttpClient, since: date, max_pages: int = 10, per_page: int = 100
) -> Iterator[FetchedDocument]:
    """Yield FDA advisory committee meeting notices published on or after `since`."""
    params: dict[str, str | int] = {
        "conditions[agencies][]": "food-and-drug-administration",
        "conditions[type][]": "NOTICE",
        "conditions[term]": '"advisory committee"',
        "conditions[publication_date][gte]": since.isoformat(),
        "order": "newest",
        "per_page": per_page,
    }
    url: str | None = API_URL
    for _ in range(max_pages):
        if url is None:
            break
        page = client.get(url, params=params).json()
        for document in page.get("results", []):
            if is_advisory_committee_meeting(document):
                yield to_document(document)
        url, params = page.get("next_page_url"), {}
