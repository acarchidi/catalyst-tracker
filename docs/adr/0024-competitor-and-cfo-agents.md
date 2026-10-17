# 0024 — Two business agents at the end of the roadmap: competitor research and a CFO

- **Status:** accepted (to be built after v1; nothing is built now)
- **Date:** 2026-10-17
- **Roadmap item:** Phase 6, "Business agents"

## Context

On 2026-10-17 the owner asked to plan two more agents for the end of the roadmap:
- **Competitor research:** who the competitors are, what they are doing, whether new ones have appeared, and ideas for valuable new features.
- **Financial management (a CFO agent).**

The owner chose:
- a weekly competitor report with feature ideas;
- a CFO agent that reads and drafts proposals, and acts on nothing itself;
- no bank connection: revenue comes from Stripe, and costs from our own logs and the owner's monthly entries.

Two rules from the spec still apply:
- Agents that read web text have no tools and no database access.
- Model calls go through `ModelClient`, with a daily cap per role.

## Decision

### Competitor research agent (weekly)

1. **Sources are plain-code collectors.** A versioned list (`config/competitors.toml`) names each competitor's public pages:
   - pricing;
   - features or changelog;
   - blog and release notes;
   - app-store listing;
   - API or connector docs.

   The spec's competitor research is the starting list. The collectors follow robots.txt and site terms, and are rate-limited. They store a dated copy of each page, as the snapshot store does for sources.
2. **New entrants come from public feeds,** for example launch sites' RSS feeds and news searches by keyword, read by plain code.
   - A web-search API would find more, but it needs an account and a key. That is the owner's decision when this is built.
   - A possible newcomer is only a candidate until the owner adds it to the list.
3. **Changes, not pages.** Plain code compares each page with last week's copy. The agent, which has no tools, reads only the changed text and writes the report. Every claim in the report quotes the changed text and links the page, as records do.
4. **Feature ideas** are a ranked list. Each idea gives:
   - what it is;
   - who it helps;
   - which competitor does it (if any), with a link;
   - a rough size (small, medium, or large);
   - a short "why it fits".

   An idea becomes a roadmap item only when the owner approves it. A coding session then adds it through a pull request.
5. **Delivery** is the same as alerts (ADR 0023): one GitHub issue a week, labelled `competitor-report`. The owner needs no new account.
6. **Cost:**
   - It gets its own role, `researcher`, with a daily cap in `config/models.toml`, which the owner merges.
   - It runs once a week. It reads the week's changed text, typically a few thousand words.
   - Expected cost is a few dollars a month at Sonnet prices. This is an estimate, to be measured on the first runs.

### CFO agent (monthly, read-only, drafts)

1. **Inputs, read-only:**
   - **revenue from Stripe:** subscribers, MRR (monthly recurring revenue), new and cancelled subscriptions, refunds, and fees. This uses a **restricted read-only Stripe key**, made in the same sitting as the business's Stripe setup;
   - **AI spend** from our own `model_call` table, by role;
   - **other costs** from a versioned file the owner fills once a month (`config/costs.toml`): hosting, domain, data licence, email service, and so on;
   - **cash on hand,** entered by the owner monthly in that file, because there is no bank connection.
2. **Plain code computes every number:**
   - revenue;
   - costs by kind;
   - profit or loss;
   - cost per subscriber;
   - Stripe fees;
   - churn;
   - months of runway at the current rate;
   - budget against actual.

   The model only writes the plain-English explanation. It may not change a number, and every figure in its text must match the computed table.
3. **Drafts, never actions.** It may propose:
   - a spending-cap change;
   - cancelling a service nobody uses;
   - a price or annual-plan change;
   - a cost to watch.

   Each proposal is one GitHub issue for the owner. It cannot pay, refund, change prices, change caps, or move money. Any change happens only through the owner, or through a pull request the owner merges.
4. **Alerts** use the same issue channel, when spend runs above budget or a cost appears that is not in the file.
5. **Not tax or legal advice.** It can remind the owner of dates the owner records, such as filing dates. It does not decide tax questions.
6. **Cost:** its own role, `cfo`, with a small cap. One run a month on summary numbers costs cents.

## Alternatives

- **Competitor research by hand, or on request only.** It is cheaper, but newcomers get missed, and the owner asked for constant research.
- **A CFO with a bank feed.** It would give a full cash picture, but it needs a new account and bank-level security. The owner declined it.
- **A CFO that can act within limits.** Faster, but money decisions stay with the owner. The orchestrator (ADR 0019) already pauses runaway AI spend.

## Consequences

- Both build on pieces from earlier phases: alerts (issues), collectors and snapshots, `ModelClient` caps, and Stripe from Phase 3.
- Setup the owner will need at build time:
  - the restricted read-only Stripe key, which could be made with the business's Stripe setup to save a sitting;
  - optionally, a web-search API key for finding new entrants;
  - a monthly 5-minute update of `config/costs.toml`.
- New roles need caps in `config/models.toml`. The owner merges those.
