# Quality gates and checkpoints

Work stops at every checkpoint below until the owner replies "approved". The owner does not read code. Each checkpoint therefore gives the owner something they can judge without reading code (a preview), and gives the code to a fresh reviewer that did not write it.

## What happens at every checkpoint

1. **Automated checks pass** in CI: lint, strict types, tests, coverage thresholds, evaluation gate, secret scan.
2. **Fresh-eyes review.** Run `/ultrareview` (or a reviewer subagent with no access to the writing session's reasoning) and fix what it finds. Checkpoints marked **Opus** must be reviewed with the Opus model: ask the owner to run `/model opus` first.
3. **Preview for the owner.** A plain-English summary with no jargon, plus the artifact named in the table (sample records with clickable links, a screenshot, a preview URL).
4. **Stop and wait.** Post the summary and the question "Approve checkpoint X?" Do not start the next item until the owner approves.

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
| Gate 2 | End of Phase 1 | Weekly sample results; error rate with its confidence bound | **Opus** + `/ultrareview` | Under 2% errors on the sample; 100% deep links |
| CP5 Site preview | Before the free launch | Vercel preview URL to click through on phone and laptop | Accessibility and performance checks | WCAG 2.2 AA checks pass; pages load fast on mobile |
| Gate 3 | End of Phase 2 | Business setup confirmed by the owner | None | Entity, terms, Stripe, sales tax done |
| CP6 Billing | Before taking money | A full test-mode purchase, upgrade, and cancellation | **Opus** | Free users never see paid data; paid users always do |
| Gate 4 | End of Phase 3 | 4 weeks of alert delivery logs | `/ultrareview` | Alerts delivered for every run in the 4 weeks |
| CAL2 Benchmark calibration | Start of Phase 4 | 20 benchmark drafts | Owner (domain expert) | Owner approves at least 18 of 20 |

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
