# 0040 — The weekly sample checker: a stronger model re-reads a random draw; plain code decides

- **Status:** accepted
- **Date:** 2026-10-20
- **Roadmap item:** Phase 1, "Auditor agent, weekly link check, sample checker on a stronger model, planted-error test, alerts only when something needs the owner plus a weekly summary" (part 1: the sample checker)

## Context

The spec (Human review and accuracy measurement) says: each week 30 auto-published records are drawn at random and checked against their sources by a different, stronger Claude model than the ones that extracted and verified them; the owner settles only the records where it disagrees. That sample is the only real measure of accuracy, and Gate 2 rests on it.

It leaves open how the draw is made, what the model sees, what counts as a disagreement, where results go, and which role pays. Facts that matter:

- The readers (extractor and verifier) are Sonnet 5.5. The only Opus 5.5 role is `analyst` ($0.50 a day, `max_tokens` 16,000, effort high). Analyst drafts start in Phase 4.
- `ModelClient` refuses a call when today's spend plus that call's worst case (its input plus a full 16,000-token answer, about $0.32 of output on Opus) would pass the cap. The spend alert fires once less is left than one full answer, that is above about $0.18 spent (ADR 0022, ADR 0023).
- The checker reads web text, so it gets no tools and no database access, and plain code must write its result (spec: Agent architecture).

## Decision

1. **The draw.** Each week runs Monday to Sunday, New York time. The population is every published catalyst (as the read API shows it, `catalyst.api.read.published`) whose first change was learned before that Monday. Sorted by id, 30 are drawn with `random.Random("sample:<Monday>")`, so the draw is the same on every run that week and anyone can repeat it. Records published during the week wait for the next week.
2. **What the model sees.** One call per source of the record: the document's publication date, the record's points that this source backs, numbered, and the document's visible text (the same text the readers see). The points are: type, company, drugs (each with alone or in combination), indication, date (start, end, precision, and the date words the record says the source used), and trials. The title is composed in code from checked fields, and the date label and status follow from the type and date, so they are not separate points. Seeing the values lets the model check what was published, not re-extract everything; the prompt tells it its job is to find the readers' mistakes.
3. **What the model answers.** JSON only (`prompts/check_sample/1.0.0.toml`): for each point, `supported`, `contradicted`, or `not_stated`, with the deciding sentence copied from the document and a short reason.
4. **Plain code decides.** A point agrees only when every answer for it is `supported` and each one's quote is found in the source's visible text (spacing aside, as for every quote). A point with no answer, a `contradicted` or `not_stated` answer, or a quote that is not in the source, disagrees. Answers for points not asked are ignored. A record agrees only when every point of every source agrees.
5. **Storage.** `sample_check` holds one row per record per week's draw, prompt version, and model: the verdict, and every point with its source claim and link, the published value, the answer, code's reason, and the checker's own quote and reason. A record is stored only when every one of its sources was checked; a failed or capped call stores nothing, and the next run tries again. A re-run that week spends nothing on records already stored.
6. **Who pays.** The `analyst` role (Opus 5.5), inside its $0.50 daily cap; no config change. `catalyst sample-check` checks one record per run by default (`--limit`). One record costs about $0.05 to $0.15, so one a day stays under the spend alert's line. When the analyst starts drafting in Phase 4, the checker gets its own role, which needs the owner's cap decision.
7. **Alerts.** Each disagreement becomes its own GitHub issue (`sample-disagrees:<catalyst>:<Monday>`, ADR 0023) naming the record's id and title, the points in code's words ("the date", "the drugs"), and its source links. The checker's words never reach an issue: they come from reading web text. No run closes such an issue; the owner closes it once settled. Records that could not be checked, or a run stopped by the cap, raise the job's usual alert (`job-failed:sample-check`), which a clean run closes. The command exits non-zero only in those two cases; a disagreement is a result, not a failure.
8. **Until the checker is scored, its agreements are not the error rate.** The prompt's evaluation set is the planted-error records (ADR 0018, part 4). Until the checker has been run on them in GitHub Actions with the product key, and its catch rate committed, the weekly summary reports the sample's counts but no error rate from it.

## Alternatives

- **A blind re-extraction compared in code, like the second reading.** More independent, but it repeats the second reading's design with a different model, costs more output per record, and gives a disagreement with no point named. The checker here is told to look for errors and must quote the sentence that decides each point.
- **One call per record with every source in it.** Fewer calls, but a large worst case under a $0.50 cap; most records have one source anyway.
- **A new role for the checker.** Needs a new cap, which adds to the $6 total the owner set (ADR 0022). Not chosen while the analyst has no other work.
- **Drawing from every record published so far, re-drawn each run.** The population grows during the week, so the draw would change from one day to the next and earlier checks would not belong to it.

## Consequences

- At one record a day, about 7 records a week are checked, not 30. Checking all 30 needs about 4 to 5 Opus calls a day: about $0.45 a day spent, plus the client's worst-case headroom of about $0.40, so an analyst cap near $0.90 a day (at most about $27 a month; expected about $13). That is the owner's decision; this record changes no cap. At 7 a week, the 300 clean checks Gate 2 needs take about 43 weeks; at 30 a week, about 10.
- A source can try to steer the checker into answering `supported`. Code still demands a real quote from the source for each point, but a misleading page could make a wrong record look checked. The planted-error scoring (decision 8) and a second provider later (spec: Later) are the defences.
- Remaining parts of the roadmap item: the weekly link check on published sources, the auditor's other re-checks (quotes still on the live page, dates passed without an outcome), the weekly summary (`catalyst weekly-summary`, ADR 0023 decision 4), the scheduled workflows (owner-path pull requests), and scoring the checker on the planted errors.
