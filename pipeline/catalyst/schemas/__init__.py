"""Pydantic models: the single source of truth for every stored entity (spec: Engineering standards).

Database tables, the data dictionary, and later the API and MCP schemas are generated from these.
"""

from catalyst.schemas.base import Entity, References
from catalyst.schemas.entities import (
    Catalyst,
    CatalystChange,
    CatalystTrial,
    Claim,
    Company,
    CompanyName,
    CompanyTicker,
    Drug,
    DrugName,
    DrugTarget,
    Indication,
    Outcome,
    Target,
    Trial,
)
from catalyst.schemas.enums import (
    CatalystStatus,
    CatalystType,
    ClaimSubject,
    DateConfidence,
    DatePrecision,
    DrugNameKind,
    LineOfTherapy,
    Modality,
    OutcomeResult,
    TrialPhase,
)
from catalyst.schemas.source import SourceDocument

# In dependency order: a table appears after every table it references.
ALL_MODELS: tuple[type[Entity], ...] = (
    SourceDocument,
    Company,
    CompanyTicker,
    CompanyName,
    Drug,
    DrugName,
    Target,
    DrugTarget,
    Indication,
    Trial,
    Catalyst,
    CatalystTrial,
    Claim,
    CatalystChange,
    Outcome,
)

__all__ = [
    "ALL_MODELS",
    "Catalyst",
    "CatalystChange",
    "CatalystStatus",
    "CatalystTrial",
    "CatalystType",
    "Claim",
    "ClaimSubject",
    "Company",
    "CompanyName",
    "CompanyTicker",
    "DateConfidence",
    "DatePrecision",
    "Drug",
    "DrugName",
    "DrugNameKind",
    "DrugTarget",
    "Entity",
    "Indication",
    "LineOfTherapy",
    "Modality",
    "Outcome",
    "OutcomeResult",
    "References",
    "SourceDocument",
    "Target",
    "Trial",
    "TrialPhase",
]
