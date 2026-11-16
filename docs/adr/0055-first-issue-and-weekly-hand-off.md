# 0055 — The first issue goes out without the remaining entry facts; a weekly hand-off builds each issue; work runs in batches

- **Status:** accepted
- **Date:** 2026-11-16
- **Roadmap item:** Phase 2, Marketing and outreach agent: "Publish on Substack for now" and "Weekly issue hand-off"
- **Amends:** ADR 0053 (the agent's proposal that the first issue waits for every entry fact); ADR 0045, decision 4 (the catch-up review of main now waits for the owner's answer) and decision 5 (how often the review is re-tried, now moot)

## Context

The weekly digest, the paid issue, the issue layout and the indication on each entry are built (ADR 0047, 0049, 0053). ADR 0053 proposed that the first issue wait for four more facts on every entry: conference dates, development stage, company pipeline pages, and cohort size. Each is its own pull request, and cohort size first needs an owner-only spec change.

The owner said on 2026-11-16 that they "can't afford all these token costs" and asked what is left before launch. Asked whether the first issue should wait for the four facts, the owner chose "Launch without them".

On 2026-10-26 the owner asked that coding work run "in batches" and that product jobs run "once per day". The collect and extract workflows already run once each weekday on a schedule (`collect-live.yml`, `extract-live.yml`).

The Claude review failed from 2026-10-20 because the account's purchased credit balance was $0; promotional credit does not cover Claude Code. The owner bought credits on 2026-11-16, and the review returned a verdict again that day, so ADR 0045's interim rule ended.

## Decision

1. **The first issue does not wait for the remaining entry facts.** Conference dates, development stage, pipeline pages and cohort size stay on the roadmap in ADR 0053's order and are added to issues as each one merges. No check is loosened: an entry still publishes only through the publish gate, and a fact still appears only with its own claim.
2. **Weekly hand-off, every Monday.** A scheduled task starts a fresh coding session each Monday morning, New York time. That session follows `docs/weekly-issue.md`: it makes a one-off key pair, runs `extract-live.yml` in `export` mode with the public key, unseals the export, builds the free and paid pages with `catalyst digest --from-export --page`, and sends both pages to the owner privately with the week's counts. It prints counts only. It never commits, publishes, or posts the pages, the export, or the key. The owner reads each issue and publishes it on Substack by hand, which is the approval ADR 0020 requires.
3. **Work runs in batches.** A coding session takes a stretch of roadmap work and finishes it in one go. It checks CI and reviews when its work is done instead of reacting to each GitHub event, so a long session is not re-read on every event. Model-using product jobs run at most once a day; the existing schedules already do.
4. **ADR 0045 has ended.** Its decision 4 asks for one Claude review of main's state to look for anything the interim reviews missed, covering every pull request merged between 2026-10-20 and 2026-11-16. That review costs roughly $5 to $15 of the owner's credit, and the owner asked on 2026-11-16 to cut costs, so this ADR changes decision 4: the agent asks the owner whether to run it, and runs it as soon as the owner says yes. Until the owner answers, the question stays open in the agent's report to the owner.

## Alternatives

- **Wait for all four facts (ADR 0053's proposal).** Declined by the owner: several more weeks and four reviewed pull requests before any issue.
- **Build the issue in a GitHub Actions job.** The paid page must never appear in a public log or artifact, and the repository is public, so the pages are built only in a private session from the sealed export, as the prototype pages are.
- **The owner runs the steps.** The owner is not a programmer and asked not to click through technical steps.

## Consequences

- The first issues show indication but not the other four facts. Entries without an indication still publish, as ADR 0053 allows.
- Each Monday costs one short coding session. The prompt is fixed and the steps are scripted, so it stays small.
- Before the first issue is published, CP7b still applies: the Opus review of the issue-building code, and the owner's test email from Substack with a postal address and a working unsubscribe link.
