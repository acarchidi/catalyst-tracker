"""Fill the reference tables the entity resolver reads (ADR 0015, part 3).

Company names and tickers come from the SEC company record each collection run already
fetches. Trials, their study names, and their phases come from each ClinicalTrials.gov record as
it is collected. Registry intervention lists are not imported as drugs here: they mix in
comparator drugs, placebos, and short forms such as "ETH", which would match the wrong company's
drug. The drug list (`catalyst.drug_list`, ADR 0035) reads them with rules that drop those.
Every write is safe to repeat.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from functools import partial
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.documents import FetchedDocument
from catalyst.db.tables import TABLES
from catalyst.logs import log_event
from catalyst.resolve import CompanyRef, DrugRef, ReferenceData, TrialRef
from catalyst.schemas import (
    Company,
    CompanyDocument,
    CompanyName,
    CompanyTicker,
    Drug,
    DrugName,
    DrugNameKind,
    SourceDocument,
    Trial,
    TrialPhase,
)

_log = logging.getLogger(__name__)
_COMPANY_NAME = TABLES[CompanyName]
_COMPANY_TICKER = TABLES[CompanyTicker]
_TRIAL = TABLES[Trial]
_EXCHANGE_CHARS = 20  # CompanyTicker.exchange limit


@dataclass(frozen=True)
class CompanyIdentity:
    """A company's current name, its former names with their dates, and its current tickers."""

    name: str
    former_names: tuple[tuple[str, date | None, date | None], ...]
    tickers: tuple[tuple[str, str | None], ...]


class _FormerName(BaseModel):
    name: str
    start: str | None = Field(default=None, alias="from")
    end: str | None = Field(default=None, alias="to")


class _Identity(BaseModel):
    name: str
    tickers: list[str] = []
    exchanges: list[str | None] = []
    former_names: list[_FormerName] = Field(default=[], alias="formerNames")


def _day(timestamp: str | None) -> date | None:
    return date.fromisoformat(timestamp[:10]) if timestamp else None


def company_identity(submissions_json: bytes) -> CompanyIdentity:
    """Read the name, former names, and tickers from an SEC company record (submissions JSON)."""
    data = _Identity.model_validate_json(submissions_json)
    exchanges = [*data.exchanges, *([None] * len(data.tickers))]
    return CompanyIdentity(
        name=data.name,
        former_names=tuple((f.name, _day(f.start), _day(f.end)) for f in data.former_names),
        tickers=tuple((t.upper(), exchanges[i]) for i, t in enumerate(data.tickers)),
    )


def _close(session: Session, row_ids: list[UUID], today: date) -> None:
    """End each ticker period the day before `today`, or on `today` if it began then."""
    if row_ids:
        session.execute(
            update(_COMPANY_TICKER)
            .where(_COMPANY_TICKER.c.id.in_(row_ids))
            .values(valid_to=func.greatest(_COMPANY_TICKER.c.valid_from, today - timedelta(days=1)))
        )


def _current_tickers(session: Session) -> list[tuple[UUID, UUID, str]]:
    query = select(_COMPANY_TICKER.c.id, _COMPANY_TICKER.c.company_id, _COMPANY_TICKER.c.ticker).where(
        _COMPANY_TICKER.c.valid_to.is_(None)
    )
    return [(row.id, row.company_id, row.ticker) for row in session.execute(query)]


def _valid[Row: (CompanyName, CompanyTicker)](build: Callable[[], Row], what: str) -> Row | None:
    """Build one row from outside data; skip it, with a log line, if it breaks a rule."""
    try:
        return build()
    except ValidationError as error:
        log_event(_log, "reference_skipped", what=what, error=str(error.errors()[0]["msg"]))
        return None


def record_company_identity(
    session: Session, company_id: UUID, identity: CompanyIdentity, today: date
) -> None:
    """Store names and tickers; SEC's current tickers are the truth as of `today`.

    A ticker the company no longer lists is closed the day before. A ticker another company still
    holds in our tables is closed for that company first, because SEC now lists it for this one.
    A name or ticker that breaks a rule (a ticker with a slash, a former name whose dates run
    backwards) is skipped and logged; an exchange name too long to store is left out.
    """
    candidates = [(identity.name, None, None), *identity.former_names]
    names = [
        _valid(partial(CompanyName, company_id=company_id, name=name, valid_from=start, valid_to=end), name)
        for name, start, end in candidates
    ]
    for name in filter(None, names):
        session.execute(
            insert(_COMPANY_NAME)
            .values(**name.model_dump())
            .on_conflict_do_nothing(index_elements=["company_id", "name"])
        )
    rows = [
        _valid(
            partial(
                CompanyTicker,
                company_id=company_id,
                ticker=ticker,
                exchange=exchange if exchange and len(exchange) <= _EXCHANGE_CHARS else None,
                valid_from=today,
            ),
            ticker,
        )
        for ticker, exchange in identity.tickers
    ]
    listed = {row.ticker: row for row in rows if row is not None}
    current = _current_tickers(session)
    held = {ticker for _, owner, ticker in current if owner == company_id}
    _close(
        session,
        [row for row, owner, ticker in current if owner == company_id and ticker not in listed],
        today,
    )
    _close(
        session, [row for row, owner, ticker in current if owner != company_id and ticker in listed], today
    )
    for ticker, row in listed.items():
        if ticker in held:
            continue
        session.execute(
            insert(_COMPANY_TICKER)
            .values(**row.model_dump())
            .on_conflict_do_nothing(index_elements=["company_id", "ticker", "valid_from"])
        )


_PHASES: dict[tuple[str, ...], TrialPhase] = {
    ("EARLY_PHASE1",): TrialPhase.EARLY_PHASE_1,
    ("PHASE1",): TrialPhase.PHASE_1,
    ("PHASE1", "PHASE2"): TrialPhase.PHASE_1_2,
    ("PHASE2",): TrialPhase.PHASE_2,
    ("PHASE2", "PHASE3"): TrialPhase.PHASE_2_3,
    ("PHASE3",): TrialPhase.PHASE_3,
    ("PHASE4",): TrialPhase.PHASE_4,
    ("NA",): TrialPhase.NOT_APPLICABLE,
}


def trial_from_study(study: dict[str, Any]) -> Trial:
    """Read a trial's registry number, the company's study name (acronym), and its phase."""
    protocol = study["protocolSection"]
    identification = protocol["identificationModule"]
    acronym = (identification.get("acronym") or "").strip()
    phases = tuple(protocol.get("designModule", {}).get("phases") or ())
    return Trial(nct_id=identification["nctId"], study_name=acronym or None, phase=_PHASES.get(phases))


def record_trial(session: Session, trial: Trial) -> None:
    """Store the trial, or update its study name and phase if the registry changed them."""
    values = trial.model_dump()
    session.execute(
        insert(_TRIAL)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["nct_id"], set_={"study_name": values["study_name"], "phase": values["phase"]}
        )
    )


def recording_trials(
    session_factory: sessionmaker[Session], documents: Callable[[], Iterable[FetchedDocument]]
) -> Iterator[FetchedDocument]:
    """Pass registry records through to the collector, recording each one's trial on the way.

    The trial is written before the collector stores the record. If storing then fails, the
    trial row stays without its record until the next run stores it; nothing is duplicated.
    """
    for fetched in documents():
        try:
            trial = trial_from_study(json.loads(fetched.content))
            with session_factory.begin() as session:
                record_trial(session, trial)
        except (ValidationError, KeyError, ValueError, SQLAlchemyError) as error:
            # One bad registry record must not stop the company's other trials (PR #25 review).
            log_event(_log, "reference_skipped", what="trial", error=f"{type(error).__name__}: {error}"[:300])
        yield fetched


def company_refs(session: Session) -> list[CompanyRef]:
    """Read every company with its current and former names and its dated tickers."""
    names: defaultdict[UUID, list[str]] = defaultdict(list)
    for row in session.execute(select(_COMPANY_NAME.c.company_id, _COMPANY_NAME.c.name)):
        names[row.company_id].append(row.name)
    tickers: defaultdict[UUID, list[tuple[str, date, date | None]]] = defaultdict(list)
    for row in session.execute(select(_COMPANY_TICKER)).mappings():
        tickers[row["company_id"]].append((row["ticker"], row["valid_from"], row["valid_to"]))
    company = TABLES[Company]
    return [
        CompanyRef(
            id=row.id,
            cik=row.cik,
            names=tuple(dict.fromkeys([row.name, *names[row.id]])),
            tickers=tuple(tickers[row.id]),
        )
        for row in session.execute(select(company.c.id, company.c.cik, company.c.name))
    ]


def drug_refs(session: Session) -> list[DrugRef]:
    """Read every drug with every name it goes by, as the resolver matches them."""
    drug, drug_name = TABLES[Drug], TABLES[DrugName]
    names: defaultdict[UUID, list[tuple[str, DrugNameKind]]] = defaultdict(list)
    for row in session.execute(select(drug_name.c.drug_id, drug_name.c.name, drug_name.c.kind)):
        names[row.drug_id].append((row.name, DrugNameKind(row.kind)))
    return [
        DrugRef(id=row.id, company_id=row.company_id, names=tuple(names[row.id]))
        for row in session.execute(select(drug.c.id, drug.c.company_id))
    ]


def _trials(session: Session) -> list[TrialRef]:
    document, link = TABLES[SourceDocument], TABLES[CompanyDocument]
    sponsors: defaultdict[str, set[UUID]] = defaultdict(set)
    query = (
        select(document.c.source_key, link.c.company_id)
        .join(link, link.c.source_document_id == document.c.id)
        .where(document.c.source_key.startswith("ctgov:"))
    )
    for row in session.execute(query):
        sponsors[row.source_key.split(":")[1]].add(row.company_id)
    return [
        TrialRef(
            id=row.id,
            nct_id=row.nct_id,
            study_name=row.study_name,
            company_ids=frozenset(sponsors[row.nct_id]),
        )
        for row in session.execute(select(_TRIAL.c.id, _TRIAL.c.nct_id, _TRIAL.c.study_name))
    ]


def load_reference_data(session: Session) -> ReferenceData:
    """Read every company, drug, and trial the resolver can match against."""
    references = ReferenceData(
        companies=company_refs(session), drugs=drug_refs(session), trials=_trials(session)
    )
    log_event(_log, "references_loaded")
    return references
