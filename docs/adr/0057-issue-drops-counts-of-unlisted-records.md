# 0057 — The issue no longer prints the counts of records it leaves out

- **Status:** accepted
- **Date:** 2026-11-23
- **Roadmap item:** Phase 2, Marketing and outreach agent: "Weekly issue hand-off"
- **Amends:** ADR 0047, decision 3 ("the issue states the two counts"); ADR 0049 (question 6b, counts in the free issue)

## Context

The free issue ended with two lines: "And N more in the next 30 days." and "Outside the next 30 days: N dates moved and N catalysts were added." On 2026-11-23, reading the first weekly hand-off, the owner said the reader "never needs to hear" them.

## Decision

1. The issue's HTML (free and paid) no longer prints either line.
2. The counts stay in the digest JSON and in what `catalyst digest` prints, so the weekly hand-off still reports them to the owner.
3. Nothing else changes: records outside the free calendar are still never named in the free issue.

## Alternatives

- **Keep the lines.** They hint that the paid issue holds more. The owner rejected them.
- **Replace them with a line about the paid issue.** Not asked for; a later owner decision if wanted.

## Consequences

- The free issue no longer tells readers that the calendar holds more than it lists.
- One test now checks the lines are absent and that the JSON keeps the count; the change explains why in the pull request.
