"""Match the names in extracted claims to companies, drugs, and trials (ADR 0015, part 4).

Plain code over the reference tables. For each catalyst claim the extractor stored:
- the company is the one its source document is linked to;
- the drug name is matched by the rules in `catalyst.resolve`;
- every trial its quote names (registry number, or study name in capitals) is matched.

Each name gets one `entity_match` row, updated on later runs as the reference tables grow.
The only record this job creates is a drug for a company's own unknown code name, when the
company is known and its release quotes that code name; other unknown names wait for review.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import and_, case, delete, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.extract.agent import ROLE
from catalyst.logs import log_alert, log_event
from catalyst.references import load_reference_data
from catalyst.resolve import DrugRef, MatchedBy, ReferenceData, Resolution, Status, whole_word_pattern
from catalyst.schemas import (
    Claim,
    CompanyDocument,
    DocumentMatch,
    Drug,
    DrugName,
    DrugNameKind,
    EntityMatch,
    MatchedEntity,
)

_log = logging.getLogger(__name__)
_MATCH = TABLES[EntityMatch]
_COMPANY_LINK = "source document"
# A code name: letters, an optional dash or space, then digits, as in SPY001, VX-548, INS 1007.
_CODE_NAME = re.compile(r"^(?P<prefix>[A-Z]{1,6})[\s\-\u2010-\u2015]?(?P<number>\d{2,6})[A-Z]?$")
# Prefixes of things shaped like code names that are not drugs: diseases, targets, years, quarters;
# medical congresses, agencies, and filings ("ASCO 2027", "FDA 2027"; CP4 round 2); and advice
# words, which must never reach a title (spec: no buy or sell ratings).
_NOT_DRUG_PREFIXES = frozenset(
    {"COVID", "SARS", "IL", "CD", "FY", "CY", "Q", "H", "NCT", "ICD", "GLP", "HER", "PD"}
    | {"ASCO", "ESMO", "ASH", "AACR", "EASL", "AASLD", "FDA", "EMA", "PDUFA", "NDA", "BLA", "IND"}
    | {"BUY", "SELL"}
)
# A number part that is a year (1900 to 2099) names a date or a meeting, not a drug (CP4 round 2).
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_NAME_CHARS = 300  # EntityMatch.name limit


@dataclass
class MatchReport:
    """What one run matched."""

    claims: int = 0
    drugs_created: int = 0
    failed: int = 0
    statuses: Counter[str] = field(default_factory=Counter[str])

    def summary(self) -> dict[str, Any]:
        """Return the counts as plain JSON."""
        counts = {"claims": self.claims, "drugs_created": self.drugs_created, "failed": self.failed}
        return counts | {"matches": dict(self.statuses)}


@dataclass(frozen=True)
class _Claim:
    id: UUID
    source_document_id: UUID
    quote: str
    drug: str | None


def _drug_name(value: object) -> str | None:
    drug = cast(dict[str, object], value).get("drug") if isinstance(value, dict) else None
    return drug if isinstance(drug, str) and drug.strip() else None


def _claims(session: Session) -> list[_Claim]:
    claim = TABLES[Claim]
    query = select(claim.c.id, claim.c.source_document_id, claim.c.quote, claim.c.value).where(
        claim.c.agent == ROLE, claim.c.field.startswith("catalyst:")
    )
    return [
        _Claim(row.id, row.source_document_id, row.quote, _drug_name(row.value))
        for row in session.execute(query.order_by(claim.c.extracted_at, claim.c.id))
    ]


def _company_links(session: Session) -> dict[UUID, list[tuple[UUID, str]]]:
    link = TABLES[CompanyDocument]
    links: dict[UUID, list[tuple[UUID, str]]] = {}
    for row in session.execute(select(link.c.source_document_id, link.c.company_id, link.c.matched_by)):
        links.setdefault(row.source_document_id, []).append((row.company_id, row.matched_by))
    return links


def _company(links: list[tuple[UUID, str]]) -> Resolution:
    """Return the company a document is linked to; 'cik' only when every link is the SEC filer's own."""
    if not links:
        return Resolution()
    by_sec = all(how == DocumentMatch.SEC_CIK.value for _, how in links)
    return Resolution(
        tuple(dict.fromkeys(c for c, _ in links)), MatchedBy.CIK if by_sec else MatchedBy.DOCUMENT_LINK
    )


def _is_new_code_name(name: str, quote: str) -> bool:
    """Say whether the name is a code name quoted as a whole word.

    Not a disease, target, congress, agency, filing, or advice word, and its number is not a year.
    """
    shape = _CODE_NAME.match(name.strip())
    if shape is None or shape.group("prefix") in _NOT_DRUG_PREFIXES or _YEAR.match(shape.group("number")):
        return False
    match = whole_word_pattern(name).search(quote)
    return match is not None and match.group(0).upper() == match.group(0)


def _create_drug(session: Session, references: ReferenceData, name: str, company_id: UUID) -> UUID:
    drug = Drug(company_id=company_id, preferred_name=name.strip())
    session.execute(insert(TABLES[Drug]).values(**drug.model_dump()))
    code = DrugName(drug_id=drug.id, name=drug.preferred_name, kind=DrugNameKind.CODE)
    session.execute(insert(TABLES[DrugName]).values(**code.model_dump()))
    references.add_drug(DrugRef(id=drug.id, company_id=company_id, names=((code.name, DrugNameKind.CODE),)))
    log_event(_log, "drug_created", name=code.name)
    return drug.id


def known_drug(references: ReferenceData, name: str, company_id: UUID | None) -> Resolution:
    """Match a drug name among the reference tables' drugs, as this job does before creating one.

    A code name that belongs to another company is ambiguous for review, never this company's drug.
    So is a generic or brand name whose one drug another company develops (CP4 review, O1; ADR
    0037): a partner's or a competitor's drug named in a release must not become the company's
    catalyst without a person. A drug with no known company still matches.
    """
    drug = references.resolve_drug(name, company_id)
    if drug.id is not None and company_id is not None:
        owner = references.drug_company(drug.id)
        if owner is not None and owner != company_id:
            return Resolution(drug.candidates, drug.matched_by, needs_review=True)
    if drug.status is not Status.UNKNOWN:
        return drug
    elsewhere = references.resolve_drug(name)
    if elsewhere.candidates:
        return Resolution(elsewhere.candidates, elsewhere.matched_by, needs_review=True)
    return drug


def _drug(
    session: Session,
    references: ReferenceData,
    claim: _Claim,
    name: str,
    company: Resolution,
    report: MatchReport,
) -> Resolution:
    """Match the drug name; create a drug only for the company's own new code name.

    A code name that belongs to another company is ambiguous for review, never a second drug.
    """
    drug = known_drug(references, name, company.id)
    if drug.status is not Status.UNKNOWN:
        return drug
    if company.id and _is_new_code_name(name, claim.quote):
        report.drugs_created += 1
        return Resolution((_create_drug(session, references, name, company.id),), MatchedBy.NEW_CODE_NAME)
    return drug


def _matches(
    session: Session,
    references: ReferenceData,
    claim: _Claim,
    links: list[tuple[UUID, str]],
    report: MatchReport,
) -> Iterator[tuple[MatchedEntity, str, Resolution]]:
    company = _company(links)
    yield MatchedEntity.COMPANY, _COMPANY_LINK, company
    if claim.drug:
        yield MatchedEntity.DRUG, claim.drug, _drug(session, references, claim, claim.drug, company, report)
    for name, trial in references.trials_mentioned(claim.quote, company.id):
        yield MatchedEntity.TRIAL, name, trial


def _store(
    session: Session, claim_id: UUID, entity: MatchedEntity, name: str, result: Resolution, at: datetime
) -> None:
    row = EntityMatch(
        claim_id=claim_id,
        entity_type=entity,
        name=name,
        status=result.status,
        matched_id=result.id,
        candidates=[str(c) for c in result.candidates],
        matched_by=result.matched_by.value if result.matched_by else None,
        resolved_at=at,
    )
    values = row.model_dump()
    statement = insert(_MATCH).values(**values)
    changes: dict[str, Any] = {k: values[k] for k in ("status", "matched_id", "candidates", "resolved_at")}
    # A drug this job created stays marked as created while it is still the match.
    keep = and_(
        _MATCH.c.matched_by == MatchedBy.NEW_CODE_NAME.value,
        _MATCH.c.matched_id == statement.excluded.matched_id,
    )
    changes["matched_by"] = case((keep, _MATCH.c.matched_by), else_=statement.excluded.matched_by)
    session.execute(
        statement.on_conflict_do_update(index_elements=["claim_id", "entity_type", "name"], set_=changes)
    )


def _resolve_one(
    session: Session,
    references: ReferenceData,
    claim: _Claim,
    links: list[tuple[UUID, str]],
    at: datetime,
    report: MatchReport,
) -> None:
    produced: list[tuple[str, str]] = []
    for entity, name, result in _matches(session, references, claim, links, report):
        shown = name[:_NAME_CHARS]
        _store(session, claim.id, entity, shown, result, at)
        produced.append((entity.value, shown))
        report.statuses[f"{entity.value}:{result.status.value}"] += 1
    # Names no longer found (for example after a study name is corrected) lose their old match.
    stale = delete(_MATCH).where(_MATCH.c.claim_id == claim.id)
    if produced:
        stale = stale.where(tuple_(_MATCH.c.entity_type, _MATCH.c.name).not_in(produced))
    session.execute(stale)


def resolve_claims(session_factory: sessionmaker[Session], now: Callable[[], datetime]) -> MatchReport:
    """Match every extractor claim's names; safe to re-run, and later runs pick up new names.

    A claim that fails (for example a malformed name) is logged and skipped; the rest still match.
    """
    report = MatchReport()
    with session_factory.begin() as session:
        references = load_reference_data(session)
        links = _company_links(session)
        at = now()
        for claim in _claims(session):
            report.claims += 1
            try:
                with session.begin_nested():
                    _resolve_one(
                        session, references, claim, links.get(claim.source_document_id, []), at, report
                    )
            except (ValidationError, SQLAlchemyError) as error:
                report.failed += 1
                log_alert(
                    _log,
                    "claim_resolution_failed",
                    claim=str(claim.id),
                    error=f"{type(error).__name__}"[:100],
                )
    log_event(_log, "claims_resolved", **report.summary())
    return report
