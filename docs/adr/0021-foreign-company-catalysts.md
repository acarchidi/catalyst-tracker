# 0021 — Foreign company catalysts, in two stages

- **Status:** accepted
- **Date:** 2026-10-17
- **Roadmap items:** Phase 1, "Foreign companies listed in the US"; Phase 5, "Companies listed only outside the US"

## Context

On 2026-10-17 the owner asked for catalysts of public foreign companies, with every filing source that needs. Foreign companies fall into two groups, and they need very different work.

### Group 1: foreign companies listed in the US

These are foreign private issuers, many with American depositary shares. They file on EDGAR ([SEC overview for foreign private issuers](https://www.sec.gov/divisions/corpfin/internatl/foreign-private-issuers-overview.shtml)).

- **Annual reports.** Most file Form 20-F. Canadian issuers may use Form 40-F instead.
- **News.** Most file Form 6-K for what they publish at home, including press releases. Some choose the domestic forms instead (8-K, 10-Q, 10-K): CRISPR Therapeutics files 8-Ks and 10-Qs, for example.
- **Where the press release sits.** In a 6-K, the press release can be an exhibit (argenx uses EX-99.1 and EX-99.2). It can also be the main 6-K document with no exhibit at all, as at GSK and Takeda.
- **Noise.** Large issuers file many 6-Ks that are not catalyst news. GSK filed about 900 in four years, mostly share-buyback notices.

The coverage list (ADR 0009) filters by industry code and US exchange, so these companies are already on it: argenx, Genmab, Novo Nordisk, AstraZeneca, Takeda, GSK, and others. But the collector reads only 8-K press-release exhibits. Their catalysts are being missed today.

### Group 2: companies listed only outside the US

Examples are companies listed in London, Euronext, Switzerland, the Nordics, Germany, Australia, Hong Kong, Japan, Korea, mainland China, Canada, and Israel.
- Each market has its own announcement system and access terms, and some use another language.
- Their trials are often in other registries, and their approvals come from other regulators.

## Decision

1. **Stage 1, in Phase 1 before the gold set grows to 200: foreign companies listed in the US.** No new account or key is needed, because everything comes from EDGAR. Doing it before the gold set means 6-K records are part of the Gate 2 sample.
   - **Forms.** The collector follows the forms each company actually files, whatever its status:
     - 6-K press releases: every EX-99 exhibit, or the main document when there is none;
     - 20-F and 40-F annual reports;
     - half-year and quarterly results filed on 6-K, for cash and runway;
     - F-1, F-3, and F-10 offering registrations, for the dilution flags;
     - amendments (/A) of each.
   - **Triage before extraction.** The cheap triage role screens out 6-Ks that are not catalyst news, such as buyback notices, before the extractor reads them. That keeps spending inside the caps, and the 10-requests-per-second EDGAR limit holds as before.
   - **Time stamps.** A 6-K often arrives hours after the home-market announcement, and overnight in New York. Each record keeps the release's own date and time as day 0, as well as the SEC acceptance time.
   - **Scoring.** The extractor and verifier get recorded 6-K fixtures and gold-set entries. This is a source change, so both prompts are scored again.
   - **Matching.** The entity resolver handles depositary-share tickers and home-market names.
   - **Fiscal years.** Dates follow each company's own fiscal year, with its naming rule stored per company:
     - Takeda's "FY2025" runs from April 2025 to March 2026;
     - a UK "FY26" usually means the year ending in 2026.

     EDGAR's fiscal-year-end field is empty for some issuers (GSK, Takeda), so it is read from the 20-F.
   - **Market value.** It needs the ratio of depositary shares to home shares.
2. **Stage 2, in a new Phase 5: companies listed only outside the US, one market at a time.**
   - **Order.** Markets go in order of how many listed drug developers each has. A company already matched to an SEC number, through its ISIN or LEI, is not added again.
   - **Before any code for a market:**
     - its announcement source's robots.txt and terms are checked;
     - a rate limit is set;
     - any account or key it needs is found, and keys are gathered for the owner in one sitting.

     A market whose terms forbid automated collection is left out. The alternative is a licensed feed, and only with the owner's approval of the cost.
   - **Other languages:**
     - The quote stays in the original language, character for character, so the quote check still works.
     - Text is normalized first, for example full-width digits, so the match is exact without being fragile.
     - A machine translation is shown beside the quote and labeled "machine translation". It is never a claim, never quoted as the source, and never checked as one.
     - The date parser learns each language's date phrases, including Japan's era years, with property-based tests.
     - The gold set gains entries in each language before that market publishes.
   - **Registries and regulators.** Each is checked the same way before it is built:
     - trial registries: the EU's CTIS and the older EudraCT, ISRCTN, Japan's jRCT, China's ChiCTR, Australia and New Zealand's ANZCTR, and Korea's CRIS, with the WHO's ICTRP as an index;
     - regulators: EMA, MHRA, PMDA, NMPA, Health Canada, TGA, Swissmedic, Korea's MFDS, and Israel's Ministry of Health.
   - **Identifiers.** Companies also carry ISIN and LEI codes, and names in other scripts.
   - **Its own checkpoint per market (CP9).** A separate random sample of that market's records must meet the Gate 2 error rate before the market publishes. Errors in a new language cannot hide inside the overall sample.
   - **Prices.** The planned price data covers US listings. Until a license covers other exchanges and currencies, companies listed only abroad show no market value, price statistics, or scenario price ranges.
3. **Every rule stays the same.**
   - Every value has a source document and a verbatim quote.
   - Agents that read web text have no tools.
   - Only the publish gate writes live data.
   - No advice.

## Alternatives

- **All foreign markets at once.** That would mean many new sources, terms checks, and languages in one step, with the gold set unable to keep up. Errors would rise above the Gate 2 limit.
- **Only foreign companies listed in the US.** This is cheaper, but it leaves out companies that many investors follow on their home exchange. The owner asked for public foreign companies in general.
- **A paid global news feed instead of each market's own source.** It would be quicker to build, but it costs money every month, may limit how data is shown, and is a secondary source rather than the company's own filing. It stays an option, chosen per market, with the owner's approval.

## Consequences

- Stage 1 closes a gap in today's coverage at no extra cost, and mostly reuses the existing collectors.
- Stage 2 is the larger build. Each market adds a terms check, possibly an owner setup step, gold-set work, and its own checkpoint before it publishes.
- The country and regulator filters already listed in the spec start to matter.
