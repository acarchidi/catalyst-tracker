"""Run every check on clean and planted claims, and report the result (ADR 0018, part 4).

Each planted record is checked among the clean claims of its own release, as of the day the
release came out. The second reading is simulated as a reader that gets every event right, so a
planted value disagrees with it, and its paired reading is the correction.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID, uuid5

from catalyst.resolve import Status
from catalyst.resolve_claims import known_drug
from catalyst.schemas import SourceTier
from catalyst.verify.advisory import AdvisoryClaim, advisory_notice
from catalyst.verify.company import company_named, known_companies
from catalyst.verify.fedreg_notice import NoticesInWindow, StoredNotices, notices_in
from catalyst.verify.independent import Statement, address, independent_sources
from catalyst.verify.planted import ERRORS, SEED, Corpus, Record, parts, plant, trial_links
from catalyst.verify.rules import CheckResult, ClaimFacts, Match, Outcome, rule_checks
from catalyst.verify.second import CHECK as SECOND_READING
from catalyst.verify.second import Reading, compare, pairs
from catalyst.verify.second_run import same_drug_rule


def _matches(record: Record, corpus: Corpus) -> tuple[Match, ...]:
    """Match the record's names as the matching job does (`catalyst.resolve_claims`)."""
    found = [Match("company", Status.RESOLVED.value, record.company_id)]
    drug = record.value["drug"]
    if drug:
        resolution = known_drug(corpus.references, drug, record.company_id)
        found.append(Match("drug", resolution.status.value, resolution.id))
    if record.trials is None:
        mentioned = corpus.references.trials_mentioned(record.quote, record.company_id)
        found += [Match("trial", r.status.value, r.id) for _, r in mentioned]
    else:
        found += [Match("trial", Status.RESOLVED.value, t) for t in record.trials]
    return tuple(found)


Check = Callable[[Sequence[Record], Corpus], Mapping[UUID, Sequence[CheckResult]]]


def _rule_checks(records: Sequence[Record], corpus: Corpus) -> dict[UUID, list[CheckResult]]:
    """Run the rule checks (`catalyst.verify.rules`) as of the day the release came out."""
    return {
        r.id: rule_checks(
            ClaimFacts(
                r.id,
                r.value,
                r.quote,
                r.published,
                _matches(r, corpus),
                corpus.phases,
                trial_names=corpus.registry,
            ),
            r.published,
        )
        for r in records
    }


def _reading(record: Record, claim_id: UUID | None = None) -> Reading:
    window = tuple(record.value[k] for k in ("window_start", "window_end", "window_precision"))
    return Reading(
        claim_id or record.id,
        record.value["catalyst_type"],
        record.value["drug"],
        (window[0], window[1], window[2]),
        record.quote_start,
        record.quote_end,
    )


def _second_readings(document: str, corpus: Corpus) -> list[Reading]:
    """Return the simulated second reading: every gold event of the release, read correctly."""
    return [_reading(r, uuid5(r.id, "second reading")) for r in corpus.records if r.document == document]


def _second_reading(records: Sequence[Record], corpus: Corpus) -> dict[UUID, list[CheckResult]]:
    """Compare each record with the second reading (`catalyst.verify.second.compare`)."""
    document = records[0].document
    same = same_drug_rule(corpus.references, corpus.owners[document])
    found = compare([_reading(r) for r in records], _second_readings(document, corpus), same)
    return {claim: [result] for claim, result in found.items()}


def _tier(url: str) -> SourceTier:
    """Return a gold release's source tier as its collector sets it: the gold set is SEC exhibits."""
    if address(url).startswith("sec.gov/"):
        return SourceTier.REGULATOR_OR_SEC
    raise ValueError(f"no source tier known for {url}; add it here as its collector sets it")


def statement_of(record: Record, corpus: Corpus, clean: bool) -> Statement:
    """State a record as `catalyst.verify.run` does; a copy shares its original's document, so its source."""
    v = record.value
    window = (
        (date.fromisoformat(v["window_start"]), date.fromisoformat(v["window_end"]))
        if v["window_end"]
        else None
    )
    drugs = frozenset(m.matched_id for m in _matches(record, corpus) if m.entity == "drug" and m.matched_id)
    return Statement(
        claim_id=record.id,
        url=corpus.urls[record.document],
        sha256=record.document,
        quote=record.quote,
        tier=_tier(corpus.urls[record.document]),
        companies=frozenset({record.company_id}),  # the company the claim is linked to
        catalyst_type=v["catalyst_type"] or None,
        drug=v["drug"],
        drug_ids=drugs,
        window=window,
        clean=clean,
    )


def _passes_rules(records: Sequence[Record], corpus: Corpus) -> set[UUID]:
    found = _rule_checks(records, corpus)
    return {r.id for r in records if not r.value["problems"] and not _failed(found[r.id])}


def _independent_source(records: Sequence[Record], corpus: Corpus) -> dict[UUID, list[CheckResult]]:
    """Run `catalyst.verify.independent` on the release's claims and every other release's clean claims."""
    others = [r for r in corpus.records if r.document != records[0].document]
    passed = _passes_rules([*records, *others], corpus)
    found = independent_sources([statement_of(r, corpus, r.id in passed) for r in [*records, *others]])
    return {r.id: [found[r.id]] for r in records}


def _advisory_meeting(records: Sequence[Record], corpus: Corpus) -> dict[UUID, list[CheckResult]]:
    """Run `catalyst.verify.advisory`; no Federal Register notice is stored for the gold releases."""
    same = same_drug_rule(corpus.references, corpus.owners[records[0].document])
    found: dict[UUID, list[CheckResult]] = {}
    for r in records:
        v = r.value
        window = (
            (date.fromisoformat(v["window_start"]), date.fromisoformat(v["window_end"]))
            if v["window_start"] and v["window_end"]
            else None
        )
        claim = AdvisoryClaim(catalyst_type=v["catalyst_type"], drug=v["drug"], window=window)
        stored = notices_in(StoredNotices(), *window) if window else NoticesInWindow()
        found[r.id] = [advisory_notice(claim, stored, same)]
    return found


def _company_named(records: Sequence[Record], corpus: Corpus) -> dict[UUID, list[CheckResult]]:
    """Run `catalyst.verify.company`: the release must name the company the claim is linked to."""
    known = known_companies(corpus.company_refs)
    text = corpus.texts[records[0].document]
    return {r.id: [company_named(text, known[r.company_id])] for r in records}


# Every check on main; a new check is one more line here.
CHECKS: tuple[Check, ...] = (
    _rule_checks,
    _second_reading,
    _independent_source,
    _advisory_meeting,
    _company_named,
)


@dataclass(frozen=True)
class Correction:
    """What the second reading says instead, and the quote it says it with."""

    value: str
    quote: str


@dataclass(frozen=True)
class Planted:
    """One planted record, the checks it failed, and the second reading's correction, if any."""

    error: str
    original: Record
    record: Record
    failed: tuple[str, ...]
    correction: Correction | None

    @property
    def caught(self) -> bool:
        """At least one check failed the planted record."""
        return bool(self.failed)


@dataclass(frozen=True)
class Results:
    """Every planted record's outcome, and every clean record a check failed.

    `flagged` lists clean records failed with every claim of their release clean: they go to
    review as they are, so no error is planted in them. `disturbed` lists clean records that a
    planted neighbour made fail a check they otherwise pass.
    """

    planted: tuple[Planted, ...]
    clean_checked: int
    flagged: tuple[tuple[Record, tuple[str, ...]], ...]
    neighbours_checked: int
    disturbed: tuple[tuple[Record, tuple[str, ...]], ...]
    checks: tuple[str, ...]
    fingerprints: Mapping[str, str]
    seed: int


def _fields(reading: Reading) -> str:
    start, end, precision = reading.window
    return (
        f"type {reading.catalyst_type}, drug {reading.drug or 'none'}, window {start} to {end} ({precision})"
    )


def _correction(record: Record, original: Record, failed: Sequence[str], corpus: Corpus) -> Correction | None:
    """Return the correction when the second reading disagrees and its paired reading is the right one."""
    if SECOND_READING not in failed:
        return None
    batch = [record if r.id == original.id else r for r in corpus.records if r.document == record.document]
    same = same_drug_rule(corpus.references, corpus.owners[record.document])
    said = pairs([_reading(r) for r in batch], _second_readings(record.document, corpus), same).get(record.id)
    if (
        said is None
        or _fields(said) != _fields(_reading(original))
        or _fields(said) == _fields(_reading(record))
    ):
        return None
    return Correction(_fields(said), corpus.texts[record.document][said.quote_start : said.quote_end])


def _failures(
    batch: Sequence[Record], corpus: Corpus, checks: Sequence[Check]
) -> dict[UUID, list[CheckResult]]:
    found: defaultdict[UUID, list[CheckResult]] = defaultdict(list)
    for check in checks:
        for claim, results in check(batch, corpus).items():
            found[claim] += results
    return found


def _failed(results: Sequence[CheckResult]) -> tuple[str, ...]:
    return tuple(sorted({r.check for r in results if r.outcome is Outcome.FAIL}))


Alone = Mapping[UUID, tuple[str, ...]]  # the checks each clean record fails with its release all clean


def _alone(corpus: Corpus, checks: Sequence[Check]) -> dict[UUID, list[CheckResult]]:
    """Run every check on each release with all its claims clean."""
    found: dict[UUID, list[CheckResult]] = {}
    for document in dict.fromkeys(r.document for r in corpus.records):
        found |= _failures([r for r in corpus.records if r.document == document], corpus, checks)
    return found


def _check_planted(
    error: str, record: Record, original: Record, corpus: Corpus, checks: Sequence[Check], alone: Alone
) -> tuple[Planted, list[Record], list[tuple[Record, tuple[str, ...]]]]:
    """Check one planted record among its release's clean claims.

    Return its outcome, its clean neighbours, and the neighbours it made fail a check.
    """
    batch = [record if r.id == original.id else r for r in corpus.records if r.document == record.document]
    found = _failures(batch, corpus, checks)
    neighbours = [r for r in batch if r is not record]
    disturbed = [
        (r, _failed(found[r.id])) for r in neighbours if set(_failed(found[r.id])) - set(alone[r.id])
    ]
    failed = _failed(found[record.id])
    correction = _correction(record, original, failed, corpus)
    return Planted(error, original, record, failed, correction), neighbours, disturbed


def run(corpus: Corpus, *, seed: int = SEED, checks: Sequence[Check] = CHECKS) -> Results:
    """Plant each error type in every clean record that passes every check, one at a time.

    Each planted record is checked among the clean claims of its own release.
    """
    clean = _alone(corpus, checks)
    alone = {claim: _failed(results) for claim, results in clean.items()}
    planted: list[Planted] = []
    disturbed: list[tuple[Record, tuple[str, ...]]] = []
    neighbours = 0
    for error in ERRORS:
        for original in corpus.records:
            for record in () if alone[original.id] else plant(error, original, corpus, seed):
                found, beside, moved = _check_planted(error.name, record, original, corpus, checks, alone)
                planted.append(found)
                neighbours += len(beside)
                disturbed += moved
    flagged = tuple(
        (r, tuple(f"{x.check}: {x.reason}" for x in clean[r.id] if x.outcome is Outcome.FAIL))
        for r in corpus.records
        if alone[r.id]
    )
    names = {x.check for results in clean.values() for x in results} | {n for p in planted for n in p.failed}
    return Results(
        tuple(planted),
        len(corpus.records),
        flagged,
        neighbours,
        tuple(disturbed),
        tuple(sorted(names)),
        corpus.fingerprints,
        seed,
    )


# --- Scores, the floor, and the owner's page ---------------------------------------------------


def _by_rules(found: Planted) -> bool:
    return any(name != SECOND_READING for name in found.failed)


def scores(results: Results) -> dict[str, Any]:
    """Return the counts `scores.json` holds, per error type, and what earned them."""
    errors: dict[str, dict[str, int]] = {}
    for error in ERRORS:
        found = [p for p in results.planted if p.error == error.name]
        errors[error.name] = {
            "planted": len(found),
            "caught": sum(p.caught for p in found),
            "caught_by_rules": sum(_by_rules(p) for p in found),
            "corrected": sum(p.correction is not None for p in found),
            "missed": sum(not p.caught for p in found),
        }
    clean = {
        "records": results.clean_checked,
        "wrongly_flagged": len(results.flagged),
        "neighbours_checked": results.neighbours_checked,
        "neighbours_wrongly_flagged": len(results.disturbed),
    }
    return {
        "seed": results.seed,
        **results.fingerprints,
        "checks": list(results.checks),
        "errors": errors,
        "clean": clean,
    }


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 0.0


def floor_problems(counted: Mapping[str, Any], baseline: Mapping[str, Any]) -> list[str]:
    """Return every way the counts fall below `baseline.json`; empty when they hold the floor."""
    problems: list[str] = []
    measures = (
        ("catch_rate", "caught"),
        ("rule_catch_rate", "caught_by_rules"),
        ("correct_rate", "corrected"),
    )
    for measure, key in measures:
        floors: Mapping[str, float] = baseline.get(measure, {})
        for name, row in counted["errors"].items():
            rate = _rate(row[key], row["planted"])
            if name not in floors:
                problems.append(f"baseline.json names no {measure} floor for {name}")
            elif rate < floors[name]:
                problems.append(f"{name}: {measure} fell to {rate}, below the floor {floors[name]}")
    least: Mapping[str, int] = baseline.get("planted", {})
    for name, row in counted["errors"].items():
        if row["planted"] < least.get(name, 1):
            problems.append(
                f"{name}: only {row['planted']} copies planted, below the floor {least.get(name, 1)}"
            )
    for key in ("wrongly_flagged", "neighbours_wrongly_flagged"):
        limit = baseline.get(f"clean_{key}")
        if limit is None or counted["clean"][key] > limit:
            problems.append(
                f"{counted['clean'][key]} clean records {key.replace('_', ' ')}, above the floor {limit}"
            )
    return problems


def _event(record: Record, corpus: Corpus) -> str:
    company = corpus.companies[corpus.owners[record.document]]
    return f'{company}, {record.value["drug"] or "no drug named"}, "{record.value["date_text"]}"'


def _change(found: Planted, corpus: Corpus) -> str:
    before, after = parts(found.original, corpus), parts(found.record, corpus)
    if before["company"] != after["company"]:
        return f"linked to {corpus.companies[found.record.company_id]}"
    if before["trials"] != after["trials"]:
        return "linked to trial " + "; ".join(
            corpus.trial_names[t] for t in trial_links(found.record, corpus)
        )
    if before["quote"] != after["quote"]:
        return f'quote swapped for "{found.record.quote[:90]}"'
    changed = [name for name in ("date_words", "window", "drug", "type") if before[name] != after[name]]
    return "; ".join(f"{name} {before[name]} became {after[name]}" for name in changed)


def _table(counted: Mapping[str, Any]) -> list[str]:
    rows = [
        "| Error planted | Planted | Caught | Caught without the second reading | Corrected (best case) "
        "| Missed |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    totals: dict[str, int] = dict.fromkeys(("planted", "caught", "caught_by_rules", "corrected", "missed"), 0)
    for error in ERRORS:
        row = counted["errors"][error.name]
        rows.append(f"| {error.label} | " + " | ".join(str(row[k]) for k in totals) + " |")
        totals = {k: totals[k] + row[k] for k in totals}
    rows.append("| **All errors** | " + " | ".join(f"**{v}**" for v in totals.values()) + " |")
    return rows


def _missed(results: Results, corpus: Corpus) -> list[str]:
    """List each missed error type once with its reason, and each event it was missed on, with an example."""
    lines = ["## Missed cases", ""]
    for error in ERRORS:
        found = [p for p in results.planted if p.error == error.name]
        missed = [p for p in found if not p.caught]
        if missed:
            lines += [
                f"**{error.label}** ({len(missed)} of {len(found)} missed). Why: {error.blind_spot}",
                "",
            ]
            for original in dict.fromkeys(p.original.id for p in missed):
                mine = [p for p in found if p.original.id == original]
                lost = [p for p in mine if not p.caught]
                lines.append(
                    f"- {_event(lost[0].original, corpus)}: {len(lost)} of {len(mine)} missed, "
                    f"for example {_change(lost[0], corpus)}"
                )
            lines.append("")
    return lines if len(lines) > 2 else [*lines, "None.", ""]


def _plain(record: Record, reason: str) -> str:
    """Say a known check failure in plain words."""
    if reason.startswith(SECOND_READING) and not record.value["drug"]:
        return "no drug is named (the trial tests several), so the second reading cannot confirm it"
    return reason


def _sec_note(corpus: Corpus) -> str:
    if all(_tier(url) is SourceTier.REGULATOR_OR_SEC for url in corpus.urls.values()):
        return (
            " Every gold release is an SEC filing, which the publish rule lets stand alone, so "
            "independent_source passes every claim here and can catch nothing yet."
        )
    return ""


def render_report(results: Results, corpus: Corpus) -> str:
    """Return the one-page Markdown result for the owner."""
    counted = scores(results)
    clean = counted["clean"]
    left = [
        f"- {_event(r, corpus)}: left out; the extractor flags it ({r.value['problems'][0]})."
        for r in corpus.left_out
    ]
    left += [
        f"- {_event(r, corpus)}: {'; '.join(_plain(r, x) for x in reasons)}."
        for r, reasons in results.flagged
    ]
    total = len(corpus.records) + len(corpus.left_out)
    lines = [
        "# Planted-error test",
        "",
        f"We took the {total} hand-checked catalysts in the gold set, made copies with one realistic error "
        "each, and ran every check we have on each copy among the correct claims of its own press release. "
        "Where an error has several forms (every other trial, company, drug, or type), each form is its own "
        f"copy. Plain code, no AI, the same result every run (seed {results.seed}).",
        "",
        *_table(counted),
        "",
        "- **Caught**: at least one check failed the copy, so it goes to the review queue, not the site.",
        "- **Caught without the second reading**: a rule check failed it too.",
        "- **Corrected (best case)**: the second reading gave the right value, with its own quote. The "
        "stand-in reads every event right, so its quote is the original's by construction.",
        "- **Missed**: every check passed the copy, so it would have published.",
        *[f"- **{e.label}**: {e.note}" for e in ERRORS if e.note],
        "",
        "The second reading in this test is a stand-in that always reads the release correctly, so "
        '"caught" and "corrected" are best cases; the real second reading can misread too.',
        "",
        f"**Correct records wrongly flagged:** {clean['wrongly_flagged']} of {clean['records']} "
        f"correct claims checked on their own, and {clean['neighbours_wrongly_flagged']} of "
        f"{clean['neighbours_checked']} checks of correct claims beside a planted copy. A claim that is "
        "flagged even when correct goes to review as it is, so no error is planted in it:",
        "",
        *left,
        "",
        *_missed(results, corpus),
        f"Checks run: {', '.join(results.checks)}.{_sec_note(corpus)}",
        "",
        "Made by `uv run catalyst planted-errors --out evals/planted_errors/report.md "
        "--scores evals/planted_errors/scores.json` (ADR 0018, part 4).",
    ]
    return "\n".join(lines) + "\n"
