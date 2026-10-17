# 0026 — Insider purchases before known catalysts, from SEC ownership filings

- **Status:** accepted (to be built at the end of the roadmap; nothing is built now)
- **Date:** 2026-10-17
- **Roadmap item:** Phase 7, "Insider purchases before catalysts"

## Context

On 2026-10-17 the owner asked for a feature that shows each company's insider purchases ahead of its known catalysts, taken from filings.

**Where the data comes from:**
- Company insiders (directors, officers, and holders of more than 10%) report changes in their holdings on SEC Form 4.
- Form 4 is due within two business days of the transaction.
- Transaction code P marks an open-market or private purchase ([SEC Form 4 instructions](https://www.sec.gov/files/form4.pdf); [ownership form codes](https://www.sec.gov/edgar/searchedgar/ownershipformcodes.html)).
- Form 4 filings are machine-readable XML on EDGAR.

**Foreign companies listed in the US:**
- Their insiders used to be exempt.
- Under the Holding Foreign Insiders Accountable Act, their directors and officers must file Section 16 reports from March 18, 2026, and the SEC amended its rules and forms to match ([fact sheet](https://www.sec.gov/files/34-104903-fact-sheet.pdf)).
- Holders of more than 10% of a foreign company are still excluded.
- The SEC also issued an exemption order for certain directors and officers of foreign companies ([Release 34-104931](https://www.sec.gov/files/rules/exorders/2026/34-104931.pdf)). Its scope is checked when this is built.

## Decision

1. **Plain code only.**
   - The SEC collector reads Form 4 and 4/A XML for every covered company, under the same 10-requests-per-second EDGAR limit.
   - It stores each transaction with:
     - the insider's name and role (director, officer, or 10% holder);
     - the transaction date and code;
     - shares, price, value, and holdings after the trade;
     - whether the filing marks it as made under a pre-set trading plan (Rule 10b5-1), where the form records that;
     - the link to the filing.
   - No model is needed. Every value is a field in the filing.
2. **What is shown.** For each upcoming catalyst, the insider purchases (code P) made in a lookback period before the catalyst's window opens: by default 90 days, or since the company's previous catalyst if that is shorter. Shown with each one:
   - its filing link;
   - the number of insiders buying;
   - the total value.

   Sales are shown beside the purchases for context, with pre-set-plan trades marked as such.
3. **Facts, never signals.**
   - No labels such as "bullish" and no buy or sell language.
   - Every page and API response carries the "nothing is financial advice" notice (spec: Legal and compliance).
   - The feature is impersonal data with sources, the same for every viewer.
4. **The historical base rate comes later, in plain code:** how often catalysts with insider buying before them turned out positive. It needs the outcome archive (Phase 1, "Change history and outcome agent"). It is shown with its sample size and labelled descriptive, not predictive.
5. **Foreign companies.** Directors' and officers' Form 4s are included from March 18, 2026, with no record before that date, and the page says so. Their 10% holders are not covered.

## Alternatives

- **Buying score or "smart money" label.** Easy to read, but it sounds like a recommendation, and the spec rules out advice.
- **A paid insider-data feed.** Quicker to build, but it costs money and is not the primary filing. EDGAR is free.
- **Model-based extraction from filing text.** Not needed, because the XML already holds every field.

## Consequences

- No new account or key: it is EDGAR, through the existing collector.
- The data is cheap to store and refresh after each twice-daily collection.
- It needs the catalyst windows (Phase 1) and, for the base rate, the outcomes archive.
