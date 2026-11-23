"""Match the names in extracted claims to companies, drugs, and trials (ADR 0015, part 4).

Plain code over the reference tables. For each catalyst claim the extractor stored:
- the company is the one its source document is linked to;
- the drug name is matched by the rules in `catalyst.resolve`;
- every trial its quote names (registry number, or study name in capitals) is matched.

Each name gets one `entity_match` row, updated on later runs as the reference tables grow.
The only record this job creates is a drug of the claim's company, for a name no record has:
- a code name its release quotes in capitals (ADR 0015, part 4); or
- a drug named by the company (ADR 0060): any name that is a drug's name by the rules of
  `catalyst.company_drugs`, when the stored copy of the company's own document names it in the
  claim's quote or just before it, by the evidence of `drug_named_near_quote`.
Other unknown names wait for review.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import httpx
from pydantic import ValidationError
from sqlalchemy import and_, case, delete, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from catalyst.company_drugs import NOT_DRUG_PREFIXES, Stated, stated_drugs
from catalyst.db.tables import TABLES
from catalyst.drug_list import ListedName
from catalyst.extract.agent import ROLE
from catalyst.extract.text import document_text
from catalyst.logs import log_alert, log_event
from catalyst.references import load_reference_data
from catalyst.resolve import (
    DrugRef,
    MatchedBy,
    ReferenceData,
    Resolution,
    Status,
    name_key,
    whole_word_pattern,
)
from catalyst.schemas import (
    Claim,
    CompanyDocument,
    DocumentMatch,
    Drug,
    DrugName,
    DrugNameKind,
    DrugNameSource,
    EntityMatch,
    MatchedEntity,
    SourceDocument,
    WithheldDrugName,
)
from catalyst.snapshots import SnapshotStore, SnapshotStoreError
from catalyst.verify.drug import DrugNames, drug_named, other_drugs
from catalyst.verify.rules import Outcome

_log = logging.getLogger(__name__)
_MATCH = TABLES[EntityMatch]
_COMPANY_LINK = "source document"
# A code name: letters, an optional dash or space, then digits, as in SPY001, VX-548, INS 1007.
_CODE_NAME = re.compile(r"^(?P<prefix>[A-Z]{1,6})[\s\-\u2010-\u2015]?(?P<number>\d{2,6})[A-Z]?$")
_NOT_DRUG_PREFIXES = NOT_DRUG_PREFIXES  # the list lives with the other name rules (ADR 0060)
# A number part that is a year (1900 to 2099) names a date or a meeting, not a drug (CP4 round 2).
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_NAME_CHARS = 300  # EntityMatch.name limit
_CREATED = (MatchedBy.NEW_CODE_NAME.value, MatchedBy.COMPANY_NAMED.value)  # rules that add a drug
_SHOWN_FIRST = [k.value for k in (DrugNameKind.INN, DrugNameKind.BRAND, DrugNameKind.CODE)]
# Between two names of one drug as a document writes them together: "BRAND® (generic)".
_BRACKET = r"[\s®™©℠]*[(\[][^()\[\]]{0,40}?"


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
    start: int = 0
    end: int = 0


def _drug_name(value: object) -> str | None:
    drug = cast(dict[str, object], value).get("drug") if isinstance(value, dict) else None
    return drug if isinstance(drug, str) and drug.strip() else None


def _claims(session: Session) -> list[_Claim]:
    claim = TABLES[Claim]
    columns = (claim.c.id, claim.c.source_document_id, claim.c.quote, claim.c.value)
    query = select(*columns, claim.c.quote_start, claim.c.quote_end).where(
        claim.c.agent == ROLE, claim.c.field.startswith("catalyst:")
    )
    return [
        _Claim(
            row.id, row.source_document_id, row.quote, _drug_name(row.value), row.quote_start, row.quote_end
        )
        for row in session.execute(query.order_by(claim.c.extracted_at, claim.c.id))
    ]


class _CompanyNamed:
    """The rule that names a drug for its company from its own document (ADR 0060).

    Reads, besides the reference tables: the stored copy of a claim's document (at most once a
    run, None when it cannot be read), the drugs each document's claims name, and the names
    withheld as shared by two companies' records.
    """

    def __init__(
        self, session: Session, store: SnapshotStore, references: ReferenceData, claims: Sequence[_Claim]
    ) -> None:
        self._session, self._store, self._references = session, store, references
        self._texts: dict[UUID, str | None] = {}
        self._beside: dict[UUID, list[tuple[UUID, str | None]]] = {}
        for claim in claims:
            self._beside.setdefault(claim.source_document_id, []).append((claim.id, claim.drug))
        withheld = TABLES[WithheldDrugName]
        self._withheld = frozenset(name_key(row.name) for row in session.execute(select(withheld.c.name)))

    def _text(self, document_id: UUID) -> str | None:
        if document_id not in self._texts:
            document = TABLES[SourceDocument]
            query = select(document.c.snapshot_key, document.c.content_type)
            row = self._session.execute(query.where(document.c.id == document_id)).one()
            try:
                self._texts[document_id] = document_text(self._store.get(row.snapshot_key), row.content_type)
            except (SnapshotStoreError, httpx.HTTPError, OSError, ValueError) as error:
                log_event(_log, "company_named_unreadable", key=row.snapshot_key, error=type(error).__name__)
                self._texts[document_id] = None
        return self._texts[document_id]

    def written(self, claim: _Claim, company_id: UUID, names: Sequence[str]) -> bool:
        """Say whether the company's own document names the drug at the claim's quote (part 1).

        The stored copy must name the drug in the quote or just before it, as
        `drug_named_near_quote` reads it (no other drug of the company or of the release named
        after it); must write every name somewhere as a whole word, not joined to more by a
        hyphen; and must write each further name beside the first, one of them in a bracket,
        since only that says they are one drug.
        """
        text = self._text(claim.source_document_id)
        if text is None or claim.drug is None:
            return False
        beside = [drug for other, drug in self._beside[claim.source_document_id] if other != claim.id]
        stated = stated_drugs(claim.drug)
        every = [n.name for drug in stated.drugs for n in drug] if stated else []
        mine = DrugNames(claim.drug, (claim.drug, *every))  # a combination's own drugs are not "another drug"
        others = other_drugs(self._references.drugs(), {company_id}, mine, beside)
        near = drug_named(
            text, claim.quote, claim.start, claim.end, DrugNames(names[0], tuple(names)), others
        )
        first = whole_word_pattern(names[0]).pattern
        return (
            near.outcome is Outcome.PASS
            and all(whole_word_pattern(name).search(text) for name in names)
            and all(
                re.search(f"{first}{_BRACKET}{also}|{also}{_BRACKET}{first}", text, re.IGNORECASE)
                for also in (whole_word_pattern(name).pattern for name in names[1:])
            )
        )

    def free(self, name: str) -> bool:
        """Say whether nothing else on file goes by the name: no withheld name, study, company, or ticker."""
        return (
            name_key(name) not in self._withheld
            and self._references.resolve_trial(name).status is Status.UNKNOWN
            and self._references.resolve_company(ticker=name, name=name).status is Status.UNKNOWN
        )

    def name(self, claim: _Claim, company_id: UUID, drug_id: UUID | None, new: Sequence[ListedName]) -> UUID:
        """Store new names for the company's drug on file (`drug_id`), or for a new drug of its own.

        Each name is marked as found in a claim's document, with that claim as its record.
        """
        if drug_id is None:
            drug = Drug(company_id=company_id, preferred_name=new[0].name)
            self._session.execute(insert(TABLES[Drug]).values(**drug.model_dump()))
            drug_id, ref = drug.id, DrugRef(id=drug.id, company_id=company_id, names=())
        else:
            ref = self._references.drug(drug_id)
        for name in new:
            row = DrugName(drug_id=drug_id, name=name.name, kind=name.kind, source_refs=[f"claim:{claim.id}"])
            self._session.execute(insert(TABLES[DrugName]).values(**row.model_dump()))
        self._references.add_drug(replace(ref, names=(*ref.names, *((n.name, n.kind) for n in new))))
        self._show(drug_id)
        log_event(_log, "drug_named_by_company", drug=str(drug_id), names=len(new), claim=str(claim.id))
        return drug_id

    def _show(self, drug_id: UUID) -> None:
        """Show a drug by the best name claims give it: a generic name, else a brand, else a code.

        Spec, Data model: "generic name if any, else code name". Only a shown name that itself
        came from a claim is replaced; a name the drug list chose (ADR 0035, part 5) stays.
        """
        drug, names = TABLES[Drug], TABLES[DrugName]
        shown = self._session.execute(select(drug.c.preferred_name).where(drug.c.id == drug_id)).scalar_one()
        query = select(names.c.name, names.c.kind).where(
            names.c.drug_id == drug_id, names.c.source == DrugNameSource.CLAIM.value
        )
        kinds = {row.name: _SHOWN_FIRST.index(row.kind) for row in self._session.execute(query)}
        best = min(kinds, key=lambda name: (kinds[name], name), default=shown)
        if shown in kinds and kinds[best] < kinds[shown]:
            self._session.execute(update(drug).where(drug.c.id == drug_id).values(preferred_name=best))

    def _spare(self, drug_id: UUID) -> bool:
        """Say whether the record is named only by claims and nothing but its names points to it."""
        names = TABLES[DrugName]
        sources = self._session.execute(select(names.c.source).where(names.c.drug_id == drug_id)).scalars()
        keys = [
            key.parent
            for table in TABLES.values()
            for key in table.foreign_keys
            if table is not names and key.column.table is TABLES[Drug]
        ]
        linked = any(self._session.execute(select(k).where(k == drug_id).limit(1)).first() for k in keys)
        return set(sources) == {DrugNameSource.CLAIM.value} and not linked

    def join(self, first: UUID, second: UUID) -> UUID | None:
        """Join two records of one drug of the company into one (part 4); None when a person must.

        The record that goes must be spare: named only by claims, with no catalyst, combination,
        or target linked to it, so no published row changes. Its names and its claims' matches
        move to the record that stays.
        """
        spare = sorted(drug for drug in (first, second) if self._spare(drug))
        if not spare:
            return None
        gone = spare[-1]
        keep = second if gone == first else first
        names = TABLES[DrugName]
        have = select(names.c.name).where(names.c.drug_id == keep)
        self._session.execute(delete(names).where(names.c.drug_id == gone, names.c.name.in_(have)))
        self._session.execute(update(names).where(names.c.drug_id == gone).values(drug_id=keep))
        matched = update(_MATCH).where(_MATCH.c.matched_id == gone)
        self._session.execute(matched.values(matched_id=keep, candidates=[str(keep)]))
        self._session.execute(delete(TABLES[Drug]).where(TABLES[Drug].c.id == gone))
        self._references.join_drugs(keep, gone)
        self._show(keep)
        log_event(_log, "drug_records_joined", kept=str(keep), joined=str(gone))
        return keep


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


# "TRYNGOLZA (olezarsen)": one drug written by two of its names (ADR 0035).
_TWO_NAMES = re.compile(r"^(?P<first>[^()]+?)\s*\((?P<second>[^()]+)\)$")


def _two_names(references: ReferenceData, name: str, company_id: UUID | None) -> Resolution | None:
    """Match "A (B)": one drug when both names are that drug's, ambiguous when they name two.

    A part that matches nothing leaves the whole unknown, so a person checks it.
    """
    shape = _TWO_NAMES.match(name.strip())
    if shape is None:
        return None
    first, second = (known_drug(references, shape.group(g), company_id) for g in ("first", "second"))
    if Status.UNKNOWN in (first.status, second.status):
        return None
    if first.id is not None and first.id == second.id:
        return first
    candidates = tuple(dict.fromkeys(first.candidates + second.candidates))
    return Resolution(candidates, first.matched_by, needs_review=True)


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
    if drug.id is not None and references.drug_unconfirmed(drug.id):
        # Named only by registry words, with no code or Drugs@FDA name: a person checks it is the
        # company's own drug, not a rival's product the registry gave plainly (ADR 0035, fifth review).
        return Resolution(drug.candidates, drug.matched_by, needs_review=True)
    if drug.status is not Status.UNKNOWN:
        return drug
    elsewhere = references.resolve_drug(name)
    if elsewhere.candidates:
        return Resolution(elsewhere.candidates, elsewhere.matched_by, needs_review=True)
    return _two_names(references, name, company_id) or drug


def _stated_drug(
    references: ReferenceData,
    claim: _Claim,
    company_id: UUID | None,
    names: Sequence[ListedName],
    named: _CompanyNamed | None,
    report: MatchReport,
) -> Resolution:
    """Match one stated drug by the names given it; name it for the company when no record has them.

    Names of two records, or of another company's drug, stay ambiguous. A name no record has is
    added only for the claim's one company, only when nothing else on file goes by it, and only
    when its own document writes it at the quote (ADR 0060): as a new drug, or as one more name
    of the company's drug that the same words also name. Two of the company's records that its
    document writes as one drug are joined when one of them is spare (`_CompanyNamed.join`).
    """
    found = [known_drug(references, name.name, company_id) for name in names]
    known = [result for result in found if result.status is not Status.UNKNOWN]
    new = [name for name, result in zip(names, found, strict=True) if result.status is Status.UNKNOWN]
    ids = list(dict.fromkeys(result.id for result in known))
    if not new and len(ids) == 1 and ids[0] is not None:
        return known[0]
    held = Resolution()  # as "A (B)" always was: a part no record has leaves the whole unknown
    if len(ids) > 1 or None in ids:
        candidates = tuple(dict.fromkeys(c for r in known for c in r.candidates))
        held = Resolution(candidates, known[0].matched_by, needs_review=True)
    owned = all(i is not None and references.drug_company(i) == company_id for i in ids)
    if named is None or company_id is None or len(ids) > 2 or not owned:
        return held  # another company's drug, a drug with no known company, or more than a pair
    if (len(ids) == 2 and new) or not all(named.free(name.name) for name in new):
        return held
    if not named.written(claim, company_id, [name.name for name in names]):
        return held
    if len(ids) == 2:
        joined = named.join(*(i for i in ids if i is not None))
        return Resolution((joined,), MatchedBy.COMPANY_NAMED) if joined else held
    report.drugs_created += not ids
    return Resolution((named.name(claim, company_id, ids[0] if ids else None, new),), MatchedBy.COMPANY_NAMED)


def _drugs(
    session: Session,
    references: ReferenceData,
    claim: _Claim,
    name: str,
    company: Resolution,
    report: MatchReport,
    named: _CompanyNamed | None,
) -> list[tuple[str, Resolution]]:
    """Match the claim's drug words; create a drug only by the two rules in the module text.

    A code name that belongs to another company is ambiguous for review, never a second drug.
    Words that state several drugs give one match for each, named by that drug's first name, once
    any of them is on file; while none is, the words keep their one unknown match.
    """
    drug = known_drug(references, name, company.id)
    if drug.status is Status.RESOLVED:
        return [(name, drug)]
    if drug.status is Status.UNKNOWN and company.id and _is_new_code_name(name, claim.quote):
        report.drugs_created += 1
        created = _create_drug(session, references, name, company.id)
        return [(name, Resolution((created,), MatchedBy.NEW_CODE_NAME))]
    stated: Stated | None = stated_drugs(name)
    # Ambiguous words are read again only as two names of one drug, whose records may be joined.
    pair = stated is not None and len(stated.drugs) == 1 and len(stated.drugs[0]) > 1
    if stated is None or (drug.status is Status.AMBIGUOUS and not pair):
        return [(name, drug)]
    found = [
        (names[0].name, _stated_drug(references, claim, company.id, names, named, report))
        for names in stated.drugs
    ]
    if len(found) == 1:
        result = found[0][1]
        joined = drug.status is Status.UNKNOWN or result.matched_by is MatchedBy.COMPANY_NAMED
        return [(name, result if result.status is Status.RESOLVED and joined else drug)]
    return [(name, drug)] if all(r.status is Status.UNKNOWN for _, r in found) else found


def _matches(
    session: Session,
    references: ReferenceData,
    claim: _Claim,
    links: list[tuple[UUID, str]],
    report: MatchReport,
    named: _CompanyNamed | None,
) -> Iterator[tuple[MatchedEntity, str, Resolution]]:
    company = _company(links)
    yield MatchedEntity.COMPANY, _COMPANY_LINK, company
    if claim.drug:
        for name, drug in _drugs(session, references, claim, claim.drug, company, report, named):
            yield MatchedEntity.DRUG, name, drug
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
    keep = and_(_MATCH.c.matched_by.in_(_CREATED), _MATCH.c.matched_id == statement.excluded.matched_id)
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
    named: _CompanyNamed | None,
) -> None:
    produced: list[tuple[str, str]] = []
    for entity, name, result in _matches(session, references, claim, links, report, named):
        shown = name[:_NAME_CHARS]
        _store(session, claim.id, entity, shown, result, at)
        produced.append((entity.value, shown))
        report.statuses[f"{entity.value}:{result.status.value}"] += 1
    # Names no longer found (for example after a study name is corrected) lose their old match.
    stale = delete(_MATCH).where(_MATCH.c.claim_id == claim.id)
    if produced:
        stale = stale.where(tuple_(_MATCH.c.entity_type, _MATCH.c.name).not_in(produced))
    session.execute(stale)


def resolve_claims(
    session_factory: sessionmaker[Session], now: Callable[[], datetime], store: SnapshotStore | None = None
) -> MatchReport:
    """Match every extractor claim's names; safe to re-run, and later runs pick up new names.

    Every run matches every claim again, held ones too. With a `store`, a name no record has may
    become a drug named by the company (ADR 0060), which needs the stored copy of its document.
    A claim that fails (for example a malformed name) is logged and skipped; the rest still match.
    """
    report = MatchReport()
    with session_factory.begin() as session:
        references = load_reference_data(session)
        links = _company_links(session)
        at = now()
        claims = _claims(session)
        named = _CompanyNamed(session, store, references, claims) if store is not None else None
        for claim in claims:
            report.claims += 1
            try:
                with session.begin_nested():
                    _resolve_one(
                        session, references, claim, links.get(claim.source_document_id, []), at, report, named
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
