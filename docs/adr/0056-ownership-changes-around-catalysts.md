# 0056 — Ownership changes around catalysts: all insider transactions, large stakes, and fund holdings, summarized first

- **Status:** accepted
- **Date:** 2026-11-23
- **Roadmap item:** Phase 7, insider purchases before catalysts
- **Amends:** ADR 0026, decision 2 (what is shown); adds Schedule 13D and 13G and Form 13F as sources

## Context

ADR 0026 plans insider purchases (Form 4, code P) in the 90 days before each catalyst, with sales beside them. On 2026-11-23 the owner asked four further things:

- whether stock grants and awards are included (ADR 0026 stores every transaction code but shows only purchases and sales);
- trades after a catalyst as well as before it;
- data from the "13-series" filings "where possible", so new large stakes are shown;
- that these features do "not bombard the user with too much information, but that they can be viewed".

The 13-series filings differ in timing and reach:

- **Schedule 13D and 13G** report a holder crossing 5% of a company's shares, and later changes. They are filed within days, so they can be placed on a timeline. 13D is the active form; 13G is the passive one.
- **Form 13F** reports each large manager's long holdings at quarter end, filed up to 45 days later. It shows holdings on one date, not when a fund bought.

## Decision

1. **Every Form 4 transaction is shown, grouped.** Grants and awards (code A), option exercises and conversions (M, C, X), shares withheld for tax (F), gifts (G) and other codes are shown as well as purchases (P) and sales (S). They sit in their own group, "Grants, exercises and other", because they are compensation or paperwork, not trades in the market. Each keeps its code's plain-English name and its filing link.
2. **Before and after each catalyst.** The lookback stays as ADR 0026 sets it: 90 days before the window opens, or since the previous catalyst if shorter. A look-ahead of 30 days after the window closes, or until the next catalyst if sooner, is added (agent's proposal for the length).
3. **Large stakes from Schedule 13D and 13G.** Plain code reads each filing on a covered company: the holder's name, the percent and shares reported, the event date, whether it is 13D or 13G, and the filing link, each as a claim quoted from the filing. Shown in the same before-and-after periods as "New or changed large stakes". The form name is shown as a fact; no label such as "activist".
4. **Fund holdings from Form 13F, as quarter-end facts.** Plain code reads the SEC's quarterly 13F data sets for covered companies only. Shown as "Fund holdings at quarter end": the number of reporting managers and the change in total shares from the previous quarter, with the quarter-end date always beside it. It is never placed as if it were a trade date. Built after decisions 1 to 3.
5. **Summary first, details on request.** Each catalyst shows one line by default, for example "Insiders: 2 purchases ($310,000), 1 sale · 1 new 5% holder". The line opens to the groups in this order: purchases and sales; large stakes; grants, exercises and other; fund holdings. A group with nothing in the period is left out. The newsletter carries the one line only, with a link to the details once the site exists.
6. **Unchanged from ADR 0026:** plain code with no model; every value is a claim with a source and a quote, published only through the publish gate; facts with no signal labels or buy and sell language; the "not financial advice" notice everywhere.

## Alternatives

- **Purchases and sales only (ADR 0026 as written).** Hides grants, which the owner asked about, and makes a large sale after an exercise hard to read.
- **Leave out Form 13F.** Simpler, but the owner asked for 13-series data where possible. Shown with its quarter-end date, it adds context without suggesting timing it cannot show.
- **Show every row by default.** The owner asked not to overwhelm the reader.

## Consequences

- More SEC requests per company: Form 4, 13D and 13G are all read through EDGAR within the 10-requests-per-second limit. The 13F data sets are one quarterly download each.
- No model cost: every value is a field in a filing.
- Phase 7 stays where it is on the roadmap unless the owner moves it.
