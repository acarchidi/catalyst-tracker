# 0036 — The coding agent merges owner-only pull requests after a separate review

- **Status:** accepted
- **Date:** 2026-10-19
- **Roadmap item:** none; a change to how work proceeds (owner decision, 2026-10-19)

## Context

The merge robot (ADR 0007) leaves pull requests that change the rules, the review, the workflows, the spending caps, or the score floors to the owner. On 2026-10-18 and 2026-10-19 the owner asked not to merge pull requests or click approvals any more ("i don't want to have to click stuff"). In the session's "Accept edits" mode each merge the agent requests needs one approval click.

## Decision

1. The repository's `.claude/settings.json` allows the GitHub merge tool, so a coding session can merge without an approval prompt.
2. The coding agent merges an owner-only pull request itself only when all of these hold:
   - the required CI checks ("Lint, types, tests, migrations", "Dependency audit", "Secret scan") passed on its latest commit;
   - a separate reviewer subagent said OK with no open blocking finding. The reviewer runs on the Opus model when the change touches the review, the merge robot, the publish gate, a spending cap, or a score floor;
   - it has no `hold` or `overseer-hold` label.
3. After merging, the agent tells the owner in one plain line what merged and what the reviewer found.
4. A change that raises a spending cap or lowers a score floor still needs the owner's written OK in the conversation before the agent merges it.
5. Ordinary `claude/` pull requests are unchanged: the merge robot merges them after CI and the Claude review.

## Alternatives

- **The owner keeps merging.** The safest, and what the owner asked to stop.
- **The merge robot merges rule changes after a review that runs from main and a 12-hour wait.** Safer, because the pull request cannot change its own reviewer, but more to build. It can replace this rule later.

## Consequences

- No human checks a rule change before it lands. The checks on it are the CI tests, a separate reviewer, and the owner's `hold` label.
- A mistaken or manipulated instruction could change the guardrails. The reviewer judges each change against main's AGENTS.md and the spec, and any change can be reverted with a new pull request.
- `.claude/settings.json` itself is an owner-only path, so changing this permission again also goes through this rule.
