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

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.extract.agent import ROLE
from catalyst.logs import log_event
from catalyst.references import load_reference_data
from catalyst.resolve import DrugRef, MatchedBy, ReferenceData, Resolution, Status, name_key
from catalyst.schemas import Claim, CompanyDocument, Drug, DrugName, DrugNameKind, EntityMatch, MatchedEntity

_log = logging.getLogger(__name__)
_MATCH = TABLES[EntityMatch]
_COMPANY_LINK = "source document"
# A code name: letters, an optional dash or space, then digits, as in SPY001, VX-548, INS 1007.
_CODE_NAME = re.compile(r"^[A-Z]{1,6}[\s\-\u2010-\u2015]?\d{2,6}[A-Z]?$")


@dataclass
class MatchReport:
    """What one run matched."""

    claims: int = 0
    drugs_created: int = 0
    statuses: Counter[str] = field(default_factory=Counter[str])

    def summary(self) -> dict[str, Any]:
        """Return the counts as plain JSON."""
        return {"claims": self.claims, "drugs_created": self.drugs_created, "matches": dict(self.statuses)}


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


def _company_links(session: Session) -> dict[UUID, list[UUID]]:
    link = TABLES[CompanyDocument]
    links: dict[UUID, list[UUID]] = {}
    for row in session.execute(select(link.c.source_document_id, link.c.company_id)):
        links.setdefault(row.source_document_id, []).append(row.company_id)
    return links


def _is_new_code_name(name: str, quote: str) -> bool:
    return bool(_CODE_NAME.match(name.strip())) and name_key(name) in name_key(quote)


def _create_drug(session: Session, references: ReferenceData, name: str, company_id: UUID) -> UUID:
    drug = Drug(company_id=company_id, preferred_name=name.strip())
    session.execute(insert(TABLES[Drug]).values(**drug.model_dump()))
    code = DrugName(drug_id=drug.id, name=drug.preferred_name, kind=DrugNameKind.CODE)
    session.execute(insert(TABLES[DrugName]).values(**code.model_dump()))
    references.add_drug(DrugRef(id=drug.id, company_id=company_id, names=((code.name, DrugNameKind.CODE),)))
    log_event(_log, "drug_created", name=code.name)
    return drug.id


def _matches(
    session: Session, references: ReferenceData, claim: _Claim, companies: list[UUID], report: MatchReport
) -> Iterator[tuple[MatchedEntity, str, Resolution]]:
    company = Resolution(tuple(dict.fromkeys(companies)), MatchedBy.CIK if companies else None)
    yield MatchedEntity.COMPANY, _COMPANY_LINK, company
    if claim.drug:
        drug = references.resolve_drug(claim.drug, company.id)
        if drug.status is Status.UNKNOWN and company.id and _is_new_code_name(claim.drug, claim.quote):
            created = _create_drug(session, references, claim.drug, company.id)
            report.drugs_created += 1
            drug = Resolution((created,), MatchedBy.NEW_CODE_NAME)
        yield MatchedEntity.DRUG, claim.drug, drug
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
    changes = {k: values[k] for k in ("status", "matched_id", "candidates", "matched_by", "resolved_at")}
    session.execute(
        insert(_MATCH)
        .values(**values)
        .on_conflict_do_update(index_elements=["claim_id", "entity_type", "name"], set_=changes)
    )


def resolve_claims(session_factory: sessionmaker[Session], now: Callable[[], datetime]) -> MatchReport:
    """Match every extractor claim's names; safe to re-run, and later runs pick up new names."""
    report = MatchReport()
    with session_factory.begin() as session:
        references = load_reference_data(session)
        links = _company_links(session)
        at = now()
        for claim in _claims(session):
            report.claims += 1
            for entity, name, result in _matches(
                session, references, claim, links.get(claim.source_document_id, []), report
            ):
                _store(session, claim.id, entity, name, result, at)
                report.statuses[f"{entity.value}:{result.status.value}"] += 1
    log_event(_log, "claims_resolved", **report.summary())
    return report
