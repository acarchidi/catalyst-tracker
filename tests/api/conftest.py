"""Published rows for the read API tests, written the way the publish gate will write them.

A published value is a `catalyst` field backed by its latest `catalyst_change` row, whose
claim gives the source and quote. The seed also holds things that must never be shown: a
claim nothing published points at, its check results, and a catalyst missing a source.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

import pytest
from pydantic import JsonValue
from sqlalchemy import insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.db.tables import TABLES
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CatalystDrug,
    CatalystField,
    CatalystStatus,
    CatalystType,
    CheckOutcome,
    Claim,
    ClaimCheck,
    ClaimSubject,
    Company,
    CompanyTicker,
    DateConfidence,
    DatePrecision,
    Drug,
    DrugName,
    DrugNameKind,
    DrugRole,
    Entity,
    SourceDocument,
    SourceTier,
)

TODAY = date(2026, 10, 17)
FIRST = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
MOVED = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
MOVED_AGAIN = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
UNPUBLISHED_QUOTE = "UNPUBLISHED: this sentence is only in a claim nothing published points at"
BACKED = [f for f in CatalystField if f is not CatalystField.DRUGS]


@dataclass(frozen=True)
class Seed:
    """Ids of the seeded records."""

    example: UUID
    other: UUID
    drug: UUID
    slipped: UUID  # EXB topline readout, moved from 3Q26 to 4Q26; in the free window
    pdufa: UUID  # EXB FDA date in March 2027; paid horizon only
    adcom: UUID  # OTH advisory committee on 2026-11-05; in the free window
    unsourced: UUID  # OTH catalyst whose title has no claim; never shown
    resolved: UUID  # EXB, already resolved; not upcoming
    unpublished_claim: UUID


def drugs_value(*links: tuple[UUID, DrugRole]) -> list[JsonValue]:
    """Return a `drugs` change row's `new_value`: the links as `{drug_id, role}`, sorted (ADR 0025)."""
    ordered = sorted((str(drug), role.value) for drug, role in links)
    return [{"drug_id": drug, "role": role} for drug, role in ordered]


def _add(session: Session, record: Entity) -> None:
    session.execute(insert(TABLES[type(record)]).values(**record.model_dump()))


def _document(session: Session, n: int, tier: SourceTier) -> UUID:
    doc = SourceDocument(
        source_key=f"sec:0000000000-26-00000{n}:ex99-1.htm",
        url=f"https://www.sec.gov/Archives/edgar/data/1111/00000{n}/ex99-1.htm",
        publisher="SEC EDGAR" if tier is SourceTier.REGULATOR_OR_SEC else "PR Newswire",
        source_tier=tier,
        document_type="8-K EX-99.1",
        published_at=FIRST,
        retrieved_at=FIRST,
        sha256=str(n) * 64,
        snapshot_key=f"sha256/{n}{n}/{str(n) * 64}",
        content_type="text/html",
    )
    _add(session, doc)
    return doc.id


def _claim(session: Session, document: UUID, quote: str, field: str) -> UUID:
    claim = Claim(
        source_document_id=document,
        subject_type=ClaimSubject.CATALYST,
        field=field,
        value={"quote": quote},
        quote=quote,
        quote_start=0,
        quote_end=len(quote),
        agent="extractor",
        prompt_id="extract_catalysts",
        prompt_version="1.0.0",
        provider="anthropic",
        model="m",
        extracted_at=FIRST,
    )
    _add(session, claim)
    return claim.id


def _publish(
    session: Session,
    catalyst: Catalyst,
    claim: UUID,
    *,
    skip: tuple[str, ...] = (),
    initial: Catalyst | None = None,
) -> None:
    """Write the catalyst and a first-value change row, with its claim, for each field.

    `initial` gives the first values when later change rows moved them.
    """
    _add(session, catalyst)
    values = (initial or catalyst).model_dump(mode="json")
    for field in BACKED:
        if field.value in skip or values[field.value] is None:
            continue
        change = CatalystChange(
            catalyst_id=catalyst.id,
            field=field,
            new_value=values[field.value],
            claim_id=claim,
            learned_at=FIRST,
        )
        _add(session, change)


def _company(session: Session, cik: int, name: str, ticker: str) -> UUID:
    company = Company(cik=cik, name=name, sic_code="2834")
    _add(session, company)
    _add(session, CompanyTicker(company_id=company.id, ticker=ticker, valid_from=date(2020, 1, 1)))
    return company.id


def _catalyst(
    company: UUID,
    kind: CatalystType,
    window: tuple[date, date, DatePrecision, str],
    status: CatalystStatus = CatalystStatus.UPCOMING,
) -> Catalyst:
    start, end, precision, raw = window
    return Catalyst(
        catalyst_type=kind,
        company_id=company,
        title=f"{kind.value} for {raw}",
        window_start=start,
        window_end=end,
        window_precision=precision,
        window_raw_text=raw,
        date_confidence=DateConfidence.COMPANY_CONFIRMED,
        status=status,
    )


def _slipped(session: Session, example: UUID, sec: UUID, wire: UUID, drug: UUID) -> UUID:
    """Write a readout first given as 3Q26, then moved to 4Q26 by a later release."""
    first = _claim(session, sec, "topline data in the third quarter of 2026", "catalyst:1")
    moved = _claim(session, wire, "topline data now expected in the fourth quarter of 2026", "catalyst:2")
    q3 = (date(2026, 7, 1), date(2026, 9, 30), DatePrecision.QUARTER, "3Q26")
    original = _catalyst(example, CatalystType.TOPLINE_READOUT, q3)
    catalyst = original.model_copy(
        update={
            "window_start": date(2026, 10, 1),
            "window_end": date(2026, 12, 31),
            "window_raw_text": "4Q26",
            "status": CatalystStatus.SLIPPED,
        }
    )
    _publish(session, catalyst, first, initial=original)
    for field, old, new in [
        (CatalystField.WINDOW_START, "2026-07-01", "2026-10-01"),
        (CatalystField.WINDOW_END, "2026-09-30", "2026-12-31"),
        (CatalystField.WINDOW_RAW_TEXT, "3Q26", "4Q26"),
        (CatalystField.STATUS, "upcoming", "slipped"),
    ]:
        _add(
            session,
            CatalystChange(
                catalyst_id=catalyst.id,
                field=field,
                old_value=old,
                new_value=new,
                claim_id=moved,
                learned_at=MOVED,
            ),
        )
    # The first claim's window rows still say 3Q26; the API must show the moved values' source.
    _add(session, CatalystDrug(catalyst_id=catalyst.id, drug_id=drug, role=DrugRole.ALONE, claim_id=first))
    _add(
        session,
        CatalystChange(
            catalyst_id=catalyst.id,
            field=CatalystField.DRUGS,
            new_value=drugs_value((drug, DrugRole.ALONE)),
            claim_id=first,
            learned_at=FIRST,
        ),
    )
    return catalyst.id


@pytest.fixture
def seed(session_factory: sessionmaker[Session]) -> Seed:
    """Write the published catalysts and the unpublished noise."""
    with session_factory.begin() as session:
        sec = _document(session, 1, SourceTier.REGULATOR_OR_SEC)
        wire = _document(session, 2, SourceTier.WIRE)
        example = _company(session, 1111, "Example Bio, Inc.", "EXB")
        other = _company(session, 2222, "Other Therapeutics", "OTH")
        drug = Drug(company_id=example, preferred_name="exampumab")
        _add(session, drug)
        _add(session, DrugName(drug_id=drug.id, name="EXB-101", kind=DrugNameKind.CODE))
        slipped = _slipped(session, example, sec, wire, drug.id)

        pdufa = _catalyst(
            example,
            CatalystType.PDUFA_DATE,
            (date(2027, 3, 15), date(2027, 3, 15), DatePrecision.DAY, "March 15, 2027"),
        ).model_copy(update={"date_confidence": DateConfidence.REGULATOR_SCHEDULED})
        _publish(session, pdufa, _claim(session, sec, "a PDUFA target action date of March 15, 2027", "c:3"))

        nov5 = (date(2026, 11, 5), date(2026, 11, 5), DatePrecision.DAY, "November 5, 2026")
        adcom = _catalyst(other, CatalystType.ADVISORY_COMMITTEE, nov5)
        _publish(session, adcom, _claim(session, sec, "an advisory committee on November 5, 2026", "c:4"))

        oct20 = (date(2026, 10, 20), date(2026, 10, 20), DatePrecision.DAY, "October 20, 2026")
        unsourced = _catalyst(other, CatalystType.TOPLINE_READOUT, oct20)
        _publish(session, unsourced, _claim(session, sec, "data on October 20, 2026", "c:5"), skip=("title",))

        oct10 = (date(2026, 10, 10), date(2026, 10, 10), DatePrecision.DAY, "October 10, 2026")
        resolved = _catalyst(example, CatalystType.CONFERENCE_PRESENTATION, oct10, CatalystStatus.RESOLVED)
        _publish(session, resolved, _claim(session, sec, "a presentation on October 10, 2026", "c:6"))

        unpublished = _claim(session, sec, UNPUBLISHED_QUOTE, "c:7")
        _add(
            session,
            ClaimCheck(
                claim_id=unpublished,
                check_name="window_not_past",
                outcome=CheckOutcome.PASS,
                checked_at=FIRST,
            ),
        )
    return Seed(example, other, drug.id, slipped, pdufa.id, adcom.id, unsourced.id, resolved.id, unpublished)
