"""Source documents: the primary documents every claim points back to (spec: Provenance)."""

from typing import Annotated, ClassVar, Self
from urllib.parse import urlsplit

from pydantic import AfterValidator, AwareDatetime, Field, model_validator

from catalyst.schemas.base import Entity
from catalyst.schemas.enums import SourceTier


def _deep_https_link(url: str) -> str:
    """Accept only https links with a document path (spec: Provenance, item 3).

    This is a minimal guard: it rejects bare domains, not every homepage-like path such as
    `/index.htm`. Collectors build links from the publisher's own document lists.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError("source links must be https URLs")
    if parts.path.strip("/") == "":
        raise ValueError("source links must point to a document, not a homepage")
    return url


DeepLink = Annotated[str, AfterValidator(_deep_https_link)]


class SourceDocument(Entity):
    """One primary document we retrieved, with a hash of the private copy we stored."""

    __tablename__ = "source_document"
    # The source's own identity for the document (an SEC accession and file name, an NCT number
    # and its update date, ...). Re-fetching the same key adds nothing (ADR 0005).
    __unique__ = (("source_key",),)
    __checks__: ClassVar[dict[str, str]] = {
        "deleted_only_after_read": "copy_deleted_at IS NULL OR fully_read_at IS NOT NULL"
    }

    source_key: str = Field(
        pattern=r"^[a-z]+:.+$",
        max_length=300,
        description="The source's own identity for this document version, such as 'sec:<accession>:<file>'.",
    )
    url: DeepLink = Field(description="Deep link to the exact document, for example an 8-K's Exhibit 99.1.")
    publisher: str = Field(
        min_length=1, max_length=200, description="Who published the document, for example 'SEC EDGAR'."
    )
    source_tier: SourceTier = Field(
        description="Rank in the provenance ranking; decides which claim wins when sources disagree."
    )
    document_type: str = Field(
        min_length=1,
        max_length=100,
        description="Form and exhibit type as the publisher labels it, for example '8-K EX-99.1'.",
    )
    published_at: AwareDatetime = Field(description="When the publisher made the document public.")
    retrieved_at: AwareDatetime = Field(description="When we downloaded the copy that `sha256` describes.")
    sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        max_length=64,
        description="SHA-256 hash of the stored copy, lowercase hex.",
    )
    snapshot_key: str = Field(
        min_length=1, description="Where the private copy is kept in the snapshot store."
    )
    content_type: str = Field(
        min_length=1, max_length=200, description="Media type the publisher sent, for example 'text/html'."
    )
    fully_read_at: AwareDatetime | None = Field(
        default=None,
        description=(
            "When the extractor finished reading every part of this filing or exhibit (ADR 0066); "
            "it is never read again. Empty until then, and for press releases."
        ),
    )
    copy_deleted_at: AwareDatetime | None = Field(
        default=None,
        description=(
            "When the stored copy was deleted, because the document was fully read and backs no claim "
            "(ADR 0066). Empty while the copy is kept."
        ),
    )

    @model_validator(mode="after")
    def _deleted_only_after_read(self) -> Self:
        if self.copy_deleted_at is not None and self.fully_read_at is None:
            raise ValueError("a copy is deleted only once its document is fully read")
        return self
