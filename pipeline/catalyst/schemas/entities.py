"""Every stored entity except source documents (spec: Data model).

Each class becomes one table. Field descriptions become column comments and the data dictionary.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, ClassVar, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, JsonValue, model_validator

from catalyst.schemas.base import Entity, References
from catalyst.schemas.enums import (
    CatalystField,
    CatalystStatus,
    CatalystType,
    CheckOutcome,
    ClaimSubject,
    DateConfidence,
    DatePrecision,
    DocumentMatch,
    DrugNameKind,
    DrugRole,
    LineOfTherapy,
    MatchedEntity,
    MatchStatus,
    Modality,
    ModelCallStatus,
    OutcomeResult,
    TrialPhase,
)
from catalyst.schemas.identifiers import (
    ChemblId,
    Cik,
    EfoId,
    EuCtNumber,
    HgncSymbol,
    Icd10Code,
    MeshId,
    NctId,
    SicCode,
    Ticker,
    Unii,
)

Name = Annotated[str, Field(min_length=1, max_length=300)]
ShortText = Annotated[str, Field(min_length=1, max_length=100)]
MAX_QUOTE_CHARS = 1000


def _check_period(valid_from: date | None, valid_to: date | None) -> None:
    if valid_from is not None and valid_to is not None and valid_to < valid_from:
        raise ValueError("valid_to cannot be earlier than valid_from")


class Company(Entity):
    """A US-listed company we cover, identified by its SEC number."""

    __tablename__ = "company"
    __unique__ = (("cik",),)

    cik: Cik = Field(description="SEC Central Index Key; the company's permanent SEC number.")
    name: Name = Field(description="Current legal name as filed with the SEC.")
    sic_code: SicCode | None = Field(
        default=None, description="SEC industry code, for example 2834 (pharmaceutical preparations)."
    )


class CompanyDocument(Entity):
    """Links a source document to a company it concerns, and says how the link was made."""

    __tablename__ = "company_document"
    __unique__ = (("company_id", "source_document_id"),)

    company_id: Annotated[UUID, References("company")] = Field(description="The company.")
    source_document_id: Annotated[UUID, References("source_document")] = Field(description="The document.")
    matched_by: DocumentMatch = Field(description="How the link was made, for example by SEC number.")


class CompanyTicker(Entity):
    """A ticker a company traded under, with the dates it was valid."""

    __tablename__ = "company_ticker"
    __unique__ = (("company_id", "ticker", "valid_from"),)
    __partial_unique__: ClassVar[dict[str, tuple[tuple[str, ...], str]]] = {
        "ix_company_ticker_current": (("ticker",), "valid_to IS NULL")
    }
    __checks__: ClassVar[dict[str, str]] = {"period": "valid_to IS NULL OR valid_to >= valid_from"}

    company_id: Annotated[UUID, References("company")] = Field(description="The company.")
    ticker: Ticker = Field(description="Ticker symbol, upper case, for example VRTX.")
    exchange: str | None = Field(
        default=None, max_length=20, description="Listing exchange, for example Nasdaq."
    )
    valid_from: date = Field(description="First day the ticker applied.")
    valid_to: date | None = Field(
        default=None, description="Last day the ticker applied; empty if still current."
    )

    @model_validator(mode="after")
    def _period(self) -> Self:
        _check_period(self.valid_from, self.valid_to)
        return self


class CompanyName(Entity):
    """A current or former company name, so searches find old names."""

    __tablename__ = "company_name"
    __unique__ = (("company_id", "name"),)
    __checks__: ClassVar[dict[str, str]] = {
        "period": "valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from"
    }

    company_id: Annotated[UUID, References("company")] = Field(description="The company.")
    name: Name = Field(description="The name as filed.")
    valid_from: date | None = Field(default=None, description="First day the name applied, if known.")
    valid_to: date | None = Field(
        default=None, description="Last day the name applied; empty if still current."
    )

    @model_validator(mode="after")
    def _period(self) -> Self:
        _check_period(self.valid_from, self.valid_to)
        return self


class Drug(Entity):
    """A drug or program (asset)."""

    __tablename__ = "drug"
    __unique__ = (("unii",), ("chembl_id",))

    company_id: Annotated[UUID, References("company")] | None = Field(
        default=None, description="Company developing the drug, if one is known."
    )
    preferred_name: Name = Field(description="Name shown to users: generic name if any, else code name.")
    modality: Modality | None = Field(default=None, description="What kind of medicine it is.")
    unii: Unii | None = Field(default=None, description="FDA Unique Ingredient Identifier.")
    chembl_id: ChemblId | None = Field(default=None, description="ChEMBL compound identifier.")


class DrugName(Entity):
    """One name for a drug: code name, generic (INN), or brand."""

    __tablename__ = "drug_name"
    __unique__ = (("drug_id", "name"),)

    drug_id: Annotated[UUID, References("drug")] = Field(description="The drug.")
    name: Name = Field(description="The name as written by its source.")
    kind: DrugNameKind = Field(description="Code name, generic (INN) name, or brand name.")


class Target(Entity):
    """A biological target, identified by its HGNC gene symbol."""

    __tablename__ = "target"
    __unique__ = (("hgnc_symbol",),)

    hgnc_symbol: HgncSymbol = Field(description="HGNC gene symbol, for example EGFR.")
    name: Name | None = Field(default=None, description="Full gene or protein name.")
    chembl_target_id: ChemblId | None = Field(default=None, description="ChEMBL target identifier.")


class DrugTarget(Entity):
    """Links a drug to a target it acts on."""

    __tablename__ = "drug_target"
    __unique__ = (("drug_id", "target_id"),)

    drug_id: Annotated[UUID, References("drug")] = Field(description="The drug.")
    target_id: Annotated[UUID, References("target")] = Field(description="The target.")
    mechanism: Name | None = Field(
        default=None, description="Mechanism of action, for example 'EGFR inhibitor'."
    )


class Indication(Entity):
    """A disease, optionally narrowed by line of therapy and biomarker."""

    __tablename__ = "indication"
    __unique_nulls_equal__ = (("mesh_id", "line_of_therapy", "biomarker"),)

    mesh_id: MeshId = Field(description="MeSH identifier of the disease.")
    name: Name = Field(description="Disease name shown to users.")
    efo_id: EfoId | None = Field(
        default=None, description="Experimental Factor Ontology (or MONDO) identifier."
    )
    icd10_code: Icd10Code | None = Field(default=None, description="ICD-10 code.")
    line_of_therapy: LineOfTherapy | None = Field(default=None, description="Treatment line, if specified.")
    biomarker: ShortText | None = Field(
        default=None, description="Biomarker subgroup, for example 'EGFR exon 19'."
    )


class Trial(Entity):
    """A clinical trial, identified by its ClinicalTrials.gov number."""

    __tablename__ = "trial"
    __unique__ = (("nct_id",),)

    nct_id: NctId = Field(description="ClinicalTrials.gov identifier, for example NCT04567890.")
    eu_ct_number: EuCtNumber | None = Field(default=None, description="EU trial number (CTIS or EudraCT).")
    study_name: Name | None = Field(
        default=None, description="The company's name for the study, for example RAINIER."
    )
    phase: TrialPhase | None = Field(default=None, description="Trial phase as the registry reports it.")


class Catalyst(Entity):
    """An expected event that can move a company's value, with its date window.

    These are the current values. Every change to them is kept in `catalyst_change`.
    """

    __tablename__ = "catalyst"
    __checks__: ClassVar[dict[str, str]] = {
        "window_order": "window_start <= window_end",
        "day_is_single_day": "(window_precision = 'day') = (window_start = window_end)",
    }

    catalyst_type: CatalystType = Field(description="What kind of event this is.")
    company_id: Annotated[UUID, References("company")] = Field(description="The company the event concerns.")
    indication_id: Annotated[UUID, References("indication")] | None = Field(
        default=None, description="The indication, if any."
    )
    title: Name = Field(description="One-line description shown to users.")
    window_start: date = Field(description="Earliest day the event may happen.")
    window_end: date = Field(description="Latest day the event may happen.")
    window_precision: DatePrecision = Field(description="How precisely the date is known.")
    window_raw_text: Name = Field(description="The date exactly as the source wrote it, for example '2H26'.")
    date_confidence: DateConfidence = Field(description="Who stands behind the date.")
    status: CatalystStatus = Field(description="Upcoming, slipped, or resolved.")

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.window_start > self.window_end:
            raise ValueError("window_start cannot be after window_end")
        single_day = self.window_start == self.window_end
        if self.window_precision is DatePrecision.DAY and not single_day:
            raise ValueError("day precision means window_start equals window_end")
        if self.window_precision is not DatePrecision.DAY and single_day:
            raise ValueError("a single-day window must have day precision")
        return self


class CatalystTrial(Entity):
    """Links a catalyst to a trial it concerns."""

    __tablename__ = "catalyst_trial"
    __unique__ = (("catalyst_id", "trial_id"),)

    catalyst_id: Annotated[UUID, References("catalyst")] = Field(description="The catalyst.")
    trial_id: Annotated[UUID, References("trial")] = Field(description="The trial.")


class CatalystDrug(Entity):
    """Links a catalyst to one drug it concerns, tested alone or in combination (ADR 0016).

    A catalyst may link many drugs, and one drug both alone and in combination. Each link names
    the claim whose quote states it.
    """

    __tablename__ = "catalyst_drug"
    __unique__ = (("catalyst_id", "drug_id", "role"),)

    catalyst_id: Annotated[UUID, References("catalyst")] = Field(description="The catalyst.")
    drug_id: Annotated[UUID, References("drug")] = Field(description="A drug the catalyst concerns.")
    role: DrugRole = Field(
        description="Whether the drug is tested alone or in combination; a combination product itself "
        "(for example SPY120) is linked as in_combination."
    )
    claim_id: Annotated[UUID, References("claim")] = Field(
        description="The claim whose quote states this link."
    )


class DrugComponent(Entity):
    """One part of a combination drug, recorded only where a source states it (ADR 0016)."""

    __tablename__ = "drug_component"
    __unique__ = (("combination_id", "component_id"),)
    __checks__: ClassVar[dict[str, str]] = {"not_itself": "combination_id <> component_id"}

    combination_id: Annotated[UUID, References("drug")] = Field(description="The combination.")
    component_id: Annotated[UUID, References("drug")] = Field(description="One drug in it.")
    claim_id: Annotated[UUID, References("claim")] = Field(
        description="The claim whose quote names this part."
    )

    @model_validator(mode="after")
    def _not_itself(self) -> Self:
        if self.combination_id == self.component_id:
            raise ValueError("a combination cannot be its own component")
        return self


class Claim(Entity):
    """One fact taken from one source document, with the exact words that state it (spec: Provenance)."""

    # Quotes are verbatim, so surrounding spaces are kept.
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=False)

    __tablename__ = "claim"
    # Re-running an extractor on the same document with the same prompt and model adds nothing.
    __unique__ = (
        (
            "source_document_id",
            "field",
            "quote_start",
            "quote_end",
            "agent",
            "prompt_id",
            "prompt_version",
            "model",
        ),
    )
    __checks__: ClassVar[dict[str, str]] = {
        "forward_span": "quote_start >= 0 AND quote_end > quote_start",
        "span_matches_quote": "quote_end - quote_start = length(quote)",
        "quote_not_blank": "length(btrim(quote)) > 0",
        "has_value": "value <> 'null'::jsonb",
    }

    source_document_id: Annotated[UUID, References("source_document")] = Field(
        description="The document the fact comes from."
    )
    subject_type: ClaimSubject = Field(description="Which kind of record the fact is about.")
    subject_id: UUID | None = Field(
        default=None, description="The record the fact is about; empty until entity resolution matches it."
    )
    field: ShortText = Field(description="Which field the fact sets, for example 'window' or 'outcome'.")
    value: JsonValue = Field(description="The extracted value, as JSON.")
    quote: str = Field(
        min_length=1,
        max_length=MAX_QUOTE_CHARS,
        description="Verbatim words from the source that state the fact.",
    )
    quote_start: int = Field(
        ge=0, description="Character offset where the quote starts in the stored copy's text."
    )
    quote_end: int = Field(gt=0, description="Character offset just after the quote ends.")
    agent: ShortText = Field(description="Agent role that extracted the fact, for example 'extractor'.")
    prompt_id: ShortText = Field(description="Identifier of the prompt file used.")
    prompt_version: ShortText = Field(description="Version of that prompt.")
    provider: ShortText = Field(description="Model provider, for example 'anthropic'.")
    model: ShortText = Field(description="Exact model identifier used.")
    extracted_at: AwareDatetime = Field(description="When the fact was extracted.")

    @model_validator(mode="after")
    def _span(self) -> Self:
        if not self.quote.strip():
            raise ValueError("quote cannot be blank")
        if self.quote_end <= self.quote_start:
            raise ValueError("quote_end must be after quote_start")
        if self.quote_end - self.quote_start != len(self.quote):
            raise ValueError("the quote span must be as long as the quote")
        if self.value is None:
            raise ValueError("a claim must carry a value")
        return self


class EntityMatch(Entity):
    """How one name in a claim was matched to a company, drug, or trial (ADR 0015, part 4).

    Resolved matches name their record. Ambiguous and unknown ones wait for review, with every
    candidate. A later run updates the row as the reference tables grow.
    """

    __tablename__ = "entity_match"
    __unique__ = (("claim_id", "entity_type", "name"),)
    __checks__: ClassVar[dict[str, str]] = {
        "resolved_has_record": "(status = 'resolved') = (matched_id IS NOT NULL)",
    }

    claim_id: Annotated[UUID, References("claim")] = Field(description="The claim the name comes from.")
    entity_type: MatchedEntity = Field(description="Company, drug, or trial.")
    name: Name = Field(description="The name as the claim gives it, or the source link for a company.")
    status: MatchStatus = Field(description="Resolved (one record), ambiguous (several), or unknown (none).")
    matched_id: UUID | None = Field(default=None, description="The record, when resolved.")
    candidates: JsonValue = Field(description="Every candidate record's id, as a list; empty when unknown.")
    matched_by: ShortText | None = Field(
        default=None, description="Which rule found the candidates, for example 'cik' or 'code_name'."
    )
    resolved_at: AwareDatetime = Field(description="When this match was last made.")

    @model_validator(mode="after")
    def _record(self) -> Self:
        if (self.status is MatchStatus.RESOLVED) != (self.matched_id is not None):
            raise ValueError("a resolved match names its record, and only a resolved match does")
        return self


class ClaimCheck(Entity):
    """One verification check's outcome for one claim (spec: Verification; ADR 0018).

    A later run replaces the outcome, so the table always holds each claim's latest result.
    """

    __tablename__ = "claim_check"
    __unique__ = (("claim_id", "check"),)
    __checks__: ClassVar[dict[str, str]] = {"fail_has_reason": "outcome <> 'fail' OR reason IS NOT NULL"}

    claim_id: Annotated[UUID, References("claim")] = Field(description="The claim checked.")
    check: ShortText = Field(description="Which check, for example 'window_not_past'.")
    outcome: CheckOutcome = Field(description="Pass, fail, or not applicable.")
    reason: Name | None = Field(default=None, description="Why it failed or did not apply, in plain words.")
    checked_at: AwareDatetime = Field(description="When the check last ran.")


class CatalystChange(Entity):
    """One change to a catalyst field (spec: Change history).

    Append-only: rows are added, never edited or deleted. The database enforces this.
    """

    __tablename__ = "catalyst_change"
    __checks__: ClassVar[dict[str, str]] = {
        "changes_something": "old_value IS DISTINCT FROM new_value",
        "has_new_value": "new_value <> 'null'::jsonb",
    }

    catalyst_id: Annotated[UUID, References("catalyst")] = Field(description="The catalyst that changed.")
    field: CatalystField = Field(description="Which catalyst field changed.")
    old_value: JsonValue | None = Field(
        default=None, description="Value before the change; empty for a first value."
    )
    new_value: JsonValue = Field(description="Value after the change.")
    claim_id: Annotated[UUID, References("claim")] = Field(description="The claim that caused the change.")
    learned_at: AwareDatetime = Field(description="When we learned of the change.")
    is_correction: bool = Field(
        default=False, description="True when the change fixes our own earlier error."
    )

    @model_validator(mode="after")
    def _changes(self) -> Self:
        if self.new_value is None:
            raise ValueError("new_value is required")
        if self.old_value == self.new_value:
            raise ValueError("new_value must differ from old_value")
        return self


class Outcome(Entity):
    """What happened once a catalyst's date passed (spec: Outcomes). Price reaction arrives in Phase 3."""

    __tablename__ = "outcome"
    __unique__ = (("catalyst_id",),)
    __checks__: ClassVar[dict[str, str]] = {
        "votes_present": "result <> 'advisory_vote' OR (votes_for IS NOT NULL AND votes_against IS NOT NULL)",
        "votes_not_negative": "coalesce(votes_for, 0) >= 0 AND coalesce(votes_against, 0) >= 0",
    }

    catalyst_id: Annotated[UUID, References("catalyst")] = Field(description="The resolved catalyst.")
    result: OutcomeResult = Field(description="Result category.")
    key_numbers: dict[str, float | str] = Field(
        default_factory=dict[str, float | str],
        description="Headline numbers by name, for example response rate, hazard ratio, p-value.",
    )
    votes_for: int | None = Field(default=None, ge=0, description="Advisory committee votes in favor.")
    votes_against: int | None = Field(default=None, ge=0, description="Advisory committee votes against.")
    resolution_claim_id: Annotated[UUID, References("claim")] = Field(
        description="The claim, with its source, that reports the result."
    )
    resolved_on: date = Field(description="Day the result became public.")

    @model_validator(mode="after")
    def _votes(self) -> Self:
        if self.result is OutcomeResult.ADVISORY_VOTE and (
            self.votes_for is None or self.votes_against is None
        ):
            raise ValueError("an advisory vote outcome needs votes_for and votes_against")
        return self


TokenCount = Annotated[int, Field(ge=0)]


class ModelCall(Entity):
    """One model call and what it cost (spec: every model call logged with its cost).

    Daily spend caps are summed from this table, so every job shares one total per role.
    """

    __tablename__ = "model_call"
    __checks__: ClassVar[dict[str, str]] = {
        "counts_not_negative": (
            "input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0 "
            "AND cache_write_tokens >= 0 AND cost_usd >= 0 AND latency_ms >= 0"
        )
    }

    role: ShortText = Field(description="Agent role that made the call, for example 'extractor'.")
    provider: ShortText = Field(description="Model provider, for example 'anthropic'.")
    model: ShortText = Field(description="Model that answered.")
    prompt_id: ShortText = Field(description="Prompt used.")
    prompt_version: ShortText = Field(description="Version of that prompt.")
    input_tokens: TokenCount = Field(description="Uncached input tokens billed.")
    output_tokens: TokenCount = Field(description="Output tokens billed, including thinking.")
    cache_read_tokens: TokenCount = Field(description="Input tokens read from the prompt cache.")
    cache_write_tokens: TokenCount = Field(description="Input tokens written to the prompt cache.")
    cost_usd: Decimal = Field(ge=0, description="Cost in US dollars at the prices in config/models.toml.")
    latency_ms: TokenCount = Field(description="Wall-clock time of the call, in milliseconds.")
    request_id: str | None = Field(
        default=None, max_length=200, description="Provider's request id, for support."
    )
    status: ModelCallStatus = Field(description="How the call ended.")
    called_at: AwareDatetime = Field(description="When the call started.")
    config_version: ShortText = Field(description="Version of config/models.toml in force.")
