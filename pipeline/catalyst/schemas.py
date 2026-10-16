"""Pydantic models: the single source of truth for every stored entity (spec: Engineering standards).

The walking skeleton defines only `SourceDocument`. The "Data schemas" roadmap item adds the rest.
"""

from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field


def _deep_https_link(url: str) -> str:
    """Accept only https links to a specific document, never a homepage (spec: Provenance, item 3)."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError("source links must be https URLs")
    if parts.path.strip("/") == "":
        raise ValueError("source links must point to a document, not a homepage")
    return url


DeepLink = Annotated[str, AfterValidator(_deep_https_link)]


class SourceDocument(BaseModel):
    """One primary document we retrieved, with a hash of the private copy we stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Internal identifier.")
    url: DeepLink = Field(description="Deep link to the exact document, for example an 8-K's Exhibit 99.1.")
    publisher: str = Field(min_length=1, description="Who published the document, for example 'SEC EDGAR'.")
    document_type: str = Field(
        min_length=1,
        description="Form and exhibit type as the publisher labels it, for example '8-K EX-99.1'.",
    )
    published_at: AwareDatetime = Field(description="When the publisher made the document public.")
    retrieved_at: AwareDatetime = Field(description="When we downloaded the copy that `sha256` describes.")
    sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="SHA-256 hash of the stored copy, lowercase hex."
    )
    snapshot_key: str = Field(
        min_length=1, description="Where the private copy is kept in the snapshot store."
    )
    content_type: str = Field(
        min_length=1, description="Media type the publisher sent, for example 'text/html'."
    )
