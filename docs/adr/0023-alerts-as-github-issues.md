# 0023 — Alerts and the weekly summary as GitHub issues, written by plain code

- **Status:** accepted
- **Date:** 2026-10-17
- **Roadmap item:** Phase 1, "Auditor agent, weekly link check, sample checker..., alerts only when something needs the owner plus a weekly summary"

## Context

The owner decided on 2026-10-17: alerts only when something needs them, plus a plain-English weekly summary with the error rate (spec: Agent architecture, Orchestrator). The spec does not say how alerts reach the owner. The owner wants no new accounts or keys. An email service would need both.

ADR 0019 says the model writes the alert text and the weekly summary from the run numbers. The numbers are few and fixed, so plain sentences in code say the same thing, cost nothing, and can be tested.

Some alert inputs come from runs that read web text. An error message can carry words from a source document or an address from a setting. An issue body is not masked the way a run log is.

## Decision

1. **Delivery is GitHub issues.** GitHub emails the repository owner about new issues and comments by default, so no new account or key is needed.
   - An alert is an issue labelled `alert`. A weekly summary is an issue labelled `weekly-summary`, one per week. The poster creates both labels the first time.
   - Each issue ends with a hidden marker: `<!-- catalyst-issue key=... runs=N -->`. Issues that do not end with it, or that the workflow (`github-actions[bot]`) did not open, are never touched.
   - A repeat of an open alert's key adds a comment and counts up `runs`. Once a job has failed `failed_runs_in_a_row` runs in a row (2 to start), the comment says so in bold.
   - A later clean run of the same job closes its alert, with a comment that links the clean run.
   - A weekly summary re-run on the same day edits that day's issue. Posting a new week's summary closes the older ones.
2. **Plain code builds every alert** (`catalyst.alerts`), with no model call:
   - **Job failures.** It reads the JSON each job prints and the step's outcome in GitHub Actions. A failed step, or a summary with failed documents, an early stop, unfinished matching, checks, second reading, publish decisions, or catalyst writing, collection errors, or missing or changed stored copies, raises `job-failed:<job>`. A skipped or cancelled step neither raises nor solves.
   - **Spending.** A role raises `spend-cap:<role>` when its spend today (New York time, from `model_call`) reaches `spend_share_of_cap` of its daily cap (0.9 to start). It also raises one when less is left under the cap than one call with a full answer could cost. The model client stops a role at that point, which for the small caps of ADR 0022 can be far under 90%. If spending cannot be read, that is an alert too.
   - **Not alerts.** A second reading that has no scores yet is low urgency. It appears in the weekly summary only.
   - **Wording.** Alerts are built from counts and names chosen in code, never from an error message. They link the run log for detail.
3. **Thresholds** live in `config/orchestrator.toml` (ADR 0019). The file is in `OWNER_PATHS`.
4. **The weekly summary** (`catalyst.weekly`) covers the 7 days before the run, from stored rows:
   - documents collected;
   - claims extracted;
   - claims passing every check so far, and how many are not checked yet;
   - checks failing, by check, in plain words;
   - spend by role: the week's total and the most in one day against the daily cap;
   - open alerts, with links;
   - the error rate. Until the weekly sample checker exists, it says "No sample yet".
5. **Commands and workflows.**
   - `catalyst alerts` and `catalyst weekly-summary` print the issue bodies and write a plan file. `catalyst post-issues` posts the plan through the GitHub REST API, using the run's own `GITHUB_TOKEN`.
   - The live workflows (`extract-live.yml`, `gate1-live.yml`) build alerts at the end of the job, even after a failure. Each long step has its own time limit, under the job's, so a step that runs too long fails and is reported; a job that hits its own limit is cancelled and reports nothing. A separate job posts them. Only that job has `issues: write`, and it holds no other secret.
   - `weekly-summary.yml` runs on Saturdays at 22:00 UTC (6pm New York time in summer, 5pm in winter), and by hand.
   - Built in two pull requests: the alerts and the poster first, then the weekly summary.

## Alternatives

- **An email service (for example Resend or SendGrid).** Needs an account, a key, and a sending domain: owner setup the owner asked to avoid. The marketing agent needs one later anyway (ADR 0020); alerts can move there then.
- **A model writes the text, as ADR 0019 first said.** It costs money every run, needs the orchestrator key and cap, and cannot be tested like code. The numbers are simple enough for fixed sentences.
- **GitHub's own email for failed workflow runs.** It goes only to whoever started the run, says nothing about spending, and gives no weekly view. It still arrives as a fallback if the alert step itself breaks.
- **Run history in a new table, for "two failures in a row" and the 4-week check-failure median.** That needs a migration. The open issue already says whether the last run of that job failed. The median rule, the second-reading disagreement rate, and the planted-error catch rate need history per run. They come with the orchestrator's controls (ADR 0019), which need a table for their own action log anyway.

## Consequences

- No setup for the owner. Alerts arrive as GitHub emails if the owner's notification settings for the repository are left at their defaults (Watching).
- A run that fails before its alert step can run (for example, when installing dependencies fails) posts nothing. GitHub's failed-run email to whoever started it still arrives.
- Alerts read only numbers, so text in a source cannot change an alert. A false alarm costs one email. Changing a threshold is an owner-merged change.
- Today, check failures appear only in the weekly summary. A check-failure alert waits for run history.
- These issues are messages to the owner about the pipeline. They are not the product's alerts to readers (spec: Watchlists and alerts), which carry catalyst data and the "not financial advice" notice.
- When the weekly sample checker lands, the summary's error-rate line reads its results instead of "No sample yet".
- A change that takes two requests (a comment, then an edit or a close) can fail after the comment. The next run then repeats that comment. This is accepted: one extra email, and the next run puts the issue right.
