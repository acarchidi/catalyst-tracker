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
