"""The planted-error test, part one: clean claims and realistic errors planted in copies (ADR 0018, part 4).

Plain code: no model, no network, the same result every run. The known-correct claims are the
gold set's events, stored as the extractor stores them; trials come from a recorded copy of the
registry. Each planted record copies one clean claim and changes one part of it, as if the error
had slipped past the extractor. `catalyst.verify.planted_report` runs the checks on them.
"""

from __future__ import annotations

import calendar
import json
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from random import Random
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from catalyst.collectors.coverage import normalize_name
from catalyst.extract.agent import check_proposal, proposal_value
from catalyst.extract.evaluate import EVALS, REPOSITORY, GoldDocument, gold_fingerprint, load_gold
from catalyst.extract.text import TEXT_VERSION, QuoteSpan, document_text, find_quote
from catalyst.references import trial_from_study
from catalyst.resolve import CompanyRef, DrugRef, ReferenceData, TrialRef, name_key
from catalyst.schemas import CatalystType, DrugNameKind, TrialPhase
from catalyst.verify.rules import PDUFA_MONTHS, phases_overlap, stated_phases

SEED = 2026
FOLDER = REPOSITORY / "evals" / "planted_errors"
_CODE = re.compile(r"[A-Z]{1,6}[\s\-]?\d{2,6}[A-Z]?")  # a code name such as SPY072 or ABP-450
_SENTENCE = re.compile(r"\S[^\n]*?(?:[.!?](?=\s)|$)", re.MULTILINE)
_STEP = {"day": 1, "month": 1, "quarter": 3, "half": 6, "year": 12}  # months in one window


@dataclass(frozen=True)
class Record:
    """One catalyst claim as stored; `trials` is None when the matcher finds them in the quote."""

    id: UUID
    document: str
    published: date
    company_id: UUID
    value: Mapping[str, Any]
    quote: str
    quote_start: int
    quote_end: int
    trials: tuple[UUID, ...] | None = None


@dataclass(frozen=True)
class Corpus:
    """The clean records and the recorded reference data they are checked against."""

    records: tuple[Record, ...]
    left_out: tuple[Record, ...]
    texts: Mapping[str, str]
    owners: Mapping[str, UUID]
    urls: Mapping[str, str]
    companies: Mapping[UUID, str]
    drugs: tuple[str, ...]
    references: ReferenceData
    phases: Mapping[UUID, TrialPhase | None]
    company_trials: Mapping[UUID, tuple[UUID, ...]]
    trial_names: Mapping[UUID, str]
    fingerprints: Mapping[str, str]


def _owners(gold: Path) -> dict[str, tuple[int, str, str]]:
    """Return each release's company, its SEC number (from the filing's address) and name, and the address."""
    found: dict[str, tuple[int, str, str]] = {}
    for line in gold.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry: dict[str, Any] = json.loads(line)
            cik = re.search(r"/edgar/data/(\d+)/", entry["url"])
            found[entry["sha256"]] = (int(cik.group(1)) if cik else 0, entry["company"], entry["url"])
    return found


def _company_id(cik: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"sec-cik:{cik}")


def _clean(document: GoldDocument, index: int, text: str, company: UUID) -> Record:
    event = document.catalysts[index]
    item = {
        "catalyst_type": event.catalyst_type.value,
        "title": event.drug or event.catalyst_type.value,
        "drug": event.drug,
        "indication": None,
        "date_text": event.date_text,
        "quote": event.quote,
    }
    checked = check_proposal(item, text, document.published)
    if checked.quote is None:
        raise ValueError(f"gold quote not found in {document.sha256}: {event.quote[:60]!r}")
    value = proposal_value(checked) | {"text_version": TEXT_VERSION}
    span = checked.quote
    record_id = uuid5(NAMESPACE_URL, f"gold:{document.sha256}:{index}")
    return Record(
        record_id, document.sha256, document.published, company, MappingProxyType(value), *_span(span)
    )


def _span(span: QuoteSpan) -> tuple[str, int, int]:
    return span.quote, span.start, span.end


def _drug_refs(records: Sequence[Record]) -> list[DrugRef]:
    named = sorted({(str(r.company_id), r.value["drug"]) for r in records if r.value["drug"]})
    kinds = {True: DrugNameKind.CODE, False: DrugNameKind.INN}
    return [
        DrugRef(uuid5(NAMESPACE_URL, f"drug:{c}:{d}"), UUID(c), ((d, kinds[bool(_CODE.fullmatch(d))]),))
        for c, d in named
    ]


def _registry(path: Path, companies: Mapping[UUID, str]) -> list[tuple[TrialRef, TrialPhase | None]]:
    """Read the recorded registry; a trial belongs to a company when it is the lead sponsor."""
    by_name = {normalize_name(name): company for company, name in companies.items()}
    trials: dict[str, tuple[TrialRef, TrialPhase | None]] = {}
    for response in json.loads(path.read_text(encoding="utf-8"))["responses"].values():
        for study in response["studies"]:
            sponsor = study["protocolSection"]["sponsorCollaboratorsModule"]["leadSponsor"]["name"]
            owner = by_name.get(normalize_name(sponsor))
            if owner is not None:
                trial = trial_from_study(study)
                ref = TrialRef(
                    uuid5(NAMESPACE_URL, trial.nct_id), trial.nct_id, trial.study_name, frozenset({owner})
                )
                trials[trial.nct_id] = (ref, trial.phase)
    return [trials[nct] for nct in sorted(trials)]


def load_corpus(
    gold: Path = EVALS / "gold.jsonl",
    copies: Path = EVALS / "documents",
    registry: Path = FOLDER / "registry.json",
) -> Corpus:
    """Turn the gold set into clean claims and build the reference tables they are matched against."""
    listed = _owners(gold)
    owners = {sha: (_company_id(cik), cik, name) for sha, (cik, name, _) in listed.items()}
    documents = load_gold(gold)
    texts = {
        d.sha256: document_text((copies / f"{d.sha256}.htm").read_bytes(), d.content_type) for d in documents
    }
    claims = [
        _clean(d, i, texts[d.sha256], owners[d.sha256][0]) for d in documents for i in range(len(d.catalysts))
    ]
    # A gold event whose claim the extractor's own checks already flag goes to review as it is;
    # planting an error in it would prove nothing, so it is left out and named in the report.
    records = tuple(r for r in claims if not r.value["problems"])
    left_out = tuple(r for r in claims if r.value["problems"])
    companies = {company: name for company, _, name in owners.values()}
    trials = _registry(registry, companies)
    company_trials: defaultdict[UUID, list[UUID]] = defaultdict(list)
    for trial, _ in trials:
        for company in trial.company_ids:
            company_trials[company].append(trial.id)
    references = ReferenceData(
        [CompanyRef(c, cik, (name,), ()) for c, cik, name in sorted(set(owners.values()))],
        _drug_refs(records),
        [trial for trial, _ in trials],
    )
    drugs = {name_key(r.value["drug"]): r.value["drug"] for r in records if r.value["drug"]}
    return Corpus(
        records=records,
        left_out=left_out,
        texts=texts,
        owners={sha: company for sha, (company, _, _) in owners.items()},
        urls={sha: url for sha, (_, _, url) in listed.items()},
        companies=companies,
        drugs=tuple(drugs[key] for key in sorted(drugs)),
        references=references,
        phases={trial.id: phase for trial, phase in trials},
        company_trials={c: tuple(ids) for c, ids in company_trials.items()},
        trial_names={
            t.id: f"{t.nct_id}{f' ({t.study_name})' if t.study_name else ''}, {_phase(p)}" for t, p in trials
        },
        fingerprints={"gold_sha256": gold_fingerprint(gold), "registry_sha256": gold_fingerprint(registry)},
    )


def _phase(phase: TrialPhase | None) -> str:
    return phase.value.replace("phase_", "Phase ").replace("_", "/") if phase else "no phase on file"


def trial_links(record: Record, corpus: Corpus) -> tuple[UUID, ...]:
    """Return the trials a record is linked to: planted ones, or those the matcher finds in its quote."""
    if record.trials is not None:
        return record.trials
    found = corpus.references.trials_mentioned(record.quote, record.company_id)
    return tuple(r.id for _, r in found if r.id is not None)


# --- Planting errors -------------------------------------------------------------------------


def _with(record: Record, **changes: Any) -> Record:
    return replace(record, value=MappingProxyType({**record.value, **changes}))


def _move(day: date, months: int) -> date:
    """Move a day by whole months; a month's last day stays the last day (Sep 30 + 3 is Dec 31)."""
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    last = calendar.monthrange(year, month)[1]
    at_end = day.day == calendar.monthrange(day.year, day.month)[1]
    return date(year, month, last if at_end else min(day.day, last))


def _window(record: Record) -> tuple[date, date]:
    return date.fromisoformat(record.value["window_start"]), date.fromisoformat(record.value["window_end"])


def _shifted(record: Record, months: int = 0, days: int = 0) -> Record:
    start, end = (_move(day, months) + timedelta(days=days) for day in _window(record))
    return _with(record, window_start=start.isoformat(), window_end=end.isoformat())


def _wrong_window(record: Record, _corpus: Corpus, rng: Random) -> Record:
    """Move the window a period or a year later than its date words; move a day 1 to 30 days."""
    step = _STEP[record.value["window_precision"]]
    if record.value["window_precision"] == "day":
        return _shifted(record, days=rng.randint(1, 30))
    return _shifted(record, months=rng.choice(sorted({step, 2 * step, 12})))


def _past_window(record: Record, _corpus: Corpus, _rng: Random) -> Record:
    """Move the window a year (or more) earlier, so it ended before the release came out."""
    months = -12
    while _window(_shifted(record, months))[1] >= record.published:
        months -= 12
    return _shifted(record, months)


def _pdufa_outside(record: Record, _corpus: Corpus, _rng: Random) -> Record | None:
    """Move an FDA decision date a year or more later, beyond 15 months after the release."""
    if record.value["catalyst_type"] != CatalystType.PDUFA_DATE.value:
        return None
    latest, months = _move(record.published, PDUFA_MONTHS), 12
    while _window(_shifted(record, months))[0] <= latest:
        months += 12
    return _shifted(record, months)


def _wrong_drug(record: Record, corpus: Corpus, rng: Random) -> Record:
    current = name_key(record.value["drug"] or "")
    return _with(record, drug=rng.choice([d for d in corpus.drugs if name_key(d) != current]))


def _wrong_type(record: Record, _corpus: Corpus, rng: Random) -> Record:
    others = [t.value for t in CatalystType if t.value != record.value["catalyst_type"]]
    return _with(record, catalyst_type=rng.choice(others))


def _wrong_company(record: Record, corpus: Corpus, rng: Random) -> Record:
    others = sorted((n, c) for c, n in corpus.companies.items() if c != record.company_id)
    return replace(record, company_id=rng.choice(others)[1])


def _wrong_trial(record: Record, corpus: Corpus, rng: Random) -> Record | None:
    linked = trial_links(record, corpus)
    others = [t for t in corpus.company_trials.get(record.company_id, ()) if t not in linked]
    return replace(record, trials=(rng.choice(others),)) if others else None


def _wrong_phase(record: Record, corpus: Corpus, rng: Random) -> Record | None:
    stated = stated_phases(record.quote)
    unlike = [
        t
        for t in corpus.company_trials.get(record.company_id, ())
        if (p := corpus.phases[t]) not in (None, TrialPhase.NOT_APPLICABLE)
        and not any(phases_overlap(said, p) for said in stated)
    ]
    return replace(record, trials=(rng.choice(unlike),)) if stated and unlike else None


def _quote_swap(record: Record, corpus: Corpus, rng: Random) -> Record | None:
    """Swap the quote for another sentence of the release (40 characters or more, apart from it)."""
    text = corpus.texts[record.document]
    spans = [
        span
        for match in _SENTENCE.finditer(text)
        if len(match.group(0)) >= 40 and (span := find_quote(text, match.group(0))) is not None
        if span.end <= record.quote_start or span.start >= record.quote_end
    ]
    if not spans:
        return None
    quote, start, end = _span(rng.choice(spans))
    return replace(record, quote=quote, quote_start=start, quote_end=end)


@dataclass(frozen=True)
class ErrorType:
    """One kind of planted error: the record parts it changes, and why today's checks can miss it."""

    name: str
    label: str
    changes: frozenset[str]
    mutate: Callable[[Record, Corpus, Random], Record | None]
    blind_spot: str


def _error(name: str, label: str, changes: str, mutate: Any, blind_spot: str) -> ErrorType:
    return ErrorType(name, label, frozenset({changes}), mutate, blind_spot)


ERRORS: tuple[ErrorType, ...] = (
    _error("wrong_window", "Wrong quarter, half, month, or year", "window", _wrong_window,
           "the stored window still matches the date words."),
    _error("past_window", "Date moved into the past", "window", _past_window,
           "the stored window still matches the date words."),
    _error("pdufa_outside_review", "FDA decision date outside its review window", "window", _pdufa_outside,
           "the date still falls within 15 months of the release."),
    _error("wrong_drug", "Wrong drug", "drug", _wrong_drug,
           "the second reading names the same drug."),
    _error("wrong_type", "Wrong event type", "type", _wrong_type,
           "the second reading gives the same type."),
    _error("wrong_company", "Wrong company", "company", _wrong_company,
           "no check compares the linked company with the company that put out the release, and the "
           "second reading does not read the company. Only a company's own code name (such as SPY072) "
           "gives the error away; a generic or brand name matches under any company."),
    _error("wrong_trial", "Wrong trial (another of the company's trials)", "trials", _wrong_trial,
           "only the phase check reads the linked trial, so the error shows only when the quote states "
           "a phase that the other trial does not have. The second reading does not compare trials "
           "(ADR 0018, part 2); part 3's cross-source checks should close this gap."),
    _error("wrong_phase", "Wrong phase (registry phase contradicts the quote)", "trials", _wrong_phase,
           "the phase check found no contradiction."),
    _error("quote_swap", "Quote that does not support the value", "quote", _quote_swap,
           "the other sentence also holds the date words, and the second reading pairs two readings in "
           "different sentences when their type, window, and drug agree."),
)  # fmt: skip


def plant(error: ErrorType, record: Record, corpus: Corpus, seed: int = SEED) -> Record | None:
    """Return a copy of `record` with one error of this type, or None if this record cannot have it."""
    planted = error.mutate(record, corpus, Random(f"{seed}:{error.name}:{record.id}"))  # noqa: S311 (a fixed seed, not a secret)
    return replace(planted, id=uuid5(record.id, error.name)) if planted else None


def parts(record: Record, corpus: Corpus) -> dict[str, object]:
    """Split a stored record into the parts a mutation may change, so a test can see which changed."""
    named = ("window_start", "window_end", "window_precision", "drug", "catalyst_type")
    return {
        "window": tuple(record.value[k] for k in named[:3]),
        "drug": record.value["drug"],
        "type": record.value["catalyst_type"],
        "company": record.company_id,
        "trials": record.trials,
        "quote": (record.quote, record.quote_start, record.quote_end),
        "rest": {k: v for k, v in record.value.items() if k not in named},
        "source": (record.document, record.published, corpus.owners[record.document]),
    }
