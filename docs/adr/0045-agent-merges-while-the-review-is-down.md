# 0045 — While the Claude review cannot run, the coding agent merges after a separate Opus review

- **Status:** accepted (temporary)
- **Date:** 2026-10-21
- **Roadmap item:** none; a change to how work proceeds (owner decision, 2026-10-21)
- **Overrides, while it lasts (owner's written OK, 2026-10-21):** AGENTS.md's "How work proceeds" lines that ordinary `claude/` pull requests go through the merge robot and that the agent merges only as ADR 0036 allows, and the "Protected main" practice that the Claude review must pass before a merge; ADR 0006 (decision 1), ADR 0007 (decision 1) and ADR 0036 (decision 5). Ordinary `claude/` pull requests may be merged by the coding agent under ADR 0036's rules. AGENTS.md itself is not changed.

## Context

Since about 14:25 UTC on 2026-10-20 the `Claude review` check fails on every pull request before it reviews anything. The model call ends in under a second at no cost with `is_error: true`, and the action reports "did not return structured_output". The Overseer uses the same key (`CATALYST_REVIEW_API_KEY`), so it fails the same way. Re-runs on 2026-10-20 at 17:22 and 22:42 UTC and on 2026-10-21 at 00:07, 04:11 and 14:09 UTC all failed. The job log does not show the API's error text. Meanwhile eight finished pull requests wait, and AGENTS.md says not to start the next item before the last one merges. The owner chose, in the conversation on 2026-10-21, to let the coding agent merge for now and to be reminded of it.

## Decision

1. While the `Claude review` check fails without a verdict (no result at all, as above), the coding agent may merge an ordinary `claude/` pull request itself under all of ADR 0036 decision 2, with two changes:
   - the separate reviewer subagent always runs on the Opus model, whatever the change touches;
   - "Claude review" and "Overseer" may be failed only by this outage: a failed run on the pull request's latest commit whose log shows the outage's signature (the model's result has `is_error: true`, zero cost, and no `structured_output`). A "did not finish" comment alone is not enough, because it also follows a partial answer. A review or overseer run that returns a verdict is followed as before, and any open blocking finding still stops the merge.
2. Everything else in ADR 0036 still holds: the CI checks "Lint, types, tests, migrations", "Dependency audit" and "Secret scan" pass on the latest commit; the reviewer reviews that same commit; no `hold` or `overseer-hold` label; decision 4 still needs the owner's written OK.
3. After each merge, the agent tells the owner in one plain line what merged and what the reviewer found, and notes on the pull request that it merged under this ADR.
4. This rule ends the first time a `Claude review` run returns a verdict again. The agent then stops using it, tells the owner, and runs the Claude review once on main's state to look for anything the interim reviews missed.
5. At least once a day while the rule lasts, the agent re-runs one failed Claude review to see whether the key works again, and reminds the owner that the rule is in force.
6. This pull request changes only ordinary files, so it is itself merged under this rule, on the owner's written OK.

## Alternatives

- **Wait for the key.** Safest; it stops all progress until the owner finds the cause.
- **The owner merges by hand.** One click per pull request; the owner asked not to click.

## Consequences

- For a while, no check outside the coding session looks at ordinary pull requests. The separate Opus reviewer and CI are the only checks. Every merge stays listed in the pull request's comment and can be reverted.
- The merge robot and `scripts/merge_gate.py` are not changed; they still wait for the Claude review, so nothing merges itself.
