"""Collect only the trials our claims and catalysts name, by registry number (ADR 0040).

The sponsor-wide ClinicalTrials.gov run reads every trial a sponsor ever registered, which for the
largest sponsors takes hours. This run asks the registry for the trials we actually show: those a
catalyst links, those a claim's names resolved to, those a claim is about, and every registry
number written in a stored claim's quote or value, resolved yet or not. It stores each record the
same way the sponsor-wide run does, records the trial's study name, phase, and size from it
(ADR 0039), and links the record to a listed company when that company leads the trial.
"""

import json
import re
from collections.abc import Callable, Iterator
from functools import partial
from uuid import UUID

from sqlalchemy import Text, cast, or_, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.documents import FetchedDocument
from catalyst.collectors.http import PoliteHttpClient
from catalyst.collectors.sources import ctgov
from catalyst.db.tables import TABLES
from catalyst.references import recording_trials
from catalyst.schemas import (
    CatalystTrial,
    Claim,
    ClaimSubject,
    Company,
    DocumentMatch,
    EntityMatch,
    MatchedEntity,
    MatchStatus,
    Trial,
)

SOURCE = "ctgov-linked"
_NCT = re.compile(r"\bNCT\d{8}\b", re.IGNORECASE)
_NCT_SQL = "NCT[0-9]{8}"


def linked_nct_ids(session: Session) -> list[str]:
    """Return, sorted and once each, the registry numbers our claims and catalysts name."""
    trial, link, match, claim = TABLES[Trial], TABLES[CatalystTrial], TABLES[EntityMatch], TABLES[Claim]
    linked_ids = (
        select(link.c.trial_id)
        .union(
            select(match.c.matched_id).where(
                match.c.entity_type == MatchedEntity.TRIAL,
                match.c.status == MatchStatus.RESOLVED,
            ),
            select(claim.c.subject_id).where(
                claim.c.subject_type == ClaimSubject.TRIAL, claim.c.subject_id.is_not(None)
            ),
        )
        .subquery()
    )
    found = set(session.execute(select(trial.c.nct_id).where(trial.c.id.in_(select(linked_ids)))).scalars())
    value = cast(claim.c.value, Text)
    written = select(claim.c.quote, value).where(
        or_(claim.c.quote.op("~*")(_NCT_SQL), value.op("~*")(_NCT_SQL))
    )
    names = select(match.c.name).where(
        match.c.entity_type == MatchedEntity.TRIAL, match.c.name.op("~*")(_NCT_SQL)
    )
    texts = [text for row in session.execute(written) for text in row] + list(
        session.execute(names).scalars()
    )
    found.update(number.upper() for text in texts for number in _NCT.findall(text))
    return sorted(found)


def _sponsor_links(session_factory: sessionmaker[Session], sponsors: dict[str, int]) -> dict[str, UUID]:
    """Map each listed sponsor name to its company, for companies already stored."""
    company = TABLES[Company]
    with session_factory() as session:
        query = select(company.c.cik, company.c.id).where(company.c.cik.in_(set(sponsors.values())))
        stored: dict[int, UUID] = {row.cik: row.id for row in session.execute(query)}
    return {name: stored[cik] for name, cik in sponsors.items() if cik in stored}


def lead_sponsor_link(links: dict[str, UUID], fetched: FetchedDocument) -> tuple[UUID, DocumentMatch] | None:
    """Link a record to the listed company whose registered name is exactly its lead sponsor."""
    company_id = links.get(ctgov.lead_sponsor(json.loads(fetched.content)))
    return None if company_id is None else (company_id, DocumentMatch.CTGOV_LEAD_SPONSOR)


def linked_documents(
    session_factory: sessionmaker[Session], web: PoliteHttpClient, batch_size: int = ctgov.BY_ID_BATCH
) -> Iterator[FetchedDocument]:
    """Yield the current registry record of every linked trial, recording each trial once stored."""
    with session_factory() as session:
        nct_ids = linked_nct_ids(session)
    fetch: Callable[[], Iterator[FetchedDocument]] = partial(
        ctgov.collect_studies_by_id, web, nct_ids, batch_size
    )
    return recording_trials(session_factory, fetch)


def sponsor_links(
    session_factory: sessionmaker[Session], companies: dict[str, int]
) -> Callable[[FetchedDocument], tuple[UUID, DocumentMatch] | None]:
    """Return the per-record company link for listed sponsors (`companies`: sponsor name to CIK).

    The companies are read at the first record, inside the collector's run, so a database failure
    there is reported as this source's failure like any other.
    """
    links: dict[str, UUID] | None = None

    def link(fetched: FetchedDocument) -> tuple[UUID, DocumentMatch] | None:
        nonlocal links
        if links is None:
            links = _sponsor_links(session_factory, companies)
        return lead_sponsor_link(links, fetched)

    return link
