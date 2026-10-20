"""ClinicalTrials.gov API v2 collector: one stored record per trial version.

The API is used under its own terms at under 50 requests a minute; the site's robots.txt is
treated as rules for web crawlers (owner decision 2026-10-16, ADR 0005).
"""

import json
import logging
from collections.abc import Iterator, Sequence
from datetime import date
from typing import Any

from catalyst.collectors.documents import FetchedDocument, new_york_midnight
from catalyst.collectors.http import PoliteHttpClient
from catalyst.logs import log_event
from catalyst.schemas import SourceTier

_log = logging.getLogger(__name__)

API_URL = "https://clinicaltrials.gov/api/v2/studies"
STUDY_URL = "https://clinicaltrials.gov/study/{nct_id}"
PUBLISHER = "ClinicalTrials.gov"
PAGE_SIZE = 100
REQUESTS_PER_SECOND = 0.75  # 45 a minute


def lead_sponsor(study: dict[str, Any]) -> str:
    """Return the study's lead sponsor name as registered."""
    return study["protocolSection"]["sponsorCollaboratorsModule"]["leadSponsor"]["name"]


def is_sponsored_by(study: dict[str, Any], sponsor_names: frozenset[str]) -> bool:
    """Return True only when the lead sponsor is exactly one of the company's registered sponsor names.

    The API's sponsor search is fuzzy (a search for one company returns its partners' trials), so
    only exact lead-sponsor matches are linked to the company.
    """
    return lead_sponsor(study) in sponsor_names


def to_document(study: dict[str, Any]) -> FetchedDocument:
    """Turn one study record into a stored document keyed by trial and last update."""
    status = study["protocolSection"]["statusModule"]
    nct_id: str = study["protocolSection"]["identificationModule"]["nctId"]
    updated = date.fromisoformat(status["lastUpdatePostDateStruct"]["date"])
    return FetchedDocument(
        source_key=f"ctgov:{nct_id}:{status['lastUpdateSubmitDate']}",
        url=STUDY_URL.format(nct_id=nct_id),
        publisher=PUBLISHER,
        source_tier=SourceTier.REGISTRY,
        document_type="Registry record",
        published_at=new_york_midnight(updated),
        content=json.dumps(study, sort_keys=True, separators=(",", ":")).encode(),
        content_type="application/json",
    )


def collect_sponsor_studies(
    client: PoliteHttpClient, sponsor_names: frozenset[str], max_pages: int = 20, page_size: int = PAGE_SIZE
) -> Iterator[FetchedDocument]:
    """Yield every trial whose lead sponsor is one of `sponsor_names`."""
    for search_term in sorted(sponsor_names):
        params: dict[str, str | int] = {"query.spons": search_term, "pageSize": page_size}
        for _ in range(max_pages):
            page = client.get(API_URL, params=params).json()
            for study in page.get("studies", []):
                if is_sponsored_by(study, sponsor_names):
                    yield to_document(study)
            token = page.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token


BY_ID_BATCH = 20  # registry numbers per request: about 0.6 MB of full records (ADR 0040)
_MAX_PAGE_SIZE = 1000  # the API's largest page


def collect_studies_by_id(
    client: PoliteHttpClient, nct_ids: Sequence[str], batch_size: int = BY_ID_BATCH
) -> Iterator[FetchedDocument]:
    """Yield the full current record of each named trial, asking for `batch_size` numbers at a time.

    Each request names its trials with `filter.ids` and asks for a page as large as the batch, so
    one page holds the whole answer. A number the registry does not know is logged, not an error.
    """
    if not 1 <= batch_size <= _MAX_PAGE_SIZE:
        raise ValueError(f"batch_size must be from 1 to {_MAX_PAGE_SIZE}")
    for start in range(0, len(nct_ids), batch_size):
        batch = list(nct_ids[start : start + batch_size])
        params: dict[str, str | int] = {"filter.ids": ",".join(batch), "pageSize": batch_size}
        studies: list[dict[str, Any]] = client.get(API_URL, params=params).json().get("studies", [])
        found = {study["protocolSection"]["identificationModule"]["nctId"] for study in studies}
        if missing := [nct for nct in batch if nct not in found]:
            log_event(_log, "trials_not_in_registry", nct_ids=missing)
        for study in studies:
            yield to_document(study)
