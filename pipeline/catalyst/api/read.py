"""The read layer under the API and MCP server: published data only (spec: Provenance; ADR 0025).

Only the publish gate writes `catalyst` and its links, so those tables hold nothing from the
review queue (ADR 0003). Claims are read only through `catalyst_change` rows. A catalyst is shown
only when every value, its drug links included, matches its field's latest change, and each drug
link cites the claim of one of the catalyst's `drugs` changes. A trial is shown only when its link
cites a claim one of the catalyst's changes cites (ADR 0039). Review and check tables are never read.
"""

import base64
import binascii
import logging
from collections import defaultdict
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import Row, Select, func, or_, select, text, tuple_
from sqlalchemy.orm import Session, sessionmaker

from catalyst.api.access import FREE_HORIZON_DAYS, NOTICE, Access, calendar_window, shape
from catalyst.api.views import (
    CatalystDetail,
    CatalystPage,
    CatalystResult,
    ChangeView,
    CompanyCatalystPage,
    CompanyView,
    DrugView,
    IndicationView,
    SourceView,
    TrialView,
    WindowView,
)
from catalyst.db.tables import TABLES
from catalyst.logs import log_event
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CatalystDrug,
    CatalystField,
    CatalystStatus,
    CatalystTrial,
    CatalystType,
    Claim,
    Company,
    CompanyTicker,
    Drug,
    DrugName,
    Indication,
    SourceDocument,
    Trial,
)

_log = logging.getLogger(__name__)
_CATALYST = TABLES[Catalyst]
_CHANGE = TABLES[CatalystChange]
_LINK = TABLES[CatalystDrug]
_CLAIM = TABLES[Claim]
_DOC = TABLES[SourceDocument]
_COMPANY = TABLES[Company]
_TICKER = TABLES[CompanyTicker]
_DRUG = TABLES[Drug]
_DRUG_NAME = TABLES[DrugName]
_INDICATION = TABLES[Indication]
_TRIAL_LINK = TABLES[CatalystTrial]
_TRIAL = TABLES[Trial]

OPEN_STATUSES = (CatalystStatus.UPCOMING, CatalystStatus.SLIPPED)
BACKED_FIELDS = tuple(f.value for f in CatalystField if f is not CatalystField.DRUGS)
DRUGS = CatalystField.DRUGS.value
TRIALS = "trials"  # a source's `fields` entry for the quote that names a trial (ADR 0039)
DATE_FIELDS = frozenset(f.value for f in CatalystField if f.value.startswith("window_"))
MAX_LIMIT = 200
_BATCH = 1000  # catalysts per `published` read
_CIK_DIGITS = 10
# Every query that adds these also names the claim, as `claim_id`, from the row that points at it.
_SOURCE_COLUMNS = (
    _CLAIM.c.quote,
    _DOC.c.url,
    _DOC.c.publisher,
    _DOC.c.document_type,
    _DOC.c.source_tier,
    _DOC.c.published_at,
    _DOC.c.retrieved_at,
)


class InvalidQueryError(ValueError):
    """A request the API cannot answer as asked, such as a cursor that is not well formed."""


@dataclass(frozen=True)
class Query:
    """Filters for a list of catalysts. `company` and `drug` take an id or a name (ADR 0025)."""

    start: date | None = None
    end: date | None = None
    company: str | None = None
    drug: str | None = None
    types: tuple[CatalystType, ...] = ()
    cursor: str | None = None
    limit: int = 50

    def __post_init__(self) -> None:
        """Refuse a page size outside 1 to 200."""
        if not 1 <= self.limit <= MAX_LIMIT:
            raise InvalidQueryError(f"limit must be from 1 to {MAX_LIMIT}")


@contextmanager
def read_session(factory: sessionmaker[Session]) -> Generator[Session]:
    """Open a session whose transaction the database itself keeps read-only."""
    with factory() as session, session.begin():
        session.execute(text("SET TRANSACTION READ ONLY"))
        yield session


def upcoming(session: Session, query: Query, access: Access, today: date) -> CatalystPage:
    """Return upcoming and slipped catalysts whose window overlaps the caller's date window."""
    window = calendar_window(access, today, query.start, query.end)
    first, last = window if window else (query.start or today, _asked_end(query, access, today))
    details, cursor = _page(session, query, window) if window else ([], None)
    return CatalystPage(
        notice=NOTICE,
        access=access.value,
        window_from=first,
        window_to=last,
        catalysts=[shape(d, access) for d in details],
        next_cursor=cursor,
    )


def _asked_end(query: Query, access: Access, today: date) -> date | None:
    """Return the end to report for an empty window: never past the free horizon for a free caller."""
    if access is Access.PAID:
        return query.end
    horizon = today + timedelta(days=FREE_HORIZON_DAYS)
    return min(query.end or horizon, horizon)


def company_catalysts(
    session: Session, key: str, query: Query, access: Access, today: date
) -> CompanyCatalystPage | None:
    """Return one company's upcoming catalysts, or None when no company has this id or name."""
    company_id = _company_id(session, key)
    if company_id is None:
        return None
    page = upcoming(session, replace(query, company=str(company_id)), access, today)
    return CompanyCatalystPage(**dict(page), company=company_views(session, [company_id])[company_id])


def catalyst(session: Session, catalyst_id: UUID, access: Access) -> CatalystResult | None:
    """Return one catalyst, or None when it is not published with a source for every value."""
    rows = session.execute(select(_CATALYST).where(_CATALYST.c.id == catalyst_id)).all()
    details = _details(session, rows)
    if not details:
        return None
    return CatalystResult(notice=NOTICE, access=access.value, catalyst=shape(details[0], access))


def published(session: Session) -> list[CatalystDetail]:
    """Return every published catalyst, of any status, as a paid caller sees one by id (ADR 0034).

    The same rule as every read applies: a record with a value no source states is left out.
    Ordered by window start, then id; read in batches to stay inside Postgres's parameter limit.
    """
    ids = session.execute(select(_CATALYST.c.id).order_by(_CATALYST.c.window_start, _CATALYST.c.id))
    ordered = list(ids.scalars())
    details: list[CatalystDetail] = []
    for start in range(0, len(ordered), _BATCH):
        batch = ordered[start : start + _BATCH]
        statement = (
            select(_CATALYST)
            .where(_CATALYST.c.id.in_(batch))
            .order_by(_CATALYST.c.window_start, _CATALYST.c.id)
        )
        details += _details(session, session.execute(statement).all())
    return details


def _page(
    session: Session, query: Query, window: tuple[date, date | None]
) -> tuple[list[CatalystDetail], str | None]:
    statement = _filtered(session, query, window)
    if statement is None:
        return [], None
    if query.cursor is not None:
        after = _decode(query.cursor)
        statement = statement.where(tuple_(_CATALYST.c.window_start, _CATALYST.c.id) > after)
    rows = session.execute(statement.limit(query.limit + 1)).all()
    more = len(rows) > query.limit
    rows = rows[: query.limit]
    cursor = _encode(rows[-1].window_start, rows[-1].id) if more else None
    return _details(session, rows), cursor


def _filtered(session: Session, query: Query, window: tuple[date, date | None]) -> Select[Any] | None:
    first, last = window
    statement = (
        select(_CATALYST)
        .where(_CATALYST.c.status.in_(OPEN_STATUSES), _CATALYST.c.window_end >= first)
        .order_by(_CATALYST.c.window_start, _CATALYST.c.id)
    )
    if last is not None:
        statement = statement.where(_CATALYST.c.window_start <= last)
    if query.types:
        statement = statement.where(_CATALYST.c.catalyst_type.in_(query.types))
    if query.company is not None:
        company_id = _company_id(session, query.company)
        if company_id is None:
            return None
        statement = statement.where(_CATALYST.c.company_id == company_id)
    if query.drug is not None:
        linked = select(_LINK.c.catalyst_id).where(_LINK.c.drug_id.in_(_drug_ids(query.drug)))
        statement = statement.where(_CATALYST.c.id.in_(linked))
    return statement


def _as_uuid(key: str) -> UUID | None:
    try:
        return UUID(key)
    except ValueError:
        return None


def _company_id(session: Session, key: str) -> UUID | None:
    """Find a company by internal id, SEC number (digits), or current ticker."""
    key = key.strip()
    if (as_id := _as_uuid(key)) is not None:
        condition = _COMPANY.c.id == as_id
    elif key.isascii() and key.isdigit() and len(key) <= _CIK_DIGITS:
        condition = _COMPANY.c.cik == int(key)
    else:
        current = select(_TICKER.c.company_id).where(
            _TICKER.c.ticker == key.upper(), _TICKER.c.valid_to.is_(None)
        )
        condition = _COMPANY.c.id.in_(current)
    return session.execute(select(_COMPANY.c.id).where(condition)).scalar_one_or_none()


def _drug_ids(key: str) -> Select[Any]:
    """Drugs with this id, or whose preferred or other name matches apart from case."""
    if (as_id := _as_uuid(key)) is not None:
        return select(_DRUG.c.id).where(_DRUG.c.id == as_id)
    name = key.strip().lower()
    named = select(_DRUG_NAME.c.drug_id).where(func.lower(_DRUG_NAME.c.name) == name)
    return select(_DRUG.c.id).where(or_(func.lower(_DRUG.c.preferred_name) == name, _DRUG.c.id.in_(named)))


def _encode(start: date, catalyst_id: UUID) -> str:
    return base64.urlsafe_b64encode(f"{start.isoformat()}|{catalyst_id}".encode()).decode()


def _decode(cursor: str) -> tuple[date, UUID]:
    try:
        start, catalyst_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return date.fromisoformat(start), UUID(catalyst_id)
    except (ValueError, binascii.Error, UnicodeDecodeError) as error:
        raise InvalidQueryError("cursor is not well formed") from error


def _source(row: Row[Any], fields: list[str]) -> SourceView:
    return SourceView(
        claim_id=row.claim_id,
        url=row.url,
        publisher=row.publisher,
        document_type=row.document_type,
        source_tier=row.source_tier,
        published_at=row.published_at,
        retrieved_at=row.retrieved_at,
        quote=row.quote,
        fields=fields,
    )


def _details(session: Session, rows: Sequence[Row[Any]]) -> list[CatalystDetail]:
    """Build each catalyst with its sources; leave out, and log, any with an unbacked value."""
    ids = [row.id for row in rows]
    changes: dict[UUID, list[Row[Any]]] = defaultdict(list)
    for change in session.execute(
        select(_CHANGE, *_SOURCE_COLUMNS)
        .join(_CLAIM, _CLAIM.c.id == _CHANGE.c.claim_id)
        .join(_DOC, _DOC.c.id == _CLAIM.c.source_document_id)
        .where(_CHANGE.c.catalyst_id.in_(ids))
        .order_by(_CHANGE.c.learned_at, _CHANGE.c.id)
    ):
        changes[change.catalyst_id].append(change)
    drugs: dict[UUID, list[Row[Any]]] = defaultdict(list)
    links: Select[*tuple[Any, ...]] = (
        select(_LINK.c.catalyst_id, _LINK.c.role, _LINK.c.claim_id, _LINK.c.drug_id, _DRUG.c.preferred_name)
        .join(_DRUG, _DRUG.c.id == _LINK.c.drug_id)
        .where(_LINK.c.catalyst_id.in_(ids))
        .order_by(_DRUG.c.preferred_name, _LINK.c.role, _LINK.c.drug_id)
    )
    for link in session.execute(links):
        drugs[link.catalyst_id].append(link)
    trial_links: dict[UUID, list[Row[Any]]] = defaultdict(list)
    linked: Select[*tuple[Any, ...]] = select(
        _TRIAL_LINK.c.catalyst_id, _TRIAL_LINK.c.trial_id, _TRIAL_LINK.c.claim_id
    )
    for link in session.execute(linked.where(_TRIAL_LINK.c.catalyst_id.in_(ids))):
        trial_links[link.catalyst_id].append(link)
    trials = trial_views(session, list({link.trial_id for links in trial_links.values() for link in links}))
    companies = company_views(session, list({row.company_id for row in rows}))
    indications = _indications(session, [row.indication_id for row in rows if row.indication_id])
    built: list[CatalystDetail] = []
    for row in rows:
        shown_trials = _cited_trials(row.id, trial_links[row.id], changes[row.id], trials)
        detail = _build(row, changes[row.id], drugs[row.id], shown_trials, companies, indications)
        if detail is None:
            log_event(_log, "catalyst_withheld", catalyst_id=str(row.id), reason="a value has no source")
        else:
            built.append(detail)
    return built


def _cited_trials(
    catalyst_id: UUID, links: list[Row[Any]], changes: list[Row[Any]], trials: dict[UUID, TrialView]
) -> list[tuple[UUID, TrialView]]:
    """Return each linked trial with its link's claim, when one of the catalyst's changes cites it.

    Any other link is left out and logged, so a hand-added link never shows a claim the gate did
    not accept for this catalyst. Ordered by registry number.
    """
    cited = {change.claim_id for change in changes}
    shown: list[tuple[UUID, TrialView]] = []
    for link in links:
        if link.claim_id in cited:
            shown.append((link.claim_id, trials[link.trial_id]))
        else:
            log_event(_log, "trial_link_withheld", catalyst_id=str(catalyst_id), trial_id=str(link.trial_id))
    return sorted(shown, key=lambda pair: (pair[1].nct_id, str(pair[1].id)))


def _build(
    row: Row[Any],
    changes: list[Row[Any]],
    links: list[Row[Any]],
    trials: list[tuple[UUID, TrialView]],
    companies: dict[UUID, CompanyView],
    indications: dict[UUID, IndicationView],
) -> CatalystDetail | None:
    current = Catalyst.model_validate(row, from_attributes=True).model_dump(mode="json")
    latest = {change.field: change for change in changes}  # oldest first, so the last one wins
    if links or DRUGS in latest:  # once the gate has recorded links, an empty list is a value too
        current[DRUGS] = drugs_value(links)
    shown = [f for f in (*BACKED_FIELDS, DRUGS) if current.get(f) is not None]
    if any(f not in latest or latest[f].new_value != current[f] for f in shown):
        return None
    # A link's source is the `drugs` change that cites its claim; a claim no change cites is not read.
    drug_changes = {c.claim_id: c for c in changes if c.field == DRUGS}
    if any(link.claim_id not in drug_changes for link in links):
        return None
    backing: dict[UUID, tuple[Row[Any], list[str]]] = {}
    for name in shown:
        if name != DRUGS:
            backing.setdefault(latest[name].claim_id, (latest[name], []))[1].append(name)
    for link in links:
        fields = backing.setdefault(link.claim_id, (drug_changes[link.claim_id], []))[1]
        if DRUGS not in fields:
            fields.append(DRUGS)
    first_change = {c.claim_id: c for c in reversed(changes)}
    for claim_id, _ in trials:
        fields = backing.setdefault(claim_id, (first_change[claim_id], []))[1]
        if TRIALS not in fields:
            fields.append(TRIALS)
    history = [_change(c) for c in changes]
    return CatalystDetail(
        id=row.id,
        catalyst_type=row.catalyst_type,
        title=row.title,
        company=companies[row.company_id],
        indication=indications.get(row.indication_id) if row.indication_id else None,
        drugs=[DrugView(id=link.drug_id, name=link.preferred_name, role=link.role) for link in links],
        trials=[trial for _, trial in trials],
        window=WindowView(
            start=row.window_start,
            end=row.window_end,
            precision=row.window_precision,
            raw_text=row.window_raw_text,
        ),
        date_confidence=row.date_confidence,
        status=row.status,
        latest_date_change=_latest_move(changes),
        sources=[_source(source, fields) for source, fields in backing.values()],
        history=history,
    )


def drugs_value(links: Sequence[Row[Any]]) -> list[JsonValue]:
    """Return the links as a `drugs` change states them: `{drug_id, role}` pairs, sorted (ADR 0025)."""
    pairs = sorted({(str(link.drug_id), str(link.role)) for link in links})
    return [{"drug_id": drug_id, "role": role} for drug_id, role in pairs]


def _latest_move(changes: list[Row[Any]]) -> list[ChangeView]:
    """Every date row of the latest move: the rows citing the claim of the newest date move.

    `changes` come ordered by `learned_at`, then id, so the newest is the last and ties break the
    same way every time. Grouping by claim keeps one move whole if its rows have different times.
    """
    moves = [c for c in changes if c.field in DATE_FIELDS and c.old_value is not None]
    if not moves:
        return []
    claim = moves[-1].claim_id
    return [_change(c) for c in moves if c.claim_id == claim]


def _change(row: Row[Any]) -> ChangeView:
    return ChangeView(
        field=row.field,
        old_value=row.old_value,
        new_value=row.new_value,
        learned_at=row.learned_at,
        is_correction=row.is_correction,
        source=_source(row, [row.field]),
    )


def company_views(session: Session, ids: list[UUID]) -> dict[UUID, CompanyView]:
    """Return each company with its current ticker, by id."""
    current = _TICKER.c.valid_to.is_(None)
    statement = (
        select(_COMPANY.c.id, _COMPANY.c.name, _COMPANY.c.cik, _TICKER.c.ticker)
        .outerjoin(_TICKER, (_TICKER.c.company_id == _COMPANY.c.id) & current)
        .where(_COMPANY.c.id.in_(ids))
    )
    return {
        row.id: CompanyView(id=row.id, name=row.name, cik=row.cik, ticker=row.ticker)
        for row in session.execute(statement)
    }


def trial_views(session: Session, ids: list[UUID]) -> dict[UUID, TrialView]:
    """Return each trial's registry number and study name, by id; registry values stay out (ADR 0039)."""
    rows = session.execute(
        select(_TRIAL.c.id, _TRIAL.c.nct_id, _TRIAL.c.study_name).where(_TRIAL.c.id.in_(ids))
    )
    return {row.id: TrialView(id=row.id, nct_id=row.nct_id, study_name=row.study_name) for row in rows}


def _indications(session: Session, ids: list[UUID]) -> dict[UUID, IndicationView]:
    rows = session.execute(select(_INDICATION).where(_INDICATION.c.id.in_(ids)))
    return {
        row.id: IndicationView(
            id=row.id,
            mesh_id=row.mesh_id,
            name=row.name,
            line_of_therapy=row.line_of_therapy,
            biomarker=row.biomarker,
        )
        for row in rows
    }
