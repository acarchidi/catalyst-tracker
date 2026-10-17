# Roadmap

The build runs in five phases. Each phase ends at a gate, and the next phase does not start until the owner approves the gate. Week counts are estimates; the owner's review time sets the real pace.

Checkpoints inside each phase (where work stops for review) are listed in [quality-gates.md](quality-gates.md).

Claude Code: tick items (`[x]`) in the same pull request that finishes them.

## Phase 0 — Foundations (weeks 1–3)

- [x] Competitor research and spec
- [x] Repository skeleton: `AGENTS.md` conventions applied, CI pipeline, pre-commit hooks, decision-record template; then turn on branch protection for main requiring CI (via `gh api`; if refused, give the owner the click steps). GitHub's free plan cannot protect a private repository; the merge robot requires CI and the review for its merges instead (owner decision, ADR 0007)
- [x] Data schemas for every entity (Pydantic) with generated data dictionary and reversible migrations
- [x] Model interface (`ModelClient`), first provider adapter (Anthropic), contract tests, per-role spend caps
- [x] Collectors: SEC EDGAR, ClinicalTrials.gov, wire RSS, openFDA, Federal Register (recorded fixtures, idempotent)
- [x] Source snapshot store with SHA-256 hashes
- [x] Coverage list from SEC industry codes 2834, 2836, 8731 (therapeutics developers only)

**Gate 1:** 50 companies flow from source to the live database, every record with a working deep source link.

## Phase 1 — Core data (weeks 4–8)

- [x] Extractor agents and prompt evaluation sets
- [ ] Entity resolver and reference tables. Includes trials of several drugs: every drug and combination is listed, each from a quoted source sentence, marked as tested alone or in combination (owner ruling, 2026-10-17; `evals/extract_catalysts/README.md`, rule 3)
- [ ] Verification checks and publish gate
- [ ] Review-queue screen
- [ ] Change history and outcome agent
- [ ] Auditor agent, weekly link check, daily status report to the owner
- [ ] Gold set of 200 verified catalysts. Includes giving the Spyre SKYLINE Part B gold entry its full drug list once the drug field is a list (owner ruling, 2026-10-17, rule 3)

**Gate 2 (the one that matters most):** error rate under 1% at 95% confidence on a random sample of published records, checked independently (owner target 2026-10-17: 99% to start, 99.9% later); 100% of published records carry a deep source link.

## Phase 2 — Free launch (weeks 9–12)

- [ ] Public site, catalyst calendar, public pages
- [ ] Basic search and filters. A search for one drug finds the trials of combinations that contain it and shows that drug first; a long drug list expands on request and never takes over the page (owner ruling, 2026-10-17)
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
- [ ] Estimated results-reporting window for each trial (owner request, 2026-10-17). A trial's primary completion date is when the last measurement is due, not when results come out. Show a second window for when results are likely to be reported, chosen in this order:
  1. The company's own guidance ("topline data in 1H27"), whenever it exists.
  2. Otherwise, the company's own history: for its past trials, the gap between primary completion (ClinicalTrials.gov) and the press release or filing that reported the results.
  3. Otherwise, a base rate for similar trials (phase, and later therapeutic area), for companies with no reporting history yet.

  The estimate carries the "inferred" date label and a tooltip saying which of the three it used, how many past trials it rests on, and the spread of their gaps. Guidance always replaces an estimate. Known limits to state in the tooltip: completion dates in the registry are often revised, and companies may take longer to report bad results, so historical gaps can run short.

## Decision log

Newest first. Every ADR in `docs/adr` adds a line here.

| Date | Decision |
| --- | --- |
| 2026-10-17 | Gate 2 raised to an error rate under 1% at 95% confidence on a random sample of published records (owner target: 99% to start, 99.9% later); the extractor's score floor (`baseline.json`) and its retired exemption (`unscored.json`) are merged by the owner only |
| 2026-10-17 | ADR 0015: the entity resolver matches companies, drugs, and trials by exact rules in plain code (SEC number, ticker on the day, name; company-scoped code names; registry number, study name); several candidates are ambiguous and go to review; no fuzzy matching |
| 2026-10-17 | Gold-set counting rules from the owner: two trials or two diseases in one sentence are separate events; a multi-drug trial names every drug and combination, searchable by each; data with no named meeting is a readout; the dated event is the data, not a later approval |
| 2026-10-17 | Roadmap: added an estimated results-reporting window per trial (company guidance first, then the company's own completion-to-results history, then a base rate by phase), with a tooltip naming its basis (owner request) |
| 2026-10-17 | ADR 0014: the extractor's first evaluation set is 12 recorded press releases with 20 agent-labeled events (owner confirmation pending); scores are earned in GitHub Actions with the product key and committed; a CI test blocks an unscored prompt change, a score earned on another prompt, model or gold set, and any drop below the baseline |
| 2026-10-17 | ADR 0013: the extractor proposes catalysts with a date phrase and a quote; code checks the quote, reads the window, and labels PDUFA and advisory dates regulator-scheduled; results are stored as claims, never catalysts; documents already extracted with the same prompt version are skipped |
| 2026-10-17 | ADR 0012: quotes are found in a versioned plain text of each stored copy, word for word with only spacing free; date phrases become windows in code ("mid-2027" is April to September; "by" runs from the document's date); vague phrases get no window |
| 2026-10-17 | ADR 0011: live jobs run in GitHub Actions with settings from Actions secrets; the live database is reached through Supabase's Session pooler; row-level security on every table; owner setup kept to one sitting at a time |
| 2026-10-16 | ADR 0010: Gate 1 uses the 10 pilot companies plus 40 drawn with seed 2026; 10 records sampled with the same seed; a link works only on HTTP 200 at its exact address; link checks obey robots.txt and source rate limits |
| 2026-10-16 | ADR 0009: coverage = US-listed companies under SIC 2834, 2836, 8731 that are the industry lead sponsor of at least one interventional drug or biological trial under their own name; exact normalized name match; hand overrides with notes; review list for the rest |
| 2026-10-16 | ADR 0008: copies keyed by their SHA-256 and re-checked on every read; local disk or a private Supabase Storage bucket, chosen explicitly; `catalyst snapshots-verify` audits every copy; development copies not migrated |
| 2026-10-16 | ADR 0007: a merge robot merges `claude/` pull requests once CI and the Claude review pass; work no longer stops at checkpoints; the owner merges only changes to the robot or the review (owner decision) |
| 2026-10-16 | Owner stays on GitHub's free plan for now: main is not protected, auto-merge stays off, and the owner merges every pull request. The skeleton item's branch-protection step waits for GitHub Pro |
| 2026-10-16 | ADR 0006: a fresh Claude reviews every ready pull request as a required check; `claude/` pull requests merge themselves after CI and that review pass; CODEOWNERS paths wait for the owner; main protected by an imported ruleset (owner decision) |
| 2026-10-16 | ADR 0005: documents keyed by each source's own identity; exact-name company links; per-host rate limits; ClinicalTrials.gov API used despite its robots.txt (owner decision); only PR Newswire among wires is reachable |
| 2026-10-16 | ADR 0004: model roles, prices, and daily caps in config/models.toml; every call logged in model_call; no refusal fallback to another model; JSON-schema output validated client-side |
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
