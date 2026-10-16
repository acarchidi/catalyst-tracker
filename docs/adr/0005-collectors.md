# 0005 — Collectors: sources, identity keys, politeness

- **Status:** accepted
- **Date:** 2026-10-16
- **Roadmap item:** Collectors: SEC EDGAR, ClinicalTrials.gov, wire RSS, openFDA, Federal Register (checkpoint CP3)

## Context

The spec lists the launch sources and requires recorded-fixture tests, idempotent jobs, respect for robots.txt and site terms, and rate limits on every source. ADR 0002 deferred per-source document identity to this item, and the CP1 review asked for it.

## Decision

1. **Identity.** Every stored document has a `source_key` built from the source's own identity: `sec:<accession>:<file>`, `ctgov:<NCT>:<last update submitted>`, `openfda:<application>:<latest action date>:<number of submissions>`, `fedreg:<document number>`, `wire:<feed>:<guid>`. A key is stored once; a changed record (a trial update, a new FDA action) gets a new key and a new row. This replaces ADR 0002's URL-plus-hash rule, which would have added a row on every fetch of sources that return slightly different bytes each time.
2. **What each source stores.** SEC: 8-K Exhibit 99 press releases, and the main document of 10-Q, 10-K, S-3, S-3ASR, and 424B filings. ClinicalTrials.gov: the full study record as JSON, linked to the public study page. openFDA: the Drugs@FDA application record as JSON, linked to the Drugs@FDA page. Federal Register: FDA advisory committee meeting notices (title contains "Advisory Committee" and "Notice of Meeting"), linked to the notice page. Wires: the RSS item (title, link, date, summary), linked to the release page; full release text is fetched later by the extractor items.
3. **Company links.** A `company_document` row links a document to a company only on an exact match: the SEC number for filings, an exact registered lead-sponsor name for trials, an exact sponsor name for FDA applications. The names per company live in `config/pilot_companies.toml`; ClinicalTrials.gov's sponsor search is fuzzy (a search for Insmed returns a University of Dundee trial), so the exact check is what decides. Federal Register and wire items are not linked here; the entity resolver (Phase 1) does that.
4. **Rates.** One shared limiter per host per process: SEC 8 requests a second, ClinicalTrials.gov 0.75 (45 a minute), openFDA 2, Federal Register 1, PR Newswire 0.5. Redirects are never followed; only listed hosts can be fetched.
5. **robots.txt.** Web pages and feeds (PR Newswire) obey robots.txt; a missing file allows, an unreadable one blocks. Documented public APIs follow their own access policies. ClinicalTrials.gov's robots.txt disallows `/api/` for crawlers; the owner decided on 2026-10-16 to use the API as its documentation intends, at under 50 requests a minute.
6. **Wire coverage.** Only PR Newswire's biotechnology feed is collected. GlobeNewswire and Business Wire's main site refuse requests from this environment (Akamai "Access Denied"); their feeds are not bypassed. Business Wire's feed host answers, but its health feed address could not be found without the blocked site. Revisit from GitHub Actions runners, which use different addresses.
7. **Re-runs.** SEC filings never change, so a filing with any stored document is skipped without fetching it. Other sources are fetched in full each run and deduplicated by key.
8. **Failures.** One source failing (an HTTP error or a robots.txt refusal) logs an error-level `collector_failed` alert and the run continues with the other sources; the command exits non-zero so a scheduled job shows as failed.
9. **Dates.** Sources that publish a date without a time (registry updates, FDA actions, Federal Register) are stored as midnight New York time on that date.

## Alternatives

- Searching openFDA by `sponsor_name`: returned 2 of Vertex's 8 applications; searching by manufacturer and filtering on exact sponsor name finds all 8.
- Fetching full wire release pages now: more data than Phase 0 needs, and more requests per run.

## Consequences

The coverage-list item replaces the ten hand-picked pilot companies with companies chosen by SEC industry code, and needs each company's registry and FDA sponsor names; finding those names is entity resolution work for Phase 1. Federal Register pages show an access check to scripted requests, so the weekly link checker (Phase 1) must treat that page as reachable.
