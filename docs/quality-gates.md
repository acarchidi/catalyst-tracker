# Quality gates and checkpoints

Work does not stop at the checkpoints below (owner decision, 2026-10-16; ADR 0007): each one is reviewed, previewed to the owner, and passed by its pass criteria, and work continues. An objection from the owner, whenever it comes, becomes the next item. The owner does not read code. Each checkpoint therefore gives the owner something they can judge without reading code (a preview), and gives the code to a fresh reviewer that did not write it.

## What happens at every checkpoint

1. **Automated checks pass** in CI: lint, strict types, tests, coverage thresholds, evaluation gate, secret scan.
2. **Fresh-eyes review.** Run `/ultrareview` (or a reviewer subagent with no access to the writing session's reasoning) and fix what it finds. Checkpoints marked **Opus** are reviewed by a reviewer subagent on the Opus model.
3. **Preview for the owner.** A plain-English summary with no jargon, plus the artifact named in the table (sample records with clickable links, a screenshot, a preview URL).
4. **Post and continue.** Post the summary to the owner and continue with the next item. Pass criteria that need the owner's judgment (CAL1, CAL2, CAL3) or the owner's own action (Gate 3) are recorded as waiting on the owner; work that does not depend on them continues.

## Checkpoints

| ID | When | Owner preview | Review | Pass criteria |
| --- | --- | --- | --- | --- |
| CP0 | Start of every session | Output of `scripts/check_setup.py` and the steps for anything missing | None | Owner has seen the list |
| CP1 Walking skeleton | After the repository skeleton | One SEC filing fetched, stored, and written as one database row, with a clickable source link | **Opus** architecture review | CI green; one command runs it locally; README explains how |
| CP2 Schema | After data schemas, before collectors scale | Plain-English data dictionary: every table and field, one line each | **Opus** | Migrations upgrade, downgrade, and upgrade again in CI; every field has a description |
| CP3 Collectors | After all five collectors | Table of 10 companies' latest documents with links the owner clicks | Reviewer subagent | Recorded-fixture tests; re-running a collector creates no duplicates |
| Gate 1 | End of Phase 0 | 50 companies end to end; random 10 records for the owner to click | **Opus** + `/ultrareview` | Every record has a working deep link; coverage thresholds met |
| CAL1 Extractor calibration | First week of Phase 1 | Review sheet: 20 documents, extracted fields beside the source sentence | Reviewer subagent | Owner agrees on at least 18 of 20; prompts adjusted before scaling |
| CP4 Publish gate | Before anything auto-publishes | List of every check and what it blocks, in plain English | **Opus** + adversarial tests (prompt-injection fixtures) | No test in the gate can be skipped; injected text cannot change a stored value |
| Gate 2 | End of Phase 1 | Weekly sample results; error rate with its confidence bound | **Opus** + `/ultrareview` | Error rate under 1% at 95% confidence on a random sample of published records, each checked against its source independently of the pipeline: about 300 records with no errors, or about 475 with one; 100% deep links |
| CP5 Site preview | Before the free launch | Vercel preview URL to click through on phone and laptop | Accessibility and performance checks | WCAG 2.2 AA checks pass; pages load fast on mobile |
| Gate 3 | End of Phase 2 | Business setup confirmed by the owner | None | Entity, terms, Stripe, sales tax done |
| CP6 Billing | Before taking money | A full test-mode purchase, upgrade, and cancellation | **Opus** | Free users never see paid data; paid users always do |
| Gate 4 | End of Phase 3 | 4 weeks of alert delivery logs | `/ultrareview` | Alerts delivered for every run in the 4 weeks |
| CAL2 Benchmark calibration | Start of Phase 4 | 20 benchmark drafts | Owner (domain expert) | Owner approves at least 18 of 20 |
| CP7 First send | Before the first newsletter issue or outreach note goes out | One test issue and one test note sent to the owner's own address, and the approval screen with its recipient list | **Opus** | Tests show the sender refuses anything without a matching approval (text, recipients, channel) and never sends to an opted-out address; every message carries the postal address and a working opt-out |
| CP8 Oversight | Before the build overseer becomes a required check, and before the orchestrator's controls switch on | A dry run: the overseer's findings on the last 10 merged pull requests, and the orchestrator's actions replayed on recorded runs | **Opus** + adversarial tests (injected text in a diff, a filing, and a run log) | Injected text cannot place or lift a hold or trigger an action; every action can only delay, never publish or send |
| CP9 New market | Before each market of companies listed only outside the US publishes (ADR 0021) | 10 of that market's records beside their source sentences and labeled translations | **Opus** + reviewer subagent for the terms check | A separate random sample of that market's records meets the Gate 2 error rate; the market's terms allow collection |
| CP10 Business agents | Before the first competitor report and the first CFO report (ADR 0024) | The first report of each, with its sources and computed table | Reviewer subagent | Every claim in the competitor report quotes a source page; every number in the CFO text matches the computed table, checked by a test; neither agent can file an issue or change anything itself |
| CP11 Devices | Before any device catalyst publishes (ADR 0027) | 20 device catalysts beside their source sentences, across the device pathways | **Opus** (schema and rule checks) + reviewer subagent | Both readers re-scored with device releases in the gold set and no field below its floor; device rule checks tested against recorded openFDA decisions |
| CP12 First X post | Before the X feed posts for the first time (ADR 0029) | 10 sample posts made from recorded data with the approved template | Reviewer subagent + owner (approves the template and the go-ahead) | Every post has the notice and a link; every ranked catalyst has at least 10 watchers; no buy, sell, or advice words; no replies or mentions possible in the code |
| CP13 Corporate events | Before any corporate event or deal date publishes (ADR 0030) | 20 corporate events, deal dates, commercial milestones, and safety events beside their source sentences, across the new types, plus 10 peer events with their competitor match and measured move | **Opus** (schema and rule checks) + reviewer subagent | Both readers re-scored with the new types in the gold set and no field below its floor; financing amounts and deal dates match their filings in every sample |
| CAL3 Scenario calibration | Before catalyst scenarios publish | 20 scenario drafts with every input, plus the backtest on past catalysts | **Opus** review of the math + owner (domain expert) | Owner approves at least 18 of 20; backtest interval score beats the naive baseline |

## Quality bars enforced in CI

| Bar | Threshold |
| --- | --- |
| Test coverage, core packages (verification, publish gate, parsers, entitlements) | 90% of lines |
| Test coverage, everything else | 80% of lines |
| Function complexity | ruff C901 maximum 10 |
| Types | pyright strict and tsc strict; no `Any` in core packages without a comment explaining it |
| Docstrings | Every public function and class (ruff `D` rules) |
| Secrets | gitleaks finds nothing |
| Dependencies | Locked (`uv.lock`, `package-lock.json`); audit passes |
| Migrations | Upgrade, downgrade, upgrade again |
| Prompt evaluations | No field's accuracy drops against the gold set |

## Shipping fast without shipping junk

- **Walking skeleton first.** The thinnest end-to-end path (one filing to one database row) comes before any breadth.
- **Vertical slices.** Each pull request delivers one working path, not a layer nobody can use yet.
- **Build only what the current phase needs.** Keep interfaces clean so later phases plug in, but do not build later features early.
- **Time-box.** Each tracker item should take about one or two days. If an approach fails twice, stop and explain the problem to the owner instead of trying a third variation.
- **Small pull requests, merged daily.** Long-lived branches are where quality and speed both die.
