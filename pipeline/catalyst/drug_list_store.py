"""Fetch the drug list's records and store the plan in the drug tables (ADR 0035).

`catalyst load-drugs` (and the `drugs` source of `catalyst collect`) runs `load_drugs`: it reads
every NDA and BLA in Drugs@FDA (seven pages) and every covered sponsor's trials on
ClinicalTrials.gov (one search per registered sponsor name), plans the list with
`catalyst.drug_list`, and writes it in one transaction. A failed fetch stores nothing, because a
list built from part of the records could give a shared name to one company. Re-running adds
nothing new; every name keeps the records that give it.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import bindparam, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.http import PoliteHttpClient
from catalyst.collectors.sources import ctgov, openfda
from catalyst.db.tables import TABLES
from catalyst.drug_list import (
    DrugPlan,
    Merged,
    SourceRecord,
    fda_records,
    fda_sponsor_key,
    merge_with_existing,
    plan_drugs,
    registry_records,
)
from catalyst.logs import log_event
from catalyst.references import drug_refs
from catalyst.schemas import Company, CompanyName, Drug, DrugName, DrugNameSource, WithheldDrugName

_log = logging.getLogger(__name__)
SOURCES = ("openfda", "ctgov")
_DRUG, _NAME, _WITHHELD = TABLES[Drug], TABLES[DrugName], TABLES[WithheldDrugName]


class Listed(Protocol):
    """A company on the company list, with the names it is registered under (`PilotCompany`)."""

    @property
    def cik(self) -> int: ...  # noqa: D102

    @property
    def ctgov_sponsors(self) -> Collection[str]: ...  # noqa: D102

    @property
    def openfda_sponsors(self) -> Collection[str]: ...  # noqa: D102


@dataclass
class DrugListReport:
    """What one run of the drug list found and stored."""

    records: int = 0
    unlinked_records: int = 0
    comparators: int = 0
    split_drugs: int = 0
    drugs_created: int = 0
    names_added: int = 0
    names_on_file: int = 0
    withheld: int = 0
    conflicts: int = 0

    def summary(self) -> dict[str, Any]:
        """Return the counts as plain JSON."""
        return dict(self.__dict__)


def sponsor_maps(session: Session, companies: Iterable[Listed]) -> dict[DrugNameSource, dict[str, UUID]]:
    """Map each sponsor name to the one covered company it names, per source.

    Registry sponsors are the company list's registered names, exactly. FDA sponsors match a
    company's current or former name, or a hand-checked FDA name from the list, compared by
    `fda_sponsor_key`. A name that would name two companies names none.
    """
    company, name = TABLES[Company], TABLES[CompanyName]
    ids = {row.cik: row.id for row in session.execute(select(company.c.id, company.c.cik, company.c.name))}
    names: defaultdict[UUID, set[str]] = defaultdict(set)
    for row in session.execute(select(company.c.id, company.c.name)):
        names[row.id].add(row.name)
    for row in session.execute(select(name.c.company_id, name.c.name)):
        names[row.company_id].add(row.name)
    registry: defaultdict[str, set[UUID]] = defaultdict(set)
    fda: defaultdict[str, set[UUID]] = defaultdict(set)
    for listed in companies:
        if (company_id := ids.get(listed.cik)) is None:
            continue  # not collected yet: nothing to link its drugs to
        for sponsor in listed.ctgov_sponsors:
            registry[sponsor].add(company_id)
        for sponsor in {*listed.openfda_sponsors, *names[company_id]}:
            if key := fda_sponsor_key(sponsor):
                fda[key].add(company_id)
    return {
        DrugNameSource.CTGOV: {k: next(iter(v)) for k, v in registry.items() if len(v) == 1},
        DrugNameSource.OPENFDA: {k: next(iter(v)) for k, v in fda.items() if len(v) == 1},
    }


def store_plan(
    session: Session, plan: DrugPlan, now: datetime, *, replace_withheld: bool = True
) -> DrugListReport:
    """Write the plan against the drugs on file, and replace the withheld names.

    Writes go in batches, so a run over the whole coverage list is a few statements, not one per name.
    A run that read only some sources (`replace_withheld=False`) keeps the withheld names it did
    not see again, and only adds its own.
    """
    report = DrugListReport(
        records=plan.records,
        unlinked_records=plan.unlinked_records,
        comparators=plan.comparators,
        split_drugs=plan.split_drugs,
    )
    merged = merge_with_existing(plan, drug_refs(session))
    query = select(_NAME.c.id, _NAME.c.drug_id, _NAME.c.name, _NAME.c.source_refs)
    stored = {(r.drug_id, r.name): (r.id, list(r.source_refs or [])) for r in session.execute(query)}
    drugs: list[dict[str, Any]] = []
    names: list[dict[str, Any]] = []
    refs: dict[UUID, list[str]] = {}
    renames = [
        {"drug": w.drug_id, "shown": w.preferred_name} for w in merged.writes if w.drug_id and w.rename
    ]
    for write in merged.writes:
        drug_id = write.drug_id
        if drug_id is None:
            drug = Drug(company_id=write.company_id, preferred_name=write.preferred_name)
            drugs.append(drug.model_dump())
            drug_id = drug.id
        for planned in write.names:
            row = DrugName(
                drug_id=drug_id,
                name=planned.name,
                kind=planned.kind,
                source=planned.source,
                source_refs=planned.refs,
            )
            names.append(row.model_dump())
        for name, found in write.known_refs.items():
            row_id, have = stored[(drug_id, name)]
            if missing := [ref for ref in found if ref not in have]:
                refs[row_id] = have + missing
    if drugs:
        session.execute(insert(_DRUG), drugs)
    if names:
        session.execute(insert(_NAME), names)
    if refs:
        statement = update(_NAME).where(_NAME.c.id == bindparam("row")).values(source_refs=bindparam("refs"))
        session.execute(statement, [{"row": row, "refs": found} for row, found in refs.items()])
    if renames:
        rename = (
            update(_DRUG).where(_DRUG.c.id == bindparam("drug")).values(preferred_name=bindparam("shown"))
        )
        session.execute(rename, renames)
    report.drugs_created, report.names_added = len(drugs), len(names)
    report.names_on_file = sum(len(write.known_refs) for write in merged.writes)
    report.conflicts = merged.conflicts
    report.withheld = _store_withheld(session, merged, now, replace_withheld)
    return report


def _store_withheld(session: Session, merged: Merged, now: datetime, replace: bool) -> int:
    """Store the withheld names (replacing the old ones when `replace`); return how many are on file."""
    if replace:
        session.execute(delete(_WITHHELD))
    rows = [
        WithheldDrugName(
            name=withheld.name,
            reason=withheld.reason,
            company_ids=sorted(str(c) for c in withheld.company_ids),
            source_refs=sorted(withheld.refs),
            recorded_at=now,
        ).model_dump()
        for withheld in merged.withheld.values()
    ]
    if rows:
        session.execute(insert(_WITHHELD).on_conflict_do_nothing(index_elements=["name"]), rows)
    return session.execute(select(func.count()).select_from(_WITHHELD)).scalar_one()


def fetch_records(
    client: PoliteHttpClient, companies: Iterable[Listed], sources: frozenset[str]
) -> list[SourceRecord]:
    """Fetch and read the records of the named sources; any failed request raises."""
    if unknown := sources - set(SOURCES):
        raise ValueError(f"unknown drug-list sources: {', '.join(sorted(unknown))}")
    if "ctgov" in sources and "openfda" not in sources:
        raise ValueError("registry names need Drugs@FDA, which tells approved comparators apart")
    records: list[SourceRecord] = []
    if "openfda" in sources:
        for application in openfda.collect_drug_list(client):
            records += fda_records(application)
    if "ctgov" in sources:
        for sponsor in sorted({s for company in companies for s in company.ctgov_sponsors}):
            for study in ctgov.collect_lead_sponsor_studies(client, sponsor):
                records += registry_records(study)
    return records


def load_drugs(
    session_factory: sessionmaker[Session],
    client: PoliteHttpClient,
    companies: Collection[Listed],
    now: Callable[[], datetime],
    sources: frozenset[str] = frozenset(SOURCES),
) -> DrugListReport:
    """Fetch the records, plan the drug list, and store it in one transaction; safe to re-run."""
    records = fetch_records(client, companies, sources)
    with session_factory.begin() as session:
        plan = plan_drugs(records, sponsor_maps(session, companies))
        report = store_plan(session, plan, now(), replace_withheld=sources >= set(SOURCES))
    log_event(_log, "drug_list_loaded", **report.summary())
    return report
