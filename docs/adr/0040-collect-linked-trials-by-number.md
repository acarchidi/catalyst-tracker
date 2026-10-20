# 0040 — Collect only the trials we name, by registry number

- **Status:** accepted
- **Date:** 2026-10-20
- **Roadmap item:** Phase 1, "Trials and their sizes on each catalyst" (ADR 0039, decision 6); owner request 2026-10-20 for trial sizes on the private catalyst page

## Context

ADR 0039 fills trial sizes by re-running the sponsor-wide ClinicalTrials.gov collector for every covered company. The live run "Collect (live)" 39367764766 (companies `coverage`, sources `ctgov`) stopped at its 160-minute limit and had still not finished Novo Nordisk.

Why it is slow, from that run's logs and from timing the API on 2026-10-20:

- The collector asks for every trial the sponsor search finds, of any date and status, with every field: there is no date, status, or field filter. Novo Nordisk's sponsor search finds about 1,700 trials (966 led by Novo itself), so 17 pages of 100.
- A page of 100 full records is 3.4 to 5 MB, but the API sends it in under half a second. The rate limit (0.75 requests a second, ADR 0005) adds 1.3 seconds a page.
- In the run, pages were requested 55 to 60 seconds apart. That time is spent storing the page's records one by one: for each record, an upload of its copy to Supabase Storage and two database transactions over the Session pooler, even when the record is already stored.

So neither the rate limit nor the download size is the cause; the number of records is. The private catalyst page needs the sizes of only the trials our claims and catalysts name, which is a small share of all those records.

## Decision

1. **A new source, `ctgov-linked`, that runs only when named.** `catalyst collect --sources ctgov-linked` (or "Collect (live)" with `sources` set to `ctgov-linked`) collects only the linked trials. It is not part of "every source", so a run with no `--sources`, or with `all`, is unchanged. It reads no SEC record and asks no other source.
2. **Which trials are linked.** The registry numbers, once each and sorted, of:
   - every trial a catalyst links (`catalyst_trial`);
   - every trial a claim's name resolved to (`entity_match`, trial, resolved);
   - every trial a claim is about (`claim.subject_type` = trial);
   - every registry number (`NCT` and eight digits, any case) written in a stored claim's quote or value, or in a trial name awaiting a match, whether or not that trial is in our tables yet.

   A trial that is only in the `trial` table, because a sponsor-wide run stored it, is not linked.
3. **How they are fetched.** `/api/v2/studies` with `filter.ids`, 20 registry numbers a request, and a page size equal to the batch so one page holds the answer (no empty follow-up page). The same host limiter as the sponsor-wide run (ADR 0005, 0.75 requests a second). A number the registry does not return is logged as `trials_not_in_registry`, not treated as a failure.
4. **Stored exactly as the sponsor-wide run stores them.** Each record goes through the same `ctgov.to_document` (full record, same source key `ctgov:<NCT>:<last update submitted>`), the same `store_fetched`, and the same `recording_trials`, so snapshots, provenance, and the size rules of ADR 0039 are unchanged. A record already stored under its key is counted as existing and not stored again; re-running adds nothing.
5. **Company links, as ADR 0005 decision 3.** A record is linked to a company only when its lead sponsor is exactly one of the `ctgov_sponsors` names of a company on the list given to the run, and that company is already stored. Other records are stored with no company link.
6. **The sponsor-wide run is unchanged.** It still reads every trial, which the entity resolver's study names and the drug list (ADR 0035, PR #73, which makes its own lighter sponsor-wide request) rely on. This amends ADR 0039, decision 6: to fill the sizes the private page shows, run `ctgov-linked` instead.

## Alternatives

- **Ask the sponsor-wide run for fewer fields (`fields=`).** The download is not the slow part, and a partial record would break ADR 0005 decision 2 (the full study record is stored) and would store different contents under a key that already holds the full record.
- **Skip trials last updated before a cutoff.** This drops old trials from the reference tables the resolver matches study names against, and old trials are often the ones a release names in its pipeline history.
- **Skip the upload and database work for records already stored.** Worth doing later for the sponsor-wide run, but the size backfill relies on re-reading records already stored before PR #80, so it needs its own care.
- **A longer time limit, or splitting the company list.** Still hours of work for data the page does not show.
- **One request per trial (`/api/v2/studies/{nct}`).** The same records with 20 times the requests.

## Consequences

- Filling trial sizes for the private page takes minutes: a few requests and a few hundred records, not tens of thousands.
- A trial a claim names that we had never stored is now stored too, so the next `resolve-claims` run can match it.
- A trial a release names only by study name, with no registry number, and not yet in our tables, is not found by this run; the sponsor-wide run still finds it.
- Revisit if the linked trials grow into the thousands; batching keeps the request count at one per 20 trials.
