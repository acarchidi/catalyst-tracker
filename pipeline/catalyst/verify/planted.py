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
import tomllib
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
from catalyst.extract.agent import check_proposal, has_words, proposal_value
from catalyst.extract.dates import parse_window
from catalyst.extract.evaluate import EVALS, REPOSITORY, GoldDocument, gold_fingerprint, load_gold
from catalyst.extract.text import TEXT_VERSION, QuoteSpan, document_text, find_quote
from catalyst.references import trial_from_study
from catalyst.resolve import CompanyRef, DrugRef, ReferenceData, TrialRef, name_key
from catalyst.schemas import CatalystType, DrugNameKind, TrialPhase
from catalyst.verify.rules import PDUFA_MONTHS, TrialNames, phases_overlap, stated_phases

SEED = 2026
FOLDER = REPOSITORY / "evals" / "planted_errors"
COVERAGE = REPOSITORY / "config" / "coverage_companies.toml"
_LISTED_SINCE = date(2000, 1, 1)  # the coverage list holds current tickers only
_CODE = re.compile(r"[A-Z]{1,6}[\s\-]?\d{2,6}[A-Z]?")  # a code name such as SPY072 or ABP-450
_SENTENCE = re.compile(r"\S[^\n]*?(?:[.!?](?=\s)|$)", re.MULTILINE)
_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?"
    r"|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_YEAR = r"20\d{2}"
_DATE_PHRASE = re.compile(  # the date phrases a release writes; a bare year last
    rf"\b(?:{_MONTH}\s+\d{{1,2}},\s+{_YEAR}|{_MONTH}\s+{_YEAR}"
    rf"|(?:first|second|third|fourth)\s+quarter\s+(?:of\s+)?{_YEAR}|Q[1-4]\s+{_YEAR}"
    rf"|(?:first|second)\s+half\s+(?:of\s+)?{_YEAR}|H[12]\s+{_YEAR}|[12]H\s?{_YEAR}"
    rf"|(?:year-end|mid-|late|early)\s?{_YEAR}|{_YEAR})\b",
    re.IGNORECASE,
)
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
    drug_refs: tuple[DrugRef, ...]  # the drugs in `references`, with their companies
    # The drug records production holds: `resolve_claims` stores only a company's new code names,
    # so brand and generic names (LIVMARLI, azer-cel) are not on file (Opus review of PR #61).
    drug_records: tuple[DrugRef, ...]
    phases: Mapping[UUID, TrialPhase | None]
    company_trials: Mapping[UUID, tuple[UUID, ...]]
    trial_names: Mapping[UUID, str]
    registry: Mapping[UUID, TrialNames]  # each trial's registry number and study name
    company_refs: tuple[CompanyRef, ...]  # the companies on file, with their tickers
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
    coverage: Path = COVERAGE,
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
    tickers = _tickers(coverage)
    company_refs = tuple(
        CompanyRef(c, cik, (name,), ((tickers[cik], _LISTED_SINCE, None),) if cik in tickers else ())
        for c, cik, name in sorted(set(owners.values()))
    )
    drug_refs = tuple(_drug_refs(records))
    references = ReferenceData(company_refs, drug_refs, [trial for trial, _ in trials])
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
        drug_refs=drug_refs,
        drug_records=tuple(d for d in drug_refs if all(k is DrugNameKind.CODE for _, k in d.names)),
        phases={trial.id: phase for trial, phase in trials},
        company_trials={c: tuple(ids) for c, ids in company_trials.items()},
        trial_names={
            t.id: f"{t.nct_id}{f' ({t.study_name})' if t.study_name else ''}, {_phase(p)}" for t, p in trials
        },
        registry={t.id: TrialNames(t.nct_id, t.study_name) for t, _ in trials},
        company_refs=company_refs,
        fingerprints={"gold_sha256": gold_fingerprint(gold), "registry_sha256": gold_fingerprint(registry)},
    )


def _tickers(coverage: Path) -> dict[int, str]:
    """Return each covered company's ticker by its SEC number, from the coverage list (ADR 0009)."""
    listed: list[dict[str, Any]] = tomllib.loads(coverage.read_text(encoding="utf-8"))["company"]
    return {int(c["cik"]): str(c["ticker"]) for c in listed if c.get("ticker")}


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


def _wrong_window(record: Record, _corpus: Corpus, _rng: Random) -> list[Record]:
    """Move the window 1 or 2 periods or a year later than its date words; a day 1, 7, or 30 days."""
    if record.value["window_precision"] == "day":
        return [_shifted(record, days=days) for days in (1, 7, 30)]
    step = _STEP[record.value["window_precision"]]
    return [_shifted(record, months=months) for months in sorted({step, 2 * step, 12})]


def _past_window(record: Record, _corpus: Corpus, _rng: Random) -> list[Record]:
    """Move the window a year (or more) earlier, so it ended before the release came out."""
    months = -12
    while _window(_shifted(record, months))[1] >= record.published:
        months -= 12
    return [_shifted(record, months)]


def _pdufa_outside(record: Record, _corpus: Corpus, _rng: Random) -> list[Record]:
    """Move an FDA decision date a year or more later, beyond 15 months after the release."""
    if record.value["catalyst_type"] != CatalystType.PDUFA_DATE.value:
        return []
    latest, months = _move(record.published, PDUFA_MONTHS), 12
    while _window(_shifted(record, months))[0] <= latest:
        months += 12
    return [_shifted(record, months)]


def date_phrases(text: str) -> list[str]:
    """Return the date phrases written in a text, in order: "Q4 2026", "September 26, 2026", "mid-2027"."""
    return [m.group(0) for m in _DATE_PHRASE.finditer(text)]


def _consistent_date(record: Record, _corpus: Corpus, _rng: Random) -> list[Record]:
    """Take another date phrase of the same quote, and store the window it gives, as the extractor would."""
    planted: dict[tuple[str, str, str], Record] = {}
    for words in date_phrases(record.quote):
        window = parse_window(words, record.published)
        if window is None:
            continue
        key = (window.start.isoformat(), window.end.isoformat(), window.precision.value)
        if key != tuple(record.value[k] for k in ("window_start", "window_end", "window_precision")):
            planted.setdefault(
                key,
                _with(
                    record, date_text=words, window_start=key[0], window_end=key[1], window_precision=key[2]
                ),
            )
    return list(planted.values())


def _drugs(corpus: Corpus, same_release: Record, *, inside: bool) -> list[str]:
    """Return the gold set's drug names inside (or outside) the record's release, one per name."""
    names = {
        name_key(r.value["drug"]): r.value["drug"]
        for r in corpus.records
        if r.value["drug"] and (r.document == same_release.document) == inside
    }
    names.pop(name_key(same_release.value["drug"] or ""), None)
    return [names[key] for key in sorted(names)]


def _drug_in_release(record: Record, corpus: Corpus, _rng: Random) -> list[Record]:
    return [_with(record, drug=drug) for drug in _drugs(corpus, record, inside=True)]


def _drug_elsewhere(record: Record, corpus: Corpus, _rng: Random) -> list[Record]:
    return [_with(record, drug=drug) for drug in _drugs(corpus, record, inside=False)]


def _wrong_type(record: Record, _corpus: Corpus, _rng: Random) -> list[Record]:
    return [
        _with(record, catalyst_type=t.value) for t in CatalystType if t.value != record.value["catalyst_type"]
    ]


def _wrong_company(record: Record, corpus: Corpus, _rng: Random) -> list[Record]:
    others = sorted((n, c) for c, n in corpus.companies.items() if c != record.company_id)
    return [replace(record, company_id=company) for _, company in others]


def _wrong_trial(record: Record, corpus: Corpus, _rng: Random) -> list[Record]:
    linked = trial_links(record, corpus)
    others = [t for t in corpus.company_trials.get(record.company_id, ()) if t not in linked]
    return [replace(record, trials=(trial,)) for trial in others]


def _wrong_phase(record: Record, corpus: Corpus, _rng: Random) -> list[Record]:
    stated = stated_phases(record.quote)
    unlike = [
        t
        for t in corpus.company_trials.get(record.company_id, ())
        if (p := corpus.phases[t]) not in (None, TrialPhase.NOT_APPLICABLE)
        and not any(phases_overlap(said, p) for said in stated)
    ]
    return [replace(record, trials=(trial,)) for trial in unlike] if stated else []


def _sentences(record: Record, corpus: Corpus) -> list[QuoteSpan]:
    """Return every other sentence of the release, 40 characters or more, apart from the true quote."""
    text = corpus.texts[record.document]
    spans = {
        (span.start, span.end): span
        for match in _SENTENCE.finditer(text)
        if len(match.group(0)) >= 40 and (span := find_quote(text, match.group(0))) is not None
        if span.end <= record.quote_start or span.start >= record.quote_end
    }
    return [spans[key] for key in sorted(spans)]


def _quoted(record: Record, span: QuoteSpan) -> Record:
    quote, start, end = _span(span)
    return replace(record, quote=quote, quote_start=start, quote_end=end)


def _quote_swap(record: Record, corpus: Corpus, rng: Random) -> list[Record]:
    """Swap the quote for one sentence of the release, picked at random with the seed."""
    spans = _sentences(record, corpus)
    return [_quoted(record, rng.choice(spans))] if spans else []


def _quote_same_date(record: Record, corpus: Corpus, _rng: Random) -> list[Record]:
    """Swap the quote for a sentence of another gold event of the release that holds the same date words.

    A sentence that names the claim's own drug may restate the same event, so it is not used.
    """
    words, drug = record.value["date_text"], name_key(record.value["drug"] or "")
    events = [
        (r.quote_start, r.quote_end)
        for r in (*corpus.records, *corpus.left_out)
        if r.document == record.document and r.id != record.id
    ]
    return [
        _quoted(record, span)
        for span in _sentences(record, corpus)
        if has_words(span.quote, words)
        and not (drug and drug in name_key(span.quote))
        and any(span.start < end and start < span.end for start, end in events)
    ]


@dataclass(frozen=True)
class ErrorType:
    """One kind of planted error: the record parts it changes, and why today's checks can miss it."""

    name: str
    label: str
    changes: frozenset[str]
    mutate: Callable[[Record, Corpus, Random], list[Record]]
    blind_spot: str
    note: str = ""


def _error(name: str, label: str, changes: str, mutate: Any, blind_spot: str, note: str = "") -> ErrorType:
    return ErrorType(name, label, frozenset(changes.split()), mutate, blind_spot, note)


_NO_COMPANY_CHECK = (
    "the release also names the other company by a name or listed ticker of its own (a partner, say), "
    "so `company_named_in_source` passes, and the second reading does not read the company."
)
ERRORS: tuple[ErrorType, ...] = (
    _error("wrong_window", "Wrong quarter, half, month, or year (date words left as they were)", "window",
           _wrong_window, "the stored window still matches the date words.",
           "Only the stored window changes, so it no longer matches its own date words; a rule catches "
           "that by construction. The next row is the harder case."),
    _error("consistent_wrong_date", "Another date from the same quote, stored consistently",
           "window date_words", _consistent_date,
           "the new date words are in the quote and give exactly the stored window, so every rule passes; "
           "only the second reading can tell which date belongs to the event.",
           "Only the second reading catches these (a rule only when the other date has passed), and in "
           "this test it never misreads."),
    _error("past_window", "Date moved into the past", "window", _past_window,
           "the stored window still matches the date words."),
    _error("pdufa_outside_review", "FDA decision date outside its review window", "window", _pdufa_outside,
           "the date still falls within 15 months of the release."),
    _error("drug_in_release", "Wrong drug (another in the same release)", "drug", _drug_in_release,
           "the quote names both drugs, or its true drug is a brand or generic name that no other claim "
           "of the release names, so the drug check passes, and the other drug is the same company's, so "
           "it matches; only the second reading compares drugs.",
           "The drug check catches most of these. It misses them when the quote names both drugs (one "
           "Spyre sentence names SPY072 alone and with IL-17A/F), or when the true drug is a brand or "
           "generic name that no other claim of the release names: production keeps records only of code "
           "names, and this test checks with those alone. Then only the second reading catches them, and "
           "in this test it never misreads."),
    _error("drug_elsewhere", "Wrong drug (another company's drug)", "drug", _drug_elsewhere,
           "the drug name is a generic or brand name, which matches under any company."),
    _error("wrong_type", "Wrong event type", "type", _wrong_type,
           "the quote also holds words for the planted type: data to be presented (a readout or a "
           "presentation), enrollment completed beside topline data, or an NDA accepted with its PDUFA "
           "date (an acceptance), so the type check (`type_named_near_quote`) passes; only the second "
           "reading compares types.",
           "The type check catches most of these without the second reading: the quote holds no words "
           "for the planted type. It misses them when the quote holds words for both types."),
    _error("wrong_company", "Wrong company", "company", _wrong_company, _NO_COMPANY_CHECK),
    _error("wrong_trial", "Wrong trial (another of the company's trials)", "trials", _wrong_trial,
           "the quote also names the other trial, so `trial_named_in_quote` passes, and the phase check "
           "finds no contradiction. The second reading does not compare trials (ADR 0018, part 2)."),
    _error("wrong_phase", "Wrong phase (registry phase contradicts the quote)", "trials", _wrong_phase,
           "the phase check found no contradiction.",
           "Built to be exactly what the phase check looks for, so this row shows that the check works, "
           "not how often such errors are caught."),
    _error("quote_swap", "Quote from an unrelated sentence", "quote", _quote_swap,
           "the other sentence also holds the date words."),
    _error("quote_same_date", "Quote from another event with the same date words", "quote", _quote_same_date,
           "the other sentence is about the same drug: SPY072's other sub-study, whose sentence comes just "
           "after the drug's name and holds the same date words. So the date and drug checks pass, and the "
           "second reading pairs two readings in different sentences when their type, window, and drug "
           "agree. The stored values (SPY072, topline data, Q4 2026) are true of that sentence too.",
           "The drug check (`drug_named_near_quote`) catches every copy whose sentence is about another "
           "drug."),
)  # fmt: skip


def plant(error: ErrorType, record: Record, corpus: Corpus, seed: int = SEED) -> tuple[Record, ...]:
    """Return every copy of `record` with one error of this type; none if this record cannot have it."""
    rng = Random(f"{seed}:{error.name}:{record.id}")  # noqa: S311 (a fixed seed, not a secret)
    return tuple(
        replace(copy, id=uuid5(record.id, f"{error.name}:{n}"))
        for n, copy in enumerate(error.mutate(record, corpus, rng))
    )


def parts(record: Record, corpus: Corpus) -> dict[str, object]:
    """Split a stored record into the parts a mutation may change, so a test can see which changed."""
    named = ("window_start", "window_end", "window_precision", "drug", "catalyst_type", "date_text")
    return {
        "window": tuple(record.value[k] for k in named[:3]),
        "date_words": record.value["date_text"],
        "drug": record.value["drug"],
        "type": record.value["catalyst_type"],
        "company": record.company_id,
        "trials": record.trials,
        "quote": (record.quote, record.quote_start, record.quote_end),
        "rest": {k: v for k, v in record.value.items() if k not in named},
        "source": (record.document, record.published, corpus.owners[record.document]),
    }
