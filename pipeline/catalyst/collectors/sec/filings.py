"""SEC filings collector: 8-K press releases, 10-Q and 10-K reports, and S-3 and 424B offerings.

Supplies catalyst announcements (8-K Exhibit 99), cash and burn (10-Q, 10-K), and dilution
signals (S-3, 424B) per the spec's Data sources table. Filings never change after acceptance,
so a filing whose documents are already stored is skipped without fetching it again.
"""

from collections.abc import Callable, Iterator
from datetime import date

from catalyst.collectors.documents import FetchedDocument
from catalyst.collectors.sec.edgar import (
    EdgarClient,
    ExhibitRef,
    FilingRef,
    list_documents,
    recent_filings_of,
)
from catalyst.schemas import SourceTier

PUBLISHER = "SEC EDGAR"
DEFAULT_FORMS = frozenset(
    {"8-K", "10-Q", "10-K", "S-3", "S-3ASR", "424B2", "424B3", "424B4", "424B5", "424B7"}
)


def source_key(filing: FilingRef, document: ExhibitRef) -> str:
    """Return the SEC's own identity for a document: accession number plus file name."""
    return f"sec:{filing.accession_number}:{document.url.rsplit('/', 1)[1]}"


def documents_to_keep(filing: FilingRef, documents: list[ExhibitRef]) -> list[ExhibitRef]:
    """For an 8-K keep its Exhibit 99 press releases; for other forms keep the main document."""
    if filing.form == "8-K":
        return [d for d in documents if d.type == "EX-99" or d.type.startswith("EX-99.")]
    return [d for d in documents if d.type == filing.form]


def collect_filings(
    client: EdgarClient,
    submissions_json: bytes,
    since: date,
    already_stored: Callable[[str], bool],
    forms: frozenset[str] = DEFAULT_FORMS,
) -> Iterator[FetchedDocument]:
    """Yield the documents of a company's filings in `forms` filed on or after `since`.

    `submissions_json` is the company's EDGAR filing list (`SUBMISSIONS_URL`).

    `already_stored(prefix)` says whether documents with that source-key prefix exist; such
    filings are skipped.
    """
    for filing in recent_filings_of(submissions_json, forms):
        if filing.filed_on < since:
            break
        if already_stored(f"sec:{filing.accession_number}:"):
            continue
        # Fetch every kept document before yielding any, so a failure part-way stores nothing and
        # the next run retries the whole filing (a stored filing is skipped by its accession prefix).
        fetched: list[FetchedDocument] = []
        for document in documents_to_keep(filing, list_documents(client.get(filing.index_url).text)):
            response = client.get(document.url)
            fetched.append(
                FetchedDocument(
                    source_key=source_key(filing, document),
                    url=document.url,
                    publisher=PUBLISHER,
                    source_tier=SourceTier.REGULATOR_OR_SEC,
                    document_type=filing.form
                    if document.type == filing.form
                    else f"{filing.form} {document.type}",
                    published_at=filing.accepted_at,
                    content=response.content,
                    content_type=response.headers.get("content-type", "application/octet-stream").split(";")[
                        0
                    ],
                )
            )
        yield from fetched
