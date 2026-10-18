"""Entity resolver: maps a name to one company, drug, or trial, or says why it cannot (ADR 0015).

Plain code, no model. A match is exact once spelling is set aside: case, punctuation, spacing,
and (for companies) legal-form words. Each rule either finds candidates or passes to the next;
one candidate resolves, two or more are ambiguous and go to review, none is unknown.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID

from catalyst.collectors.coverage import normalize_name
from catalyst.schemas import DrugNameKind, MatchStatus

Status = MatchStatus  # resolved, ambiguous, or unknown; stored as EntityMatch.status


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
    DOCUMENT_LINK = "document_link"  # a company linked to the document by registry or FDA sponsor
    NEW_CODE_NAME = "new_code_name"  # the company's own code name, first seen in its release


_KIND_RULE = {
    DrugNameKind.CODE: MatchedBy.CODE_NAME,
    DrugNameKind.INN: MatchedBy.INN,
    DrugNameKind.BRAND: MatchedBy.BRAND,
}


@dataclass(frozen=True)
class Resolution:
    """The candidates a rule found, and that rule; `id` is set only when there is exactly one.

    `needs_review` marks a result that must not resolve even with one candidate: a text that
    names two registry numbers when only one is on file, or a code name that belongs to another
    company.
    """

    candidates: tuple[UUID, ...] = ()
    matched_by: MatchedBy | None = None
    needs_review: bool = False

    @property
    def status(self) -> Status:
        """Resolved for one candidate, ambiguous for several (or several named), unknown for none."""
        if self.needs_review or len(self.candidates) > 1:
            return Status.AMBIGUOUS
        return Status.RESOLVED if self.candidates else Status.UNKNOWN

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


def _letters(text: str) -> str:
    return "".join(ch for ch in text if ch.isalpha())


_DASHES = r"\-\u2010-\u2015"


def whole_word_pattern(study_name: str) -> re.Pattern[str]:
    """Match a name as a whole word, allowing any spacing or dash between its parts.

    A name joined by a dash to more letters or digits is part of a longer name, so "IMAGINE" is
    not found in "IMAGINE-II" or "PRE-IMAGINE" (Mirum has trials named IMAGINE and IMAGINE-II).
    """
    parts = re.findall(r"[^\W_]+", study_name)
    if not parts:
        return re.compile(r"(?!)")
    body = rf"[\s{_DASHES}]*".join(re.escape(part) for part in parts)
    before, after = rf"(?<![^\W_])(?<![^\W_][{_DASHES}])", rf"(?![{_DASHES}]?[^\W_])"
    return re.compile(before + body + after, re.IGNORECASE)


def _written(pattern: re.Pattern[str], study_name: str, text: str) -> Iterator[str]:
    """Yield each place the text writes the study name, with the registry's capital letters."""
    for match in pattern.finditer(text):
        if _letters(match.group(0)) == _letters(study_name):
            yield match.group(0)


def names_trial(text: str, nct_id: str, study_name: str | None) -> bool:
    """Say whether the text names this trial the way `trials_mentioned` finds trials.

    That is by its registry number, or by its study name as a whole word written with the
    registry's capital letters. The rule checks use this, so they and the resolver agree.
    """
    if nct_id.upper() in {number.upper() for number in _NCT.findall(text)}:
        return True
    return study_name is not None and any(_written(whole_word_pattern(study_name), study_name, text))


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
        self._by_cik: defaultdict[int, list[UUID]] = defaultdict(list)
        for company in self._companies:
            self._by_cik[company.cik].append(company.id)
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
        self._study_patterns = {t.id: whole_word_pattern(t.study_name) for t in self._trials if t.study_name}
        self._by_nct: defaultdict[str, list[UUID]] = defaultdict(list)
        for trial in self._trials:
            self._by_nct[trial.nct_id.upper()].append(trial.id)

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
            return _found(self._by_cik.get(cik, []), MatchedBy.CIK)
        if ticker and (symbol := ticker.strip().lstrip("$").upper()):
            held = [
                c.id for c in self._companies for t in c.tickers if t[0].upper() == symbol and _held_on(t, on)
            ]
            if held:
                return _found(held, MatchedBy.TICKER)
        key = normalize_name(name or "")
        return _found(self._by_name.get(key, []), MatchedBy.NAME) if key else _UNKNOWN

    def resolve_drug(self, name: str, company_id: UUID | None = None) -> Resolution:
        """Match a drug name; a code name matches only the given company's drugs, if one is given.

        When the candidates were found through different kinds of name, `matched_by` names the
        first kind in DrugNameKind order (code, generic, brand).
        """
        hits = [
            (drug, kind)
            for drug, kind in self._drug_names.get(name_key(name), [])
            if kind is not DrugNameKind.CODE or company_id is None or drug.company_id == company_id
        ]
        if not hits:
            return _UNKNOWN
        rule = _KIND_RULE[min((kind for _, kind in hits), key=list(DrugNameKind).index)]
        return _found((drug.id for drug, _ in hits), rule)

    def add_drug(self, drug: DrugRef) -> None:
        """Index a drug created after loading, so later lookups in the same run find it."""
        for name, kind in drug.names:
            if key := name_key(name):
                self._drug_names[key].append((drug, kind))

    def trials_mentioned(self, text: str, company_id: UUID | None = None) -> list[tuple[str, Resolution]]:
        """Find every trial a longer text names: registry numbers, and study names in capitals.

        A study name counts only within the given company's trials (none when no company is
        given), and only as a whole word written with the same capital letters as the registry
        has it, so "ARISE" is a trial and "questions may arise" is not. Each name found is reported
        once, with its own resolution: two registry numbers in one text are two names here, while
        `resolve_trial` treats the same text as one ambiguous mention.
        """
        found: list[tuple[str, Resolution]] = []
        for number in dict.fromkeys(n.upper() for n in _NCT.findall(text)):
            found.append((number, _found(self._by_nct.get(number, []), MatchedBy.NCT_ID)))
        by_key: dict[str, tuple[str, list[UUID]]] = {}
        for trial in self._trials:
            if not trial.study_name or company_id is None or company_id not in trial.company_ids:
                continue
            for written in _written(self._study_patterns[trial.id], trial.study_name, text):
                by_key.setdefault(name_key(written), (written, []))[1].append(trial.id)
        found += [(written, _found(ids, MatchedBy.STUDY_NAME)) for written, ids in by_key.values()]
        return found

    def resolve_trial(self, text: str, company_id: UUID | None = None) -> Resolution:
        """Match registry numbers in the text, else a study name within the company's trials.

        A text naming two different registry numbers is ambiguous, even if only one is on file.
        """
        numbers = set(n.upper() for n in _NCT.findall(text))
        if numbers:
            found = _found((i for n in sorted(numbers) for i in self._by_nct.get(n, [])), MatchedBy.NCT_ID)
            if len(numbers) == 1:
                return found
            return Resolution(found.candidates, MatchedBy.NCT_ID, needs_review=True)
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
