"""Gate 1 tools: choose the 50 companies to run end to end, and sample stored records to click.

Gate 1 (docs/quality-gates.md): 50 companies flow from source to the live database, and the
owner clicks 10 random records. ADR 0010 records how both are chosen.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.run import PilotCompany
from catalyst.db.tables import TABLES
from catalyst.schemas import Company, CompanyDocument, SourceDocument


def select_companies(
    pilot: list[PilotCompany], coverage: list[PilotCompany], count: int, seed: int
) -> list[PilotCompany]:
    """Return every pilot company plus a repeatable random draw from the coverage list, by SEC number."""
    pilot_ciks = {company.cik for company in pilot}
    pool = sorted((c for c in coverage if c.cik not in pilot_ciks), key=lambda c: c.cik)
    extra = count - len(pilot)
    if extra < 0 or extra > len(pool):
        raise ValueError(f"count must be between {len(pilot)} and {len(pilot) + len(pool)}, not {count}")
    drawn = random.Random(seed).sample(pool, extra)  # noqa: S311 - a repeatable sample, not security
    return sorted([*pilot, *drawn], key=lambda c: c.cik)


def _value(value: object) -> str:
    if isinstance(value, frozenset):
        return "[" + ", ".join(json.dumps(item, ensure_ascii=False) for item in sorted(value)) + "]"  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
    return json.dumps(value, ensure_ascii=False)


def companies_toml(companies: list[PilotCompany], header: str) -> str:
    """Return a company list in the format the collectors read."""
    lines = [f"# {header}"]
    for company in companies:
        lines += ["", "[[company]]"]
        for name, value in company.model_dump(exclude_none=True, exclude_defaults=True).items():
            lines.append(f"{name} = {_value(value)}")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class SampledRecord:
    """One stored document, as the owner sees it."""

    company: str
    publisher: str
    document_type: str
    published_at: datetime
    url: str
    sha256: str


def sample_records(session_factory: sessionmaker[Session], count: int, seed: int) -> list[SampledRecord]:
    """Return a repeatable random sample of stored documents, spread evenly across sources.

    Documents are grouped by source (the part of the source key before the first colon), each
    group is shuffled with `seed`, and records are taken from the groups in turn, so a source
    with few documents (SEC, FDA) is sampled as well as one with many (ClinicalTrials.gov).
    """
    documents, links, companies = TABLES[SourceDocument], TABLES[CompanyDocument], TABLES[Company]
    query = (
        select(
            documents.c.id,
            documents.c.source_key,
            companies.c.name,
            documents.c.publisher,
            documents.c.document_type,
            documents.c.published_at,
            documents.c.url,
            documents.c.sha256,
        )
        .outerjoin(links, links.c.source_document_id == documents.c.id)
        .outerjoin(companies, companies.c.id == links.c.company_id)
        .order_by(documents.c.id, companies.c.name)
    )
    with session_factory() as session:
        rows = {row.id: row for row in session.execute(query)}  # one row per document
    groups: dict[str, list[UUID]] = {}
    for row_id in sorted(rows):
        groups.setdefault(rows[row_id].source_key.split(":", 1)[0], []).append(row_id)
    rng = random.Random(seed)  # noqa: S311 - a repeatable sample, not security
    for source in sorted(groups):
        rng.shuffle(groups[source])
    chosen: list[UUID] = []
    while len(chosen) < count and any(groups.values()):
        for source in sorted(groups):
            if groups[source] and len(chosen) < count:
                chosen.append(groups[source].pop())
    return [
        SampledRecord(
            company=rows[i].name or "(not linked to a company)",
            publisher=rows[i].publisher,
            document_type=rows[i].document_type,
            published_at=rows[i].published_at,
            url=rows[i].url,
            sha256=rows[i].sha256,
        )
        for i in chosen
    ]


def sample_markdown(records: list[SampledRecord]) -> str:
    """Return the sample as a table the owner can click through."""
    lines = [
        "| Company | Source | Document | Published (UTC) | Link | Fingerprint |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for record in records:
        published = record.published_at.strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"| {record.company} | {record.publisher} | {record.document_type} | {published} "
            f"| [open]({record.url}) | {record.sha256[:12]} |"
        )
    return "\n".join(lines) + "\n"
