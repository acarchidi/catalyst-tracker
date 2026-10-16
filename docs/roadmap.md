# Roadmap

The build runs in five phases. Each phase ends at a gate, and the next phase does not start until the owner approves the gate. Week counts are estimates; the owner's review time sets the real pace.

Checkpoints inside each phase (where work stops for review) are listed in [quality-gates.md](quality-gates.md).

Claude Code: tick items (`[x]`) in the same pull request that finishes them.

## Phase 0 — Foundations (weeks 1–3)

- [x] Competitor research and spec
- [ ] Repository skeleton: `AGENTS.md` conventions applied, CI pipeline, pre-commit hooks, decision-record template; then turn on branch protection for main requiring CI (via `gh api`; if refused, give the owner the click steps)
- [x] Data schemas for every entity (Pydantic) with generated data dictionary and reversible migrations
- [ ] Model interface (`ModelClient`), first provider adapter (Anthropic), contract tests, per-role spend caps
- [ ] Collectors: SEC EDGAR, ClinicalTrials.gov, wire RSS, openFDA, Federal Register (recorded fixtures, idempotent)
- [ ] Source snapshot store with SHA-256 hashes
- [ ] Coverage list from SEC industry codes 2834, 2836, 8731 (therapeutics developers only)

**Gate 1:** 50 companies flow from source to the live database, every record with a working deep source link.

## Phase 1 — Core data (weeks 4–8)

- [ ] Extractor agents and prompt evaluation sets
- [ ] Entity resolver and reference tables
- [ ] Verification checks and publish gate
- [ ] Review-queue screen
- [ ] Change history and outcome agent
- [ ] Auditor agent, weekly link check, daily status report to the owner
- [ ] Gold set of 200 verified catalysts

**Gate 2 (the one that matters most):** weekly random sample under 2% errors; 100% of published records carry a deep source link.

## Phase 2 — Free launch (weeks 9–12)

- [ ] Public site, catalyst calendar, public pages
- [ ] Basic search and filters
- [ ] Read-only MCP server and directory listings
- [ ] Weekly email and marketing-agent drafts (owner approves each)
- [ ] Business setup: entity, terms, privacy policy, Stripe, sales tax (owner)

**Gate 3:** business set up (entity, terms, Stripe, sales tax).

## Phase 3 — Paid tier, $15/month (weeks 13–16)

- [ ] Accounts and $15 subscription
- [ ] Watchlists, alerts (email, push, Slack), interest feeds
- [ ] All filters, saved searches, full history, CSV export
- [ ] Full MCP tools and research assistant
- [ ] Buy Tiingo Commercial ($50); get written confirmation that derived statistics may be shown, before any price-impact number publishes (owner)
- [ ] Price history backfill on Tiingo Commercial for internal backtests
- [ ] Price-impact statistics; cash runway and dilution flags

**Gate 4:** alerts reliable for 4 straight weeks.

## Phase 4 — Analysis (weeks 17–24)

- [ ] Readout benchmarking, oncology first
- [ ] Market-size estimates

## Later (unscheduled)

- [ ] Public API tier (about $50/month)
- [ ] Second model provider for verification
- [ ] EMA, PMDA, NMPA sources; conference abstracts at scale; Slack and Teams apps

## Decision log

Newest first. Every ADR in `docs/adr` adds a line here.

| Date | Decision |
| --- | --- |
| 2026-10-16 | ADR 0003: tables generated from Pydantic models; controlled lists as checked text; catalyst current values plus append-only change history; rules enforced in Pydantic and the database |
| 2026-10-16 | ADR 0002: walking skeleton stores an 8-K's press-release exhibit; one row per URL and hash; local copies until the snapshot store; 8 SEC requests per second |
| 2026-10-16 | ADR 0001: stack and layout; one `pyproject.toml` at the root, package in `pipeline/catalyst`, migrations checked up-down-up in CI |
| 2026-10-16 | Setup steps that need the owner are tracked in README; `scripts/check_setup.py` reports what is missing at every session start |
| 2026-10-14 | Scheduled jobs run in GitHub Actions in v1 instead of Prefect plus Railway; revisit when jobs outgrow it |
| 2026-10-14 | One paid tier at $15/month; API tier (about $50/month) on the roadmap |
| 2026-10-14 | Ingestion twice each weekday: pre-market and post-close |
| 2026-10-14 | Verification uses independent cross-checks; model providers plug in through one interface |
| 2026-10-14 | Price data: no free source is licensed; Tiingo Commercial bought at the Paid tier phase; display license to be confirmed |
| 2026-10-14 | Openings adopted: source links, date history, benchmarking, market sizing, price-impact statistics |
| 2026-10-13 | Retail investors and small institutions first; never personalized advice |
| 2026-10-13 | Business setup and distribution plan deferred |
