# 0032 — Research reports from our own catalyst and price-reaction data

- **Status:** accepted (planned; nothing is built now)
- **Date:** 2026-10-18
- **Roadmap item:** Phase 11, all items

## Context

On 2026-10-18 the owner asked for a last roadmap item. Once we hold a deep set of catalysts and their price reactions, we would write analysis and research reports from it. Then we would either sell the reports or let paid subscribers read them.

**What we will have by then:**
- every resolved catalyst with its outcome and source;
- its date history (slips);
- its measured move around the event (Phase 3 price statistics; Phase 10 past-catalysts page);
- corporate and safety events, including peer moves (ADR 0030);
- insider purchases (ADR 0026);
- devices (ADR 0027);
- the scenario accuracy record (ADR 0017).

**What limits it:**
- The price-data license. The spec allows event-window statistics, not price series, and plans written confirmation from the vendor (spec: Open questions, Price data). Selling a report is a new use of derived numbers, so the license must cover it too.
- The spec bans unbacked predictions. Any predictive feature must publish its accuracy against a naive baseline.
- The spec bans personal advice. Content must stay impersonal and regularly published, not timed to market events (spec: Compliance).

## Decision

1. **Start only when the data is deep enough.** A report topic is allowed once its sample has at least 200 resolved events, with at least 30 in each group the report compares. The thresholds live in config. Until then the topic stays on a list.
2. **Topics are descriptive questions with base rates.** Examples:
   - how often a guided readout window slips, and by how much, by phase and company size;
   - the price reaction to advisory committee votes, PDUFA outcomes, and complete response letters, with day-0 and day-5 abnormal returns against XBI;
   - whether company-confirmed dates hold better than registry estimates;
   - how competitors' shares move on a peer's safety event;
   - insider purchases before catalysts against outcomes;
   - how well our own scenarios did.
3. **Plain code computes every number; a model only writes the words.**
   - Each report has a fixed analysis script: counts, medians, percentiles, and confidence intervals, with sample sizes shown.
   - The analyst role drafts the text from that table.
   - A test checks every number in the text against the computed table, as for the CFO agent (ADR 0024).
   - Each report includes a methods section, the data's date range, what was left out and why, and the naive baseline for any forward-looking figure.
   - It also includes the notice "Research, not financial advice". No buy or sell language, no single price target, nothing about any reader's holdings.
4. **Every report is checked before it is released.**
   - An Opus review checks the methods and statistics (new checkpoint CP14).
   - The owner approves each report before it reaches anyone, because releasing it publishes outside the project.
   - Reports come out on a fixed schedule, for example quarterly, never in reaction to a price move.
5. **Two ways to offer them; the owner chooses when Phase 11 starts:**
   - **(a) Included in the paid tier**, or a higher tier (ADR 0029). This is simplest: no new checkout.
   - **(b) Sold one at a time** through Stripe Checkout, as a one-time purchase per report.
   - Both can coexist: subscribers read them, non-subscribers buy them.
   - A free summary page for each report shows its question, method, and one headline figure, with every source link. Source links are never paywalled (spec: Offering).
6. **The license comes first.** Before any report that uses price-derived figures is released, the price vendor confirms in writing that such reports may be shown and sold. Reports that use no price data (slip rates, date reliability) can go first.

## Alternatives

- **Model-written analysis with model-computed numbers.** Rejected. A model can state a number the data does not support, and no test could catch it.
- **Publishing as soon as the first events resolve.** Rejected. Small samples give confident-looking numbers that later reverse, which would cost the trust the product sells.
- **Selling the raw dataset instead of reports.** Not now. The price license forbids raw series, and the API tier (ADR 0030) already offers the published data.

## Consequences

- Phase 11 needs the outcome history, the price license, and the Phase 3 billing. It is the last phase, because it needs the most data.
- One new checkpoint (CP14) and one owner approval per report.
- Costs: analyst model spend per report under its cap, plus Stripe fees on any sale. The analysis itself is plain code.
