"""Controlled lists. Values are stored as text; the database checks each column against its list."""

from enum import StrEnum


class DatePrecision(StrEnum):
    """How precisely a catalyst date is known (spec: Date precision)."""

    DAY = "day"
    MONTH = "month"
    QUARTER = "quarter"
    HALF = "half"
    YEAR = "year"


class DateConfidence(StrEnum):
    """Who stands behind a catalyst date (spec: Date precision)."""

    COMPANY_CONFIRMED = "company_confirmed"
    REGULATOR_SCHEDULED = "regulator_scheduled"
    REGISTRY_ESTIMATE = "registry_estimate"
    INFERRED = "inferred"


class Modality(StrEnum):
    """Drug modality (spec: Data model, Modality)."""

    SMALL_MOLECULE = "small_molecule"
    ANTIBODY = "antibody"
    ADC = "adc"
    BISPECIFIC = "bispecific"
    CELL_THERAPY = "cell_therapy"
    GENE_THERAPY = "gene_therapy"
    RNAI = "rnai"
    ANTISENSE = "antisense"
    MRNA = "mrna"
    VACCINE = "vaccine"
    RADIOPHARMACEUTICAL = "radiopharmaceutical"
    PEPTIDE = "peptide"
    OTHER = "other"


class DrugNameKind(StrEnum):
    """Kind of drug name (spec: Data model, Drug)."""

    CODE = "code"
    INN = "inn"
    BRAND = "brand"


class DrugRole(StrEnum):
    """How a drug is tested in the trial a catalyst concerns (owner ruling 2026-10-17, rule 3)."""

    ALONE = "alone"
    IN_COMBINATION = "in_combination"


class LineOfTherapy(StrEnum):
    """Treatment line an indication refers to."""

    FIRST_LINE = "first_line"
    SECOND_LINE = "second_line"
    THIRD_LINE_PLUS = "third_line_plus"
    MAINTENANCE = "maintenance"
    ADJUVANT = "adjuvant"
    NEOADJUVANT = "neoadjuvant"


class TrialPhase(StrEnum):
    """Trial phase as ClinicalTrials.gov reports it."""

    EARLY_PHASE_1 = "early_phase_1"
    PHASE_1 = "phase_1"
    PHASE_1_2 = "phase_1_2"
    PHASE_2 = "phase_2"
    PHASE_2_3 = "phase_2_3"
    PHASE_3 = "phase_3"
    PHASE_4 = "phase_4"
    NOT_APPLICABLE = "not_applicable"


class CatalystType(StrEnum):
    """Catalyst types (spec: Data sources and coverage, Catalyst types)."""

    PDUFA_DATE = "pdufa_date"
    ADVISORY_COMMITTEE = "advisory_committee"
    TOPLINE_READOUT = "topline_readout"
    CONFERENCE_PRESENTATION = "conference_presentation"
    REGULATORY_SUBMISSION = "regulatory_submission"
    REGULATORY_ACCEPTANCE = "regulatory_acceptance"
    CHMP_OPINION = "chmp_opinion"
    COMPLETE_RESPONSE_LETTER = "complete_response_letter"
    LABEL_EXPANSION = "label_expansion"
    ENROLLMENT_COMPLETION = "enrollment_completion"
    OFFERING = "offering"
    SHELF_FILING = "shelf_filing"
    MERGER_ACQUISITION = "merger_acquisition"
    EARNINGS = "earnings"


class CatalystStatus(StrEnum):
    """Where a catalyst stands (spec: Search and filters, Status)."""

    UPCOMING = "upcoming"
    SLIPPED = "slipped"
    RESOLVED = "resolved"


class ClaimSubject(StrEnum):
    """Which kind of record a claim is about."""

    COMPANY = "company"
    DRUG = "drug"
    TARGET = "target"
    INDICATION = "indication"
    TRIAL = "trial"
    CATALYST = "catalyst"
    OUTCOME = "outcome"


class OutcomeResult(StrEnum):
    """Result categories for a resolved catalyst (spec: Data model, Outcomes)."""

    APPROVED = "approved"
    COMPLETE_RESPONSE_LETTER = "complete_response_letter"
    ADVISORY_VOTE = "advisory_vote"
    ENDPOINT_MET = "endpoint_met"
    ENDPOINT_MISSED = "endpoint_missed"
    MIXED = "mixed"
    DELAYED = "delayed"
    WITHDRAWN = "withdrawn"


class SourceTier(StrEnum):
    """Source ranking, highest first (spec: Provenance, item 1). News is a lead, never a source."""

    REGULATOR_OR_SEC = "regulator_or_sec"
    COMPANY_SITE = "company_site"
    WIRE = "wire"
    REGISTRY = "registry"
    CONFERENCE_OR_JOURNAL = "conference_or_journal"


class CatalystField(StrEnum):
    """Catalyst fields whose changes are recorded in change history."""

    CATALYST_TYPE = "catalyst_type"
    COMPANY_ID = "company_id"
    DRUGS = "drugs"
    INDICATION_ID = "indication_id"
    TITLE = "title"
    WINDOW_START = "window_start"
    WINDOW_END = "window_end"
    WINDOW_PRECISION = "window_precision"
    WINDOW_RAW_TEXT = "window_raw_text"
    DATE_CONFIDENCE = "date_confidence"
    STATUS = "status"


class ModelCallStatus(StrEnum):
    """How a model call ended."""

    OK = "ok"
    REFUSED = "refused"
    TRUNCATED = "truncated"
    INVALID_OUTPUT = "invalid_output"
    MODEL_SWITCHED = "model_switched"
    ERROR = "error"


class ReadingSkip(StrEnum):
    """Why a reader made no model call for a document (ADR 0038)."""

    NOT_TEXT = "not_text"  # not a web page or plain text, such as a PDF copy
    NO_READABLE_TEXT = "no_readable_text"  # fewer readable words than any real release has
    TOO_LARGE = "too_large"  # over the role's input limit


class MatchStatus(StrEnum):
    """Whether a name found exactly one record (ADR 0015)."""

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class CheckOutcome(StrEnum):
    """What a verification check concluded about a claim (ADR 0018)."""

    PASS = "pass"  # noqa: S105 - a check outcome, not a password
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


class GateDecision(StrEnum):
    """What the publish gate decided about a claim (ADR 0018, part 5)."""

    PUBLISH = "publish"
    REVIEW = "review"


class MatchedEntity(StrEnum):
    """Which kind of record a name in a claim was matched to."""

    COMPANY = "company"
    DRUG = "drug"
    TRIAL = "trial"


class DocumentMatch(StrEnum):
    """How a source document was linked to a company."""

    SEC_CIK = "sec_cik"
    CTGOV_LEAD_SPONSOR = "ctgov_lead_sponsor"
    OPENFDA_SPONSOR = "openfda_sponsor"
