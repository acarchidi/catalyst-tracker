# 0009 — Coverage list from SEC industry codes

- **Status:** accepted
- **Date:** 2026-10-16
- **Roadmap item:** Coverage list from SEC industry codes 2834, 2836, 8731 (therapeutics developers only); leads to Gate 1

## Context

The spec starts coverage with "US-listed therapeutics developers filed under SEC industry codes 2834 (pharmaceutical preparations), 2836 (biological products), and 8731 (commercial biological research)", built without price data. It leaves open what makes a company a therapeutics developer and how the list is kept current. The industry codes alone are too broad: 8731 holds contract research firms (IQVIA, ICON, Medpace, Charles River), and 2834 holds makers of generics, supplements, and animal products.

## Decision

1. **Registrants.** EDGAR's company browse (`/cgi-bin/browse-edgar?action=getcompany&SIC=…&output=atom`, 100 per page) lists every registrant under each code. SEC's robots.txt, read on 2026-10-16, disallows paths such as `/core/`, `/search/`, and `/admin/` but nothing under `/cgi-bin/`.
2. **US-listed.** SEC's `company_tickers_exchange.json` keeps companies with a ticker on a US exchange (Nasdaq, NYSE, Cboe, the Texas Stock Exchange); over-the-counter and unlisted registrants are left out. The main ticker is the first without a class or rights suffix.
3. **Therapeutics developer means trial evidence.** A company is included when ClinicalTrials.gov lists it as the industry lead sponsor of at least one interventional drug or biological trial under its own name. One query per company (`query.spons`, filtered by sponsor class, study type, and intervention type, 1,000 results, lead-sponsor field only) at the existing ClinicalTrials.gov rate.
4. **Same name, written differently.** Names match after ignoring case, punctuation, "&" versus "and", EDGAR's state suffix ("/DE/", " / MA"), and trailing legal-form words (Inc, Corp, Ltd, plc, SE, and similar). Dotted, slashed, and spaced legal forms ("S.A.", "N.V.", "A/S", "A S") count as legal forms. A sponsor that states its parent in its own name ("Stiefel, a GSK Company", "…, a wholly owned subsidiary of Zentalis Pharmaceuticals, Inc") counts for that parent, when the parent's name matches by the same rule. Nothing else: a prefix or partial match is not a match ("Regeneron" is not "Regeneron Pharmaceuticals"). A diagnostic run of 229 left-out companies on 2026-10-16 showed why: prefix matching would have joined ICON plc, a research-services firm, to the unrelated ICON Bioscience, and Merck & Co. to Merck KGaA. The matched registry names become the company's `ctgov_sponsors`, which the trial collector links on exactly (ADR 0005).
5. **Corrections by hand.** `config/coverage_overrides.toml` adds registry names that differ from the SEC name (Moderna registers trials as "ModernaTX, Inc."), or includes or excludes a company outright. Every entry needs a note naming its source. Each hand-added name is searched on its own, so the list's trial count covers every name the company uses. It starts with the ten CP3 pilot companies' hand-checked names and four large companies that register trials under subsidiary names (Merck & Co., Johnson & Johnson, GSK, Takeda).
6. **Outputs.** `catalyst coverage-build` writes `config/coverage_companies.toml` (the included companies, in the format the collectors read, with name, industry code, exchange, and trial count) and `docs/coverage-review.md` (every listed company left out, with the reason and an EDGAR link). Both are rebuilt, never edited.

## Alternatives

- **Industry codes alone.** Simple, but includes research-service firms and non-drug makers the spec excludes.
- **FDA application evidence (Drugs@FDA).** Misses developers with no approved product, which is most of biotech; useful later as a second signal.
- **Fuzzy name matching.** Finds more companies, but the trial collector links on exact names, and a wrong match would attach another company's trials. The review list and overrides catch the misses instead.
- **SEC's bulk submissions file.** Has every company's industry code in one download, but is over a gigabyte; the browse pages need about 30 requests.

## Consequences

- Companies that register trials only under a subsidiary or former name are left out until an override adds the name. The review list is where to look; the entity resolver (Phase 1) can propose overrides.
- Tool and service companies that sponsor one trial of their own are included (MaxCyte, with 1 trial, in the recorded 8731 run). The trial count in the list shows how strong each company's evidence is.
- Trial counts are lower bounds for the largest sponsors: each search reads one page of up to 1,000 results, and a sponsor spelling that appears only past that page is not added. Inclusion is not affected, since one matching trial is enough.
- openFDA names are filled only for overridden companies; the others get FDA records once the entity resolver matches sponsor names.
- A full build makes about 30 SEC requests and one ClinicalTrials.gov request per listed company (677 on 2026-10-16) plus one per hand-added name, about 15 minutes at 45 requests a minute. It should run monthly.
