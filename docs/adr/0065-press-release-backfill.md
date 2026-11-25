# 0065 — Backfill of 2025 and 2026 press releases, paid by the promotional credit until 2026-12-08

- **Status:** accepted
- **Amended by:** ADR 0081 (2026-11-25), decision 7: filings may use at most half of each raised cap while the credit holds; the backfill keeps the rest.
- **Date:** 2026-11-23
- **Roadmap item:** none; a one-off catch-up of Phase 1 "Collectors" and "Catalyst extractor" (owner request 2026-11-23)
- **Amends:** ADR 0051, decision 1 (one more scheduled job, until 2026-12-08) and ADR 0022 (two caps raised for a dated period)

## Context

The live jobs read only 8-K Exhibit 99.1 press releases filed since about July 2026, at most 20 a night (ADR 0051). Of the 466 covered companies, 114 have a published catalyst. A company that announced a catalyst in a 2025 or early-2026 release and has not repeated it since is missing.

The owner has a promotional API credit of about $96 that expires on 2026-12-08, and asked on 2026-11-23 that it be used to read the older releases of covered companies. In writing, the owner allowed the extractor's daily cap, and if needed the verifier's, to be raised to at most $8 a day until 2026-12-08, and then lowered back to $2.

Facts that shape the design:

- EDGAR's filing list (`submissions/CIK….json`) has a "recent" block of at least a year or 1,000 filings; older filings are on extra pages it names. Vertex's recorded list reaches back to 2020, but a company with many filings can need an older page to reach January 2025.
- The list names each 8-K's items. An 8-K without item 9.01 ("Financial Statements and Exhibits") has no exhibit, so no press release. In Vertex's list, 24 of 53 8-Ks have no 9.01.
- The weekday collection reads the last 10 days of filings and stores every Exhibit 99 of an 8-K (ADR 0046's table). Only Exhibit 99.1 is ever read; other Exhibit 99s are often slide decks in PDF, which do not compress, and the bucket is on the free 1 GB plan (ADR 0051).
- The extractor already reads newest first and skips what it has read (ADR 0038), and `extract-catalysts` runs the whole chain after it: matching, rule checks, the verifier's second reading, the publish decisions, the publish gate, and the indication readers.
- Caps reset at New York midnight. A run that spans midnight could use two days of caps.

## Decision

1. **Collection: `catalyst backfill-collect`** (`collectors/sec/backfill.py`), free, no model call. For every company in `config/coverage_companies.toml` it reads the filing list, and the older pages only when the recent block does not reach 2025-01-01 (only the pages whose dates overlap the period). It keeps 8-K filings filed from 2025-01-01 up to 10 days before today; newer ones are the weekday collection's. It skips, without a request, a filing already stored (by accession number) and an 8-K whose items list no 9.01. For the rest it reads the filing index and stores only the Exhibit 99.1, typed `8-K EX-99.1` and linked to the company by its SEC number, the same way the weekday collector does. EDGAR's limit applies through `EdgarClient` (8 requests a second, the `SEC_CONTACT_EMAIL` User-Agent). A filing whose index page or press release EDGAR answers 404 for is skipped, logged (`filing_not_found`) and listed in the run's summary (`filings_not_found`, `not_found`), and the company's other filings are still read; it is tried again on the next run, and it is not a company failure. Any other error still ends that company's collection for the run (live run 45406121773: one 404 used to end the whole company, 60 of 316 companies). With `--max-minutes` it starts no new company after that time and says how many are left; the next run carries on, and re-running stores nothing twice. After 2026-12-08 it does nothing.
2. **The queue is the extractor's own.** Stored releases are read newest first, so this autumn's go before 2025's, and anything the weekday collection adds still goes first. `catalyst backfill-status` (`extract/backfill.py`) counts the releases from 2025 on that no run has read, leaving out those kept only for a free re-check (ADR 0038).
3. **When the backfill may spend.** `backfill-status` says "active" only when all hold: the New York day is not after 2026-12-08; releases are waiting; and every model call since 2026-11-24, of any role and any job, plus the most one New York day can spend ($14: the raised $8 and $4 caps plus $2 for all other roles), stays within $96. So the credit is never overspent into paid money by the backfill. The daily caps still apply to every call through `ModelClient`; a role at its cap stops and alerts, and never switches model.
4. **Reading: `extract-catalysts` with two new options.** `--max-minutes` starts no new document after that time (`stopped_early` then begins "time limit reached"). `--stop-is-expected` makes a stop at a daily cap or the time limit a clean end (exit 0) instead of a failure; it is still printed. Without these options the command behaves as before, so the weekday job is unchanged.
5. **Workflow: `.github/workflows/backfill.yml`**, its own small pull request (an owner path). Started by hand or once a day at 05:23 UTC (00:23 in New York in winter), so its run starts and ends inside one New York day and uses only that day's caps. Steps: bring the database up to date; `backfill-collect --max-minutes 100`; `backfill-status`; only when active, `extract-catalysts --since 2025-01-01 --limit 500 --max-minutes 170 --stop-is-expected`; then alerts and the separate issue-posting job, as in the other live jobs. The schedule runs only in November and December, and the code refuses after 2026-12-08, so it stops by itself; it also stops spending when nothing is left or the credit is used. It shares the `live-database` concurrency group, so it never overlaps the other live jobs. This is one more model-using job a day until 2026-12-08, each job still at most once a day (ADR 0055, decision 3).
6. **Alerts.** The run reports as `backfill-collect` and `backfill-extract`. For the backfill, a stop at a cap or the time limit is not a problem; a document that could not be read, a check that did not finish, or a company that could not be collected is. The spending check runs as in the nightly job, so the owner sees one "reached its daily spending cap" issue, which stays open while the backfill runs and closes by itself after.
7. **Caps, dated in the file, in their own pull request** (an owner path). In `config/models.toml` the extractor gets $8.00 and the verifier $4.00 until 2026-12-08, written as a raised cap with its last day next to the usual $2.00. The model client and the spending alert read the cap for the New York day of each call, so from 2026-12-09 both roles are at $2.00 again without anyone changing the file.

### Estimate, with the working

- **Releases.** Vertex filed 6 press-release 8-Ks in the 18 months from January 2025 to June 2026 (its recorded list), but issues most of its news on its own site. Smaller biotechs file most material news as 8-Ks. Taking 6 to 12 a year per company over 18 months: 466 × 9 to 18 ≈ **4,200 to 8,400 releases, about 6,000**. From July 2026 on most are already stored.
- **SEC requests and time.** Per company: 1 list, about 1.3 index pages per release (some 9.01 8-Ks have no Exhibit 99.1), and 1 release: 466 × (1 + 13 × 2.3) ≈ 14,500 requests, about 30 minutes at 8 a second, plus storing about 6,000 copies. One to two days of the 100-minute collection step.
- **Storage.** The 13 recorded releases average 66 KB and 7.5 KB compressed (ADR 0046): 6,000 × 7.5 KB ≈ 45 MB, against a bucket at about 0.57 GB of 1 GB.
- **Cost per release.** A median release is about 3,200 tokens of text plus about 1,800 of prompt and schema: about $0.010 of input at $2 per million. Output, with thinking, is a few hundred tokens for a release with nothing to report and one to two thousand for one with catalysts, at $10 per million. ADR 0038 measured $1.84 for 153 releases with nothing to report: $0.012 each. Allow **about $0.02 a release for the extractor ($0.012 to $0.035)**. The verifier reads only releases with a claim, assumed about half, at about the same cost: **about $0.01 per release overall**. The indication readers add little.
- **Per day at $8.** The client stops when the next call's worst case (about $0.18) could pass the cap, so about $7.80 is usable: **about 390 releases a day (220 to 650)**. The verifier then needs about $3.90 a day, so its $2 cap would leave half the claims unchecked each day and the backlog would never clear; its cap goes to $4.
- **Total.** About 6,000 × $0.03 ≈ **$180 to read everything ($80 to $425)**, more than the credit. **$96 lasts about 7 days at about $14 a day** (all roles), roughly 2,700 releases, newest first: from mid-2026 back to about late 2025. Reading all 6,000 at $8 a day takes **about 15 to 16 days**, more than the 15 days to 2026-12-08, and about $84 beyond the credit, which decision 3 refuses.
- **Time per run.** At 10 to 20 seconds a release, 390 releases take 65 to 130 minutes and the second reading half that again: inside the 170-minute limit and the job's 345 minutes.

## Alternatives

- **Run `catalyst collect --since 2025-01-01` by hand.** Reads only the recent block, stores every Exhibit 99 (PDF decks included), and fetches the index of every 8-K, including those with no exhibit. It also reads the 10-Q, 10-K and offering forms unless told not to.
- **Raise the nightly job's limit instead of a new workflow.** `extract-live.yml` is being changed on another branch, and a run there starts at 19:43 New York and would cross midnight.
- **A table that remembers which filings had no press release.** Saves a few hundred index requests a day after the first run, but needs a migration for a job that ends in two weeks.
- **No credit guard; let the caps alone bound the spend.** At $8 + $4 a day for 15 days, up to $180 of model calls, about $84 of it paid.
- **Raise the verifier to $8 as well.** More than the estimate shows it needs; the extra would only buy faster use of the same credit.
- **Lower the caps back by a later pull request.** Someone has to remember on 2026-12-09; the dated cap needs no one.

## Consequences

- Older catalysts that were never repeated in a later release can now publish, each through the same checks and publish gate. Releases from 2025 often name dates already past; those claims are held by the existing checks, as before.
- Until 2026-12-08 the nightly extraction shares the day's raised caps with the backfill, which runs first each New York day. A new release the nightly job cannot read for lack of cap is read by the next morning's backfill, newest first.
- The owner's Anthropic workspace limit of $50 a month (docs/setup.md Part 2) is lower than this plan. If it is still set, model calls fail once it is reached, each failure is reported, and nothing is overspent; to use the whole credit, the owner raises it for November and December.
- After 2026-12-08 the workflow does nothing and can be deleted in a later clean-up pull request; the dated caps can then be removed from the file.
- Revisit if the first run shows a much higher cost per release than $0.03, or if the bucket nears 1 GB.
