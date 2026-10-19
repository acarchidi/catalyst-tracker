# 0022 — Spending controls: lower daily caps, no paid review on owner-merged pull requests

- **Status:** accepted
- **Amended by:** ADR 0036 (2026-10-19), decision 2: owner-path pull requests are now merged by the coding agent after a separate review; the paid review still does not run on them.
- **Date:** 2026-10-17
- **Changes:** ADR 0004 (the caps), ADR 0006 (the Claude review), ADR 0013 (the extractor's cap)

## Context

On 2026-10-17 the Anthropic account's prepaid credit ran out. API calls failed with "Your credit balance is too low to access the Anthropic API". The owner asked to measure what reviews cost and to run everything as cheaply as possible.

Today's logs gave these numbers:
- **Cost per review:** across 12 successful Claude reviews, the median was $0.27, the range $0.10 to $0.39, and each took 6 to 11 steps.
- **Total:** 23 reviews succeeded that day, about $6.
- **Caps:** the product's daily model caps added up to $13.50, which allows up to about $405 a month.

## Decision

1. **Daily caps total $6** (owner decision). They are in `config/models.toml`:
   - reader $2, second reader $2;
   - outcome $0.25, auditor $0.50;
   - triage $0.25, name matching $0.25;
   - analyst $0.50;
   - research assistant $0.125, orchestrator $0.125.

   Each cap covers at least one worst-case call of its role. One analyst call on Opus can cost about $0.35, so its cap is $0.50.
2. **No paid Claude review on pull requests the owner merges by hand** (owner decision).
   - The review workflow checks the changed files against `OWNER_PATHS` and `OWNER_FILE_NAMES`. It reads them from the base commit's `scripts/merge_gate.py`, never from the pull request.
   - When any file matches, no model runs. The check **fails**, with the reason "not run, the owner merges this".
   - It fails rather than passes because the robot's own file list can differ from the one the review saw (separate review of PR #35). Two cases:
     - the base moved after a sibling pull request merged;
     - the pull request lists more files than GitHub's files API returns (3,000).

     So an unreviewed commit never carries a passing "Claude review" that the robot could count.
   - A separate reviewer in the coding session still checks every owner-merged pull request (AGENTS.md).
   - This narrows ADR 0006's "the review must run on any pull request that can merge". Owner-path pull requests can only merge by the owner's hand.
3. **Fewer review runs.** Coding sessions push to a pull request marked ready only when a change is finished, because each push to a ready pull request starts a paid review.

## Alternatives

- **Sonnet for every review.** Its token price is half Opus's, but it gives a weaker check on code the robot merges by itself. The owner did not choose it.
- **A passing "not run" result.** This was the first version (PR #35). The cases above showed that the robot could then merge an unreviewed commit.
- **Keeping the $13.50 caps.** The caps are only ceilings, but the owner wants a firm monthly bound while building.

## Consequences

- Owner-merged pull requests show a red "Claude review" check, on purpose. The robot no longer posts "waiting for the owner" on them.
- `.github/rulesets/main.json` lists "Claude review" as a required check. The ruleset is not active on the free plan (ADR 0007). If it is ever imported (docs/setup.md, "Later, with a paid GitHub plan"), the owner merges owner-path pull requests with the ruleset's bypass.
- At most about $180 a month in product model calls, plus reviews at about $0.27 each.
- A large catch-up extraction stops at the $2 cap and resumes the next day.
