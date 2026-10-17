"""Entity resolver: maps a name to one company, drug, or trial, or says why it cannot (ADR 0015).

Plain code, no model. A match is exact once spelling is set aside: case, punctuation, spacing,
and (for companies) legal-form words. Each rule either finds candidates or passes to the next;
one candidate resolves, two or more are ambiguous and go to review, none is unknown.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID

from catalyst.collectors.coverage import normalize_name
from catalyst.schemas import DrugNameKind


class Status(StrEnum):
    """Whether a name found exactly one record."""

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class MatchedBy(StrEnum):
    """Which rule found the candidates."""

    CIK = "cik"
    TICKER = "ticker"
    NAME = "name"
    CODE_NAME = "code_name"
    INN = "inn"
    BRAND = "brand"
    NCT_ID = "nct_id"
    STUDY_NAME = "study_name"


_KIND_RULE = {
    DrugNameKind.CODE: MatchedBy.CODE_NAME,
    DrugNameKind.INN: MatchedBy.INN,
    DrugNameKind.BRAND: MatchedBy.BRAND,
}


@dataclass(frozen=True)
class Resolution:
    """The candidates a rule found, and that rule; `id` is set only when there is exactly one."""

    candidates: tuple[UUID, ...] = ()
    matched_by: MatchedBy | None = None

    @property
    def status(self) -> Status:
        """Resolved for one candidate, ambiguous for several, unknown for none."""
        if not self.candidates:
            return Status.UNKNOWN
        return Status.RESOLVED if len(self.candidates) == 1 else Status.AMBIGUOUS

    @property
    def id(self) -> UUID | None:
        """The one matching record, if exactly one matched."""
        return self.candidates[0] if self.status is Status.RESOLVED else None


_UNKNOWN = Resolution()


def _found(ids: Iterable[UUID], rule: MatchedBy) -> Resolution:
    unique = tuple(dict.fromkeys(ids))
    return Resolution(unique, rule) if unique else _UNKNOWN


@dataclass(frozen=True)
class CompanyRef:
    """A company as the resolver sees it: SEC number, current and former names, dated tickers."""

    id: UUID
    cik: int
    names: tuple[str, ...]
    tickers: tuple[tuple[str, date, date | None], ...]


@dataclass(frozen=True)
class DrugRef:
    """A drug and every name it goes by; code names count only for the company that owns them."""

    id: UUID
    company_id: UUID | None
    names: tuple[tuple[str, DrugNameKind], ...]


@dataclass(frozen=True)
class TrialRef:
    """A trial: its registry number, the company's name for it, and its sponsor companies."""

    id: UUID
    nct_id: str
    study_name: str | None
    company_ids: frozenset[UUID]


_MARKS = re.compile(r"[®™©℠]")
_NCT = re.compile(r"\bNCT\d{8}\b", re.IGNORECASE)


def name_key(name: str) -> str:
    """Return a drug or study name with case, marks, spacing, and punctuation removed."""
    text = _MARKS.sub("", unicodedata.normalize("NFKC", name)).casefold()
    return "".join(ch for ch in text if ch.isalnum())


def _held_on(period: tuple[str, date, date | None], on: date | None) -> bool:
    _, valid_from, valid_to = period
    if on is None:
        return valid_to is None
    return valid_from <= on and (valid_to is None or on <= valid_to)


class ReferenceData:
    """Indexes of the reference tables, built once and queried many times."""

    def __init__(
        self, companies: Iterable[CompanyRef], drugs: Iterable[DrugRef], trials: Iterable[TrialRef]
    ) -> None:
        """Index every name each record goes by."""
        self._companies = list(companies)
        self._by_cik = {c.cik: c.id for c in self._companies}
        self._by_name: defaultdict[str, list[UUID]] = defaultdict(list)
        for company in self._companies:
            for name in company.names:
                if key := normalize_name(name):
                    self._by_name[key].append(company.id)
        self._drug_names: defaultdict[str, list[tuple[DrugRef, DrugNameKind]]] = defaultdict(list)
        for drug in drugs:
            for name, kind in drug.names:
                if key := name_key(name):
                    self._drug_names[key].append((drug, kind))
        self._trials = list(trials)
        self._by_nct = {t.nct_id: t.id for t in self._trials}

    def resolve_company(
        self,
        *,
        cik: int | None = None,
        ticker: str | None = None,
        name: str | None = None,
        on: date | None = None,
    ) -> Resolution:
        """Match by SEC number, else by the ticker held on `on` (today's if None), else by name.

        A given SEC number decides alone: an unknown one is an unknown company, not a cue to
        try the name.
        """
        if cik is not None:
            found = self._by_cik.get(cik)
            return _found([found] if found else [], MatchedBy.CIK)
        if ticker and (symbol := ticker.strip().lstrip("$").upper()):
            held = [c.id for c in self._companies for t in c.tickers if t[0] == symbol and _held_on(t, on)]
            if held:
                return _found(held, MatchedBy.TICKER)
        key = normalize_name(name or "")
        return _found(self._by_name.get(key, []), MatchedBy.NAME) if key else _UNKNOWN

    def resolve_drug(self, name: str, company_id: UUID | None = None) -> Resolution:
        """Match a drug name; a code name matches only the given company's drugs, if one is given."""
        hits = [
            (drug, kind)
            for drug, kind in self._drug_names.get(name_key(name), [])
            if kind is not DrugNameKind.CODE or company_id is None or drug.company_id == company_id
        ]
        if not hits:
            return _UNKNOWN
        rule = _KIND_RULE[min((kind for _, kind in hits), key=list(DrugNameKind).index)]
        return _found((drug.id for drug, _ in hits), rule)

    def resolve_trial(self, text: str, company_id: UUID | None = None) -> Resolution:
        """Match registry numbers in the text, else a study name within the company's trials."""
        numbers = [n.upper() for n in _NCT.findall(text)]
        if numbers:
            return _found((self._by_nct[n] for n in numbers if n in self._by_nct), MatchedBy.NCT_ID)
        key = name_key(text)
        if not key:
            return _UNKNOWN
        return _found(
            (
                t.id
                for t in self._trials
                if t.study_name
                and name_key(t.study_name) == key
                and (company_id is None or company_id in t.company_ids)
            ),
            MatchedBy.STUDY_NAME,
        )
