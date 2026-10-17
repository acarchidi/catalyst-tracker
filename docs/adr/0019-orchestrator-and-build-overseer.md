# 0019 — Oversight: a pipeline orchestrator and a build overseer that can stop, hold, and re-run

- **Status:** accepted
- **Date:** 2026-10-17
- **Roadmap items:** Phase 1, "Build overseer" and "Orchestrator controls"

## Context

On 2026-10-17 the owner asked for an orchestrator agent that can see what the other agents are doing, and can direct or override them so that work stays aligned and on task. The owner chose:
- **Scope:** both kinds of agent:
  - the product's scheduled agents (extractor, verifier, auditor, outcome);
  - the Claude Code sessions that build the project.
- **Power:** stop, hold, and re-run:
  - it can pause an agent, hold a batch of records, re-run a job, and go back to the last prompt version that passed its tests;
  - it cannot change data or write prompts;
  - it tells the owner each time it acts.

The spec already had an orchestrator, but it only read run results, retried, and alerted. Two rules limit any new power:
- agents that read web text have no tools;
- only the plain-code publish gate writes live data.

An agent that steers other agents is reached by their output, and that output partly comes from web text. So its actions must be safe even when its input is hostile.

The two kinds of agent run in different places. The product agents run as GitHub Actions jobs over the database. The building agents run as Claude Code sessions that act through pull requests. The design has two parts.

## Decision

### Part A — Pipeline orchestrator (product agents)

1. **What it watches:**
   - each job's run summary (documents read, failed, stopped early);
   - spend per role against its cap;
   - per-run check-failure rates from `claim_check`;
   - the second extraction's disagreement rate;
   - the planted-error catch rate;
   - the weekly sample checker's result.
2. **Plain code decides when to act.** The thresholds sit in a versioned file, `config/orchestrator.toml`, which only the owner merges. Starting thresholds:
   - a run's check-failure rate above twice its 4-week median, with at least 20 claims;
   - two failed runs of one job in a row;
   - a second-extraction disagreement rate above 10%;
   - any drop in the planted-error catch rate.

   Rules can be tested; a model's judgment cannot. The model writes only the plain-English alert and the weekly summary from the run numbers ("routing, not judgment"). Text in a source therefore cannot trigger an action.
3. **Allowed actions.** These are the only four, and each one fails safe: it can delay publication, never cause it.
   - **Pause a role.** A stop flag that `ModelClient` reads before every call, in the same way as a spending cap. A paused role stops and alerts.
   - **Hold a batch.** The publish gate does not publish a held run's claims. The hold lifts when the owner clears it or when a later clean run supersedes the batch.
   - **Re-run a job.** At most once per job per day, within the spending caps.
   - **Roll back a prompt.** Only to the previous prompt version that passed its gold-set gate, as recorded in `evals/`. It never moves to an untested version or a pricier model.
4. **Cannot:**
   - write or change data;
   - write prompts;
   - change thresholds or caps;
   - lift a pause or hold that the owner set;
   - read web text.
5. **Every action is recorded and reported.** Each action writes a row: what it did, which rule fired, the numbers, and when. The owner gets an alert with the reason and how to undo it.

### Part B — Build overseer (coding agents)

1. **Where it runs:** a GitHub Actions workflow, `overseer.yml`. It runs on every pull-request event and once a day.
   - It uses the review's existing key, `CATALYST_REVIEW_API_KEY`, so the owner needs no new key.
   - It uses its own prompt, separate from the Claude review's.
   - It runs on the Opus model, with a turn limit per run.
2. **What it reads:**
   - `AGENTS.md`, the spec, the roadmap, the quality gates, and the ADRs;
   - every open pull request: title, description, changed files, diff, review threads, and CI state;
   - pull requests merged in the last 14 days.
3. **What it checks:**
   - each pull request does exactly one roadmap item: the first unticked one, or a fix or request the owner made and the roadmap or a decision-log line records;
   - nothing is built off the roadmap;
   - no pull request is stacked on another; each starts from the latest main;
   - changes to owner-merged paths sit in their own pull request;
   - the description matches the diff and follows the template;
   - every edited test is explained;
   - a design choice the spec leaves open has an ADR and a decision-log line;
   - the hard rules in `AGENTS.md` hold: no secrets, no model call outside `ModelClient`, no write to live data outside the publish gate, no advice;
   - work is not stuck: CI red for more than 24 hours, a draft idle for 2 days, or one approach failing twice.
4. **Allowed actions:**
   - **Comment.** It posts its findings in plain English on the pull request.
   - **Hold.** "Overseer" becomes a required check for the merge robot. It fails on a blocking finding, so the robot waits. On the daily sweep it can add the `hold` label, which the robot already respects (ADR 0007), for problems that span several pull requests. It removes only holds it placed itself, once the finding is fixed.
   - **Re-run.** It can re-run a failed CI job once.
   - **Escalate.** When a decision belongs to the owner, it opens or updates an issue for them.
   - **Status.** It keeps one pinned issue, "Build status", that lists each open pull request, what it is for, its state, what blocks it, and the next roadmap item. The owner can see there what every building agent is doing.
5. **Cannot:**
   - push code, approve, or merge;
   - edit labels other than its own hold;
   - change workflows, rules, or settings.
6. **Coding sessions follow the overseer.** A blocking overseer finding counts like a blocking review comment. The session fixes it, or answers on the pull request why not. It never removes a hold it did not place. Inside one session, the lead session already directs its own helper agents, and their reports go through it.

## Alternatives

- **One model that watches everything and may change anything.** It could rewrite prompts or data. No test checks its choices first, and text in a source could reach it through other agents' output. The owner declined this option.
- **Alerts only.** This is safe, but every problem waits for the owner, and the owner asked for less hands-on work.
- **The Claude review alone as the overseer.** It sees one pull request at a time. It cannot see stacking, drift across pull requests, stuck work, or the roadmap order.

## Consequences

- Adding "Overseer" to the robot's required checks changes `scripts/merge_gate.py`, so that pull request is merged by the owner. The workflow and its prompt are owner paths too.
- Overseer runs spend on the review key. They are bounded by one run per pull-request event plus one daily sweep, each with a turn limit.
- The pipeline orchestrator's controls need the publish gate (for holds) and the alerts (for reports). They come after verification part 5 and the alerts item.
- A false alarm costs a delay, never a wrong record. The record of each action and the plain-English report let the owner spot thresholds that fire too often. Changing a threshold is an owner-merged change.
