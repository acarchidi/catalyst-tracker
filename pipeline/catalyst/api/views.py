"""What the API and MCP tools return. Every value comes with the sources that state it (spec: Provenance).

The free shape is `CatalystSummary`. Paid-only fields live only on `CatalystDetail`, so a free
response cannot carry them (ADR 0025).
"""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from catalyst.schemas import (
    CatalystStatus,
    CatalystType,
    DateConfidence,
    DatePrecision,
    DrugRole,
    LineOfTherapy,
    SourceTier,
)


class View(BaseModel):
    """Base for response shapes: frozen, and no field outside the declared ones."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceView(View):
    """One primary document and the verbatim words in it that state one or more values."""

    claim_id: UUID = Field(description="The published claim that holds the quote (ADR 0034).")
    url: str = Field(description="Deep link to the exact document, for example an 8-K's Exhibit 99.1.")
    publisher: str = Field(description="Who published the document.")
    document_type: str = Field(description="Form and exhibit type, for example '8-K EX-99.1'.")
    source_tier: SourceTier = Field(description="Rank in the provenance ranking.")
    published_at: datetime = Field(description="When the publisher made the document public.")
    retrieved_at: datetime = Field(description="When we retrieved the copy the quote was checked against.")
    quote: str = Field(description="Verbatim words from the document that state the values.")
    fields: list[str] = Field(description="Which values of this record the quote states.")


class CompanyView(View):
    """The company a catalyst concerns."""

    id: UUID = Field(description="Internal identifier.")
    name: str = Field(description="Current legal name as filed with the SEC.")
    cik: int = Field(description="SEC Central Index Key.")
    ticker: str | None = Field(description="Current ticker, if the company has one.")


class DrugView(View):
    """A drug the catalyst concerns; the link's source is in the catalyst's `sources` under 'drugs'."""

    id: UUID = Field(description="Internal identifier.")
    name: str = Field(description="Name shown to users: generic name if any, else code name.")
    role: DrugRole = Field(description="Tested alone or in combination.")


class IndicationView(View):
    """The disease the catalyst concerns."""

    id: UUID = Field(description="Internal identifier.")
    mesh_id: str = Field(description="MeSH identifier of the disease.")
    name: str = Field(description="Disease name.")
    line_of_therapy: LineOfTherapy | None = Field(description="Treatment line, if specified.")
    biomarker: str | None = Field(description="Biomarker subgroup, if specified.")


class WindowView(View):
    """When the event may happen: a window, not a single day (spec: Date precision)."""

    start: date = Field(description="Earliest day the event may happen.")
    end: date = Field(description="Latest day the event may happen.")
    precision: DatePrecision = Field(description="How precisely the date is known.")
    raw_text: str = Field(description="The date exactly as the source wrote it, for example '2H26'.")


class ChangeView(View):
    """One recorded change to a catalyst value, with the source that caused it."""

    field: str = Field(description="Which value changed.")
    old_value: JsonValue = Field(description="Value before the change; empty for a first value.")
    new_value: JsonValue = Field(description="Value after the change.")
    learned_at: datetime = Field(description="When we learned of the change.")
    is_correction: bool = Field(description="True when the change fixes our own earlier error.")
    source: SourceView = Field(description="The document and quote behind the change.")


class CatalystSummary(View):
    """A catalyst as every caller sees it: the summary fields and every source (free tier)."""

    id: UUID = Field(description="Internal identifier.")
    catalyst_type: CatalystType = Field(description="What kind of event this is.")
    title: str = Field(description="One-line description.")
    company: CompanyView = Field(description="The company the event concerns.")
    indication: IndicationView | None = Field(description="The disease, if any.")
    drugs: list[DrugView] = Field(description="Drugs the event concerns.")
    window: WindowView = Field(description="When the event may happen.")
    date_confidence: DateConfidence = Field(description="Who stands behind the date.")
    status: CatalystStatus = Field(description="Upcoming, slipped, or resolved.")
    latest_date_change: list[ChangeView] = Field(
        description="The latest move of the date window, one entry per date value; empty if it never moved."
    )
    sources: list[SourceView] = Field(description="Every document and quote behind the values above.")


class CatalystDetail(CatalystSummary):
    """A catalyst with the paid-tier fields."""

    history: list[ChangeView] = Field(description="Every recorded change, oldest first (paid).")


Shaped = CatalystDetail | CatalystSummary


class Envelope(View):
    """What every response carries."""

    notice: str = Field(description="Nothing here is financial advice.")
    access: Literal["free", "paid"] = Field(description="The access level the response was shaped for.")


class CatalystPage(Envelope):
    """One page of catalysts in date order."""

    window_from: date = Field(description="First day of the date window shown.")
    window_to: date | None = Field(description="Last day of the date window shown; empty if open-ended.")
    catalysts: list[Shaped] = Field(description="Catalysts whose window overlaps the date window.")
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page; empty on the last.")


class CompanyCatalystPage(CatalystPage):
    """One page of a company's catalysts."""

    company: CompanyView = Field(description="The company.")


class CatalystResult(Envelope):
    """One catalyst."""

    catalyst: Shaped = Field(description="The catalyst.")
