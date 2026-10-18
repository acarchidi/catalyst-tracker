# 0030 — Corporate events, deal dates, past catalysts with their price moves, and the full API

- **Status:** accepted (planned; nothing is built now)
- **Date:** 2026-10-18
- **Roadmap items:** Phase 10, all items

## Context

On 2026-10-18 the owner asked for five things:
1. A view of past catalysts with their actual effect on the share price.
2. An API that offers all of the data.
3. In the history of events: departures and arrivals of top executives, major fundraises, and collaboration or deal announcements.
4. On the upcoming schedule: future merger, deal, and collaboration dates.
5. All of it added to the end of the roadmap.

**What the spec already has:**
- Historical outcomes: free sees the last 90 days, paid sees the full archive. The outcome agent is a Phase 1 item.
- Price-impact statistics per event, per company, and as base rates (Phase 3), measured against the XBI biotech index.
- One API under everything: the web app, the MCP server, and the research assistant all use it. A public REST API tier at about $50 a month is listed under "Later (unscheduled)".
- Catalyst types that include offerings and shelf filings, and M&A, but not executive changes or the dates inside a deal.

**What the filings give:**
- Companies must report a departure or appointment of a principal executive, financial, accounting, or operating officer, or of a director, on Form 8-K Item 5.02. They must also report a material definitive agreement on Item 1.01, and an unregistered sale of shares on Item 3.02. Most 8-K items are due within four business days ([SEC Form 8-K](https://www.sec.gov/files/form8-k.pdf)).
- Foreign companies listed in the US report such events on Form 6-K (ADR 0021).
- Registered offerings appear as 424B prospectuses. Private placements appear on 8-K Item 3.02 and in releases.
- Merger dates appear in the merger agreement, the proxy (DEFM14A) with its shareholder meeting date, a tender offer's Schedule TO with its expiration date, and releases about antitrust waiting periods. Each of these is an SEC filing or a company release.

## Decision

1. **Past catalysts page.** The spec's outcome archive and per-event price statistics become one view: each resolved catalyst with its outcome, its source, and its measured move.
   - **The move:** abnormal return on day 0, day +1, and day +5 against XBI, the 30-day run-up before it, and volume against its 20-day average.
   - **Same rules as the spec:** day 0 follows the release time; only event-window statistics are shown, never a price series; free sees 90 days.
   - It is built when the price license is in place (Phase 3) and needs no new data beyond the outcome agent.
2. **New event types, recorded in history and kept up to date:**
   - **Executive changes:** CEO, CFO, chief medical officer, chief scientific officer, chief operating officer, president, and board chair. Each one is an arrival, a departure, or a planned departure with its effective date. The source is 8-K Item 5.02 (or a 6-K) and the release. The reason is shown only when the filing states one.
   - **Major financings:** registered offerings, private placements (PIPEs), registered directs, at-the-market programmes (their size and what was sold), convertible and other debt, and royalty or synthetic-royalty deals. "Major" means at least 10% of market value, or at least $50 million where market value is not yet licensed. Both thresholds are set in config. The amount and price come from the filing.
   - **Collaborations and deals:** licences, co-development and co-commercialisation deals, options, and asset sales. Each has its partners, upfront payment, milestone total, royalty terms as disclosed, and whether a payment is contingent. Collaborators are linked to their records (ADR 0028). Undisclosed terms are shown as "terms not disclosed".
   - **Upcoming deal dates as catalysts:** a merger's expected closing window, the shareholder vote date, the tender offer's expiration date, the antitrust waiting period's end, and the outside date in the agreement. A collaboration also gets its expected effective date and any option-exercise deadline the filing states.
   - These are not trial or regulatory catalysts, so they appear in their own section and filter ("Corporate events"). They also feed the price statistics as their own event types.
   - **Both readers learn the new types** under new prompt versions, with gold-set entries for each type. Both are scored again before any of these publishes (checkpoint CP13).
   - **Back-fill:** history is filled for covered companies from their past filings, at the 10-requests-per-second EDGAR limit, inside the daily model caps. It runs over several weeks rather than raising any cap.
3. **The full API.** The public REST API tier moves from "Later" into this phase.
   - It offers every published dataset: catalysts, history, outcomes, corporate events, deal dates, trials, collaborators, benchmarks, market size, scenarios, price statistics, runway, insider purchases (Phase 7), and devices (Phase 8).
   - Each one comes with its sources, as on the site.
   - It keeps the spec's design: OpenAPI, versioned paths, cursor paging, ETags, and rate limits by tier.
   - Access needs an API key per account, rate limits by plan, and usage metering. Bulk export is at most a daily file of changes, never a price series.
   - **Price:** the spec's "about $50 a month" stands until the owner decides otherwise. Whether the API tier and ADR 0029's possible higher tier are one plan is decided together.
4. **Every rule stays the same.**
   - Every value has a source document and a verbatim quote.
   - The readers have no tools.
   - Only the publish gate writes live data.
   - No advice: an executive departure is shown as a fact, never as a signal.

## Alternatives

- **Buy corporate events from a data vendor.** Rejected for now. It costs money every month, and the 8-K items give the same events with a primary source we can link.
- **Mix corporate events into the main catalyst calendar.** Rejected as the default. Trial and FDA dates are the core product, and a busy week of executive news would bury them. Users can turn the section on in their calendar and watchlists.
- **Leave the API under "Later".** Rejected, because the owner asked for it.

## Consequences

- Coverage of SEC forms grows: 8-K Items 1.01, 3.02, and 5.02; 424B prospectuses; DEFM14A; and Schedule TO.
- The readers' prompts change, so the gold set and both scores are redone for the new types.
- The API tier needs the Phase 3 accounts and billing, and an API key system.
- The back-fill takes weeks at our caps. That is a trade-off for keeping spending low.
