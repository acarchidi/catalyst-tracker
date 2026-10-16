# 0010 — Gate 1 tools: company choice, record sample, link check

- **Status:** accepted
- **Date:** 2026-10-16
- **Roadmap item:** Gate 1 (end of Phase 0): "50 companies flow from source to the live database, every record with a working deep source link"

## Context

Gate 1's preview is "50 companies end to end; random 10 records for the owner to click", and its pass criterion is "every record has a working deep link" (docs/quality-gates.md). The spec also asks for a weekly link check (Provenance, item 6). Neither says how the 50 or the 10 are chosen, or what counts as a working link.

## Decision

1. **The 50 companies.** The 10 CP3 pilot companies, whose registry and FDA names were checked by hand, plus 40 drawn at random from `config/coverage_companies.toml` with seed 2026. `catalyst gate1-select` writes them to `config/gate1_companies.toml`; the same seed always gives the same list, so anyone can check the choice was not hand-picked.
2. **The 10 records.** `catalyst sample-records` draws 10 stored documents at random (seed 2026), taking them from each source in turn (SEC, ClinicalTrials.gov, FDA, Federal Register, wires), and prints each one's company, source, type, publication time, link, and the first 12 characters of its fingerprint.
3. **A working link.** `catalyst check-links` fetches each stored source link once and gives one verdict: works (HTTP 200 at that exact address), moved (a redirect; the new address is shown), missing (404 or 410), refused by the site (401, 403, 429), site error or no answer, or not checked (the site's robots.txt does not allow it, or the link is not on one of the five source sites). Only "works" passes; the command exits non-zero otherwise. `--sample N` checks a repeatable random subset.
4. **Politeness.** The link checker uses only the five source sites (SEC, ClinicalTrials.gov, FDA's Drugs@FDA pages, the Federal Register, PR Newswire), each at the collectors' rates (SEC at 8 a second, ClinicalTrials.gov at 45 a minute), obeys every site's robots.txt, and never follows redirects. ClinicalTrials.gov's robots.txt disallows `/api/` but allows the `/study/` pages records link to.

## Alternatives

- **A plain random draw of 10 records.** Tried in the dress rehearsal: all 10 were trial records, since trials are 91% of the documents (2,304 of 2,543), so no SEC or FDA link would have been clicked.
- **Hand-picking the 40.** Would favor easy companies; a seeded draw is a fair test.
- **Only the 10 pilot companies plus the 40 largest.** Large companies are the best-documented; a random draw includes small ones, which is where problems show up.
- **Counting a redirect as working.** A redirect means the stored address is no longer the document's address; the spec wants deep links, so the stored address must be fixed.

## Consequences

- Checking every ClinicalTrials.gov link takes time at 45 a minute (1,562 links took about 35 minutes in the CP3 data). The weekly job (Phase 1) can check all of them; Gate 1 checks every link of the 50 companies.
- Federal Register pages may answer scripted requests with an access check (ADR 0005); if they do, those links show as refused rather than missing, and the report says so.
- The live Gate 1 run still needs Supabase (setup Part 3): the same commands run with `--snapshot-store supabase` and `CATALYST_DATABASE_URL` pointing at the live database.
