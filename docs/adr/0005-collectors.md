# 0005 — Collectors: sources, identity keys, politeness

- **Status:** accepted
- **Date:** 2026-10-16
- **Roadmap item:** Collectors: SEC EDGAR, ClinicalTrials.gov, wire RSS, openFDA, Federal Register (checkpoint CP3)

## Context

The spec lists the launch sources and requires recorded-fixture tests, idempotent jobs, respect for robots.txt and site terms, and rate limits on every source. ADR 0002 deferred per-source document identity to this item, and the CP1 review asked for it.

## Decision

1. **Identity.** Every stored document has a `source_key` built from the source's own identity: `sec:<accession>:<file>`, `ctgov:<NCT>:<last update submitted>`, `openfda:<application>:<latest action date>:<12-character hash of the record>` (the hash catches changes with no new submission, such as a product being discontinued), `fedreg:<document number>`, `wire:<feed>:<guid>`. A key is stored once; a changed record (a trial update, a new FDA action) gets a new key and a new row. This replaces ADR 0002's URL-plus-hash rule, which would have added a row on every fetch of sources that return slightly different bytes each time.
2. **What each source stores.** SEC: 8-K Exhibit 99 press releases, and the main document of 10-Q, 10-K, S-3, S-3ASR, and 424B filings. ClinicalTrials.gov: the full study record as JSON, linked to the public study page. openFDA: the Drugs@FDA application record as JSON, linked to the Drugs@FDA page. Federal Register: FDA advisory committee meeting notices (title contains "Advisory Committee" and "Notice of Meeting"), linked to the notice page. Wires: the RSS item (title, link, date, summary), linked to the release page; full release text is fetched later by the extractor items.
3. **Company links.** A `company_document` row links a document to a company only on an exact match: the SEC number for filings, an exact registered lead-sponsor name for trials, an exact sponsor name for FDA applications. The names per company live in `config/pilot_companies.toml`; ClinicalTrials.gov's sponsor search is fuzzy (a search for Insmed returns a University of Dundee trial), so the exact check is what decides. Federal Register and wire items are not linked here; the entity resolver (Phase 1) does that.
4. **Rates.** One shared limiter per host per process: SEC 8 requests a second, ClinicalTrials.gov 0.75 (45 a minute), openFDA 2, Federal Register 1, PR Newswire 0.5. Redirects are never followed; only listed hosts can be fetched.
5. **robots.txt.** Web pages and feeds (PR Newswire) obey robots.txt; a missing file (404 and other client errors) allows; a refusal (401, 403, 429) or a server error blocks, as Python's own robots parser does for refusals. Documented public APIs follow their own access policies. ClinicalTrials.gov's robots.txt disallows `/api/` for crawlers; the owner decided on 2026-10-16 to use the API as its documentation intends, at under 50 requests a minute.
6. **Wire coverage.** Only PR Newswire's biotechnology feed is collected. GlobeNewswire and Business Wire's main site refuse requests from this environment (Akamai "Access Denied"); their feeds are not bypassed. Business Wire's feed host answers, but its health feed address could not be found without the blocked site. Revisit from GitHub Actions runners, which use different addresses.
7. **Re-runs.** SEC filings never change, so a filing with any stored document is skipped without fetching it. All of a filing's documents are fetched before any is stored, so a failure part-way stores none of them and the next run retries the whole filing. Other sources are fetched in full each run and deduplicated by key. Federal Register pages after the first are fetched from the API's own next-page address, which carries every filter. openFDA answers 404 when nothing matches; that counts as zero results.
8. **Failures.** One source failing for any reason (an HTTP error, a robots.txt refusal, a page that will not parse) logs an error-level `collector_failed` alert and the run continues with the other sources; the command exits non-zero so a scheduled job shows as failed.
9. **Dates.** Sources that publish a date without a time (registry updates, FDA actions, Federal Register) are stored as midnight New York time on that date.

10. **Older rows.** The migration that adds source keys rebuilds SEC rows' keys from their links (the folder digits are the accession number), so a later collection does not store them again; other old rows get a `legacy:` key.

## Alternatives

- Searching openFDA by `sponsor_name`: returned 2 of Vertex's 8 applications; searching by manufacturer and filtering on exact sponsor name finds all 8.
- Fetching full wire release pages now: more data than Phase 0 needs, and more requests per run.

## Consequences

The coverage-list item replaces the ten hand-picked pilot companies with companies chosen by SEC industry code, and needs each company's registry and FDA sponsor names; finding those names is entity resolution work for Phase 1. Federal Register pages show an access check to scripted requests, so the weekly link checker (Phase 1) must treat that page as reachable. The Federal Register also refused some requests from this environment on 2026-10-16 (HTTP 403 on 2 of about 7 requests; the next request succeeded); the run isolates that failure and the next scheduled run collects what was missed, so watch its failure rate once runs are scheduled.
