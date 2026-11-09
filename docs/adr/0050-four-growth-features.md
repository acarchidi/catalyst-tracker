# 0050 — Four growth features: embeddable calendar, correction requests, a professional tier, and referral credit

- **Status:** accepted (the owner chose all four on 2026-11-09); prices and start dates stay with the owner
- **Date:** 2026-11-09
- **Roadmap item:** new items in Phases 2, 3, and 10 (owner decision 2026-11-09; proposed in ADR 0047, question 6c)

## Context

ADR 0047 proposed four features that bring readers or raise revenue per reader. The owner approved all four. This record says what each one is, its rules, what it needs first, and how it is tested. None is built here.

## Decision

### 1. Embeddable calendar for newsletter writers (Phase 2, after the public site)

- **What:** a small read-only calendar a writer can place on their own page. It shows the free 30-day calendar, optionally narrowed to a list of tickers or one catalyst type. Each entry shows the company, the date as written, the date label, and a link to our page for that catalyst, where the quote and source are.
- **Why:** the 26 newsletter writers on the prospect list need a calendar and do not have a sourced one. Each embed is a standing link back to us.
- **Forms:** a web frame served from our site, and, for services that do not allow frames, a weekly block of plain HTML the writer pastes. Whether Substack's editor accepts a frame was not confirmed; it is tested before this is offered to Substack writers.
- **Rules:**
  - free shape only, built from the same code path as the free API, so it cannot carry a paid field;
  - the notice and a "Data from" link back are part of the widget and cannot be removed by a setting;
  - no cookies and no reader tracking; plain code counts loads per embedding site, as totals only;
  - cached and rate-limited per site; no price data; WCAG 2.2 AA.
- **Needs:** the public site and hosting. **Checkpoint:** CP5 (site preview). **Tests:** the widget's records equal the free API's for the same filter; hostile text in a record cannot add a tag or link (the digest's tests, reused).

### 2. Correction requests (Phase 2, after the public site)

- **What:** a form on every public record: "Report an error". The sender gives the record, what is wrong, and a link to a primary document that shows it. Anyone may use it, including the company's investor-relations staff.
- **How a request is handled:**
  1. Plain code checks it: an https link, a length limit, a rate limit per sender, a spam trap. Emails and phone numbers are removed from the free text.
  2. Plain-code collectors fetch the linked document and store a copy with its hash, as for every source.
  3. The document goes through the normal path: extractor, verification, publish gate. The request's own words never reach an agent and are never a source. Only the fetched document can support a value.
  4. A change that results is recorded as a correction, alerts watchers, and appears on the corrections page (spec: Corrections).
  5. The owner sees the request's text only in the private review queue, escaped.
- **Rules:** no record is ever edited by hand. No one can pay to change, add, or remove a record. A news article is a lead, never a source (spec: Provenance). The sender gets a page showing the request's status; no email is sent until our own sender exists.
- **Needs:** the public site, a `correction_request` table (reversible migration), and the corrections writer. **Checkpoint:** CP4's rules apply again (Opus review; injected text in a request cannot change a stored value). **Tests first:** a request with hidden instructions changes nothing; a request without a primary link is refused; a request cannot publish by itself.

### 3. Professional tier (Phase 10, with the public REST API)

- **What:** a higher-priced plan for funds, analysts, and business-development teams. It contains what the spec already plans to charge more for: the REST API, the full MCP tools at higher limits, the daily file of changes, webhooks, and CSV export, plus three seats on one bill.
- **Price:** the owner decides, together with ADR 0029's possible higher tier and ADR 0030's API tier ("about $50 a month until the owner decides"). These three are one decision: one professional plan, not three.
- **Rules:** the same data and the same rules as every other tier. No price series, no advice, the notice on every response. Limits and metering by plan, as ADR 0030 says.
- **Until it ships:** nothing is sold under this name. Substack's optional "founding member" plan may be switched on by the owner as support for the project, described truthfully, with no promise of features that do not exist yet.
- **Needs:** accounts, billing, API keys, and metering (Phases 3 and 10). **Checkpoint:** CP6 (billing).

### 4. Referral credit (now on Substack; later with accounts)

- **Now:** Substack has its own referral program, which gives a subscriber free paid months for bringing in new subscribers. The owner may switch it on in Substack's settings. No code is needed. Its current terms are read on Substack's own page before it is switched on; they were not confirmed in this research.
- **Later, with our own accounts (Phase 3):** a paid subscriber gets a personal link. When someone new subscribes through it and stays paid for 30 days, the referrer gets one free month.
  - Credit is free months only, never cash, at most 12 months a year per subscriber.
  - No credit for referring oneself: the same card, email, or account never counts.
  - Credit is applied through Stripe as a coupon by plain code, from Stripe's own records. A test checks that a refund or a cancellation inside 30 days gives no credit, and that replaying a webhook gives it only once.
  - The page that explains the program carries the notice and makes no claim about returns.
- **Needs:** accounts and Stripe (Phase 3). **Checkpoint:** CP6 (billing).

## Alternatives

- **Paid placement or sponsored records** as a revenue line. Refused: it would end the claim that every record stands only on its source.
- **Letting companies edit their own records.** Refused for the same reason; correction requests give them a path that keeps the source rule.
- **Cash referral rewards.** More attractive, but they invite fraud and add tax paperwork. Free months cost nothing extra to serve.
- **Selling the professional tier now on Substack.** There is nothing to deliver yet. Selling it would be a promise the product cannot keep.

## Consequences

- Three of the four features wait for the public site or accounts. Only the Substack referral program can start now, and it is the owner's switch.
- Correction requests add a new way for outside text to arrive, so they get the publish gate's adversarial tests.
- The professional tier settles three open price questions as one. The owner still sets the number.
- Each feature adds lines to the spec's Offering table and a checkpoint. Those are owner-only files, so they change in their own pull request.
