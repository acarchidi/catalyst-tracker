# 0081 — Until 2026-12-08, the newest filings are read first on half of the raised caps

- **Status:** accepted
- **Date:** 2026-11-25
- **Roadmap item:** none; a catch-up of Phase 1 "Extractor agents" on the promotional credit, at the owner's request
- **Amends:** ADR 0066 decision 8 (filings after the releases) and decision 9 (filings never use a raised cap); ADR 0065 decision 7 (the raised caps were the press-release backfill's alone)

## Context

ADR 0066 reads every stored 10-Q, 10-K and other 8-K Exhibit 99, part by part, within each role's usual $2 cap. While the press-release backfill spends (ADR 0065), it takes the extractor past $2 each day, so filings read almost nothing until the credit is used (ADR 0066's estimate: done around late January to mid-February 2027).

On 2026-11-25 the owner wrote "do all those things" to this plan: read the newest stored 10-Qs first, then 10-Ks and other 8-K exhibits, paid from the promotional API credit (about $96, expiring 2026-12-08), so that about 300 to 400 of the newest filings are read by Monday 2026-11-30, for about $25 to $35 of the credit, within the already approved $8 extractor and $4 verifier caps (2026-11-24 to 2026-12-08), with the press-release backfill keeping its share.

Facts that shape the design:

- `model_call` records each call's role, not which document it was for. The caps, the spending alert and the credit check all sum it by role and day.
- The backfill run starts at 00:23 New York (ADR 0065, decision 5), so it is the first spender of each New York day. Its reading step has 120 minutes (`--max-minutes 120`) and reads releases until the extractor's $8 is spent; anything after it that day finds the cap used.
- The nightly run starts at 19:43 New York with about 16 minutes of reading time (ADR 0064).

## Decision

1. **Order.** Filings are read newest first by New York filing date (the date of `published_at`, EDGAR's acceptance time), and on the same date a 10-Q before a 10-K before an exhibit. A started filing still comes first, so a stopped run goes on with its next part, and the free re-checks still come last.
2. **The share, in the owner's file.** A raised cap in `config/models.toml` may carry `filings_share` with its own `filings_approved` line. The extractor's $8 and the verifier's $4 carry `filings_share = "0.50"`, approved in writing on 2026-11-25. The code refuses a share above 0.5, or one without `filings_approved`; with no share (the default) filings never use a raised cap, as before. The caps themselves do not change.
3. **The rule.** On a day whose raised cap gives filings a share, and only while the backfill's credit check holds for the day (the day is not after 2026-12-08, and every model call since 2026-11-24, of any role and any job, filing reads included, plus one more whole day of $14, stays within $96), a filing part starts only while the role's **whole** spend that day plus that part's worst case stays within the share: $4.00 for the extractor, $2.00 for the verifier. Otherwise the line is the usual $2.00, as in ADR 0066. Because the line is compared with everything the role spent that day, releases included, filings can never take more than half of the day's raised cap, whichever runs first.
4. **Filings first while on the credit, with half the reading time.** On such a day, `extract-catalysts` reads filings before the press releases, until the share is reached or half of the run's reading time has passed (60 of the backfill's 120 minutes; about 8 of the nightly run's 16), then reads the releases with the rest of the time and of the raised cap. Off the credit the order is ADR 0066's: releases first, filings after, in the same reading time. The second reading keeps its order (releases, then filings) under the same guard.
5. **Back by itself.** From 2026-12-09 there is no raised cap, so no share: filings are back at the usual $2.00 caps and after the releases, with no change to the file. A credit check that cannot be made (the database cannot be reached) also keeps the usual caps.
6. **What is reported.** A stop at the share reads "the filings' share of today's raised cap is reached; the rest is the backfill's (ADR 0081)", a normal end like the usual-cap stop.

### Estimate, with the working

- **Worst case of one part:** 88,000 characters at 3 per token (29,334 tokens at $2 a million, $0.059) plus a full 16,000-token answer ($0.16): about $0.22. So about $3.78 of the extractor's $4 share is usable a day.
- **Cost per filing:** ADR 0066's estimate, about $0.06 of extractor per 10-Q, $0.19 per 10-K, $0.02 per text exhibit. November's newest filings are mostly third-quarter 10-Qs, with a few exhibits and 10-Ks.
- **Per backfill run:** about 55 to 75 filings for the money; at about 48 seconds a 10-Q (ADR 0066), the 60 minutes allow about 75, so money, not time, is the limit.
- **By Monday 2026-11-30:** five backfill runs (2026-11-26 to 2026-11-30, if this merges before 05:23 UTC on 2026-11-26): **about 280 to 370 filings**, about $19 of extractor money. The verifier's share is mostly unused, because the second reading has about 9 minutes after the reading step in the backfill run (ADR 0064's shares) and reads releases first; filings' claims are compared later, as time allows.
- **Credit:** filings spend inside the same two raised caps, so one day still costs at most $14 and the credit lasts as long as before; the releases get about $4 of the extractor's $8 instead of all of it, about 190 releases a day instead of 390.

## Alternatives

- **Filings after the releases, with the share counted on filing calls only.** Needs a new column or table to tell filing calls from release calls (a migration for a job that ends in two weeks), and the backfill's releases, read first, would spend the whole $8 before filings start.
- **A cap of filings' own (a new role).** Adds money to the day's total, which the owner did not approve; the credit check's $14 would no longer hold.
- **Stop releases at half the raised cap instead.** Changes the press-release reader, and if releases ran out early filings would still be limited by a split nobody needs.
- **The whole raised cap for filings.** The owner wants the backfill to keep its share.

## Consequences

- About 300 of the newest filings are read by 2026-11-30 on the credit, and their claims go through the same checks and publish gate.
- The press-release backfill reads about half as many releases a day while it lasts; its newest-first order is unchanged.
- On a credit day the nightly run reads filings first for up to half its reading time, but only while the extractor's spend that day is under $4, which after a full backfill run it is not.
- The second reading of filings lags; to give it more minutes in the backfill run, the backfill workflow's reading step would need a larger `--time-budget-minutes` (a workflow change, an owner path, not made here).
- After 2026-12-08 the `filings_share` lines can be removed with the dated caps in the same clean-up.
