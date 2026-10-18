"""Federal Register collector: FDA advisory committee meeting notices (spec: Data sources)."""

import json
import re
from collections.abc import Iterator
from datetime import date
from typing import Any

from catalyst.collectors.documents import FetchedDocument, new_york_midnight
from catalyst.collectors.http import PoliteHttpClient
from catalyst.schemas import SourceTier

API_URL = "https://www.federalregister.gov/api/v1/documents.json"
PUBLISHER = "Federal Register"
REQUESTS_PER_SECOND = 1.0
# The search answer's usual fields, plus `dates` and `action`: the DATES section is where a notice
# states its meeting day, and the search answer leaves it out unless asked (review of PR #43).
FIELDS = (
    "abstract",
    "action",
    "agencies",
    "dates",
    "document_number",
    "excerpts",
    "html_url",
    "pdf_url",
    "public_inspection_pdf_url",
    "publication_date",
    "title",
    "type",
)


def is_advisory_committee_meeting(document: dict[str, Any]) -> bool:
    """Return True for FDA advisory committee meeting notices and the notices that change them.

    An amendment ("Amendment of Notice"), a cancellation, or a postponement moves or ends a
    meeting, so the date check must see it (Claude review of PR #49).
    """
    title: str = document.get("title") or ""
    return "Advisory Committee" in title and _MEETING_TITLE.search(title) is not None


_MEETING_TITLE = re.compile(
    r"Notice of Meeting|Amendment of Notice|Cancell?ation|Postponement|Reschedul", re.I
)


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
    params: dict[str, str | int | list[str]] = {
        "fields[]": list(FIELDS),
        "conditions[agencies][]": "food-and-drug-administration",
        "conditions[type][]": "NOTICE",
        "conditions[term]": '"advisory committee"',
        "conditions[publication_date][gte]": since.isoformat(),
        "order": "newest",
        "per_page": per_page,
    }
    page = client.get(API_URL, params=params).json()
    for page_number in range(max_pages):
        for document in page.get("results", []):
            if is_advisory_committee_meeting(document):
                yield to_document(document)
        next_url = page.get("next_page_url")
        if not next_url or page_number + 1 == max_pages:
            break
        # The next-page URL already carries every filter; adding params would replace them.
        page = client.get(next_url).json()
