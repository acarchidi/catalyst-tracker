# 0033 — Several roadmap items at once

- **Status:** accepted
- **Date:** 2026-10-18
- **Roadmap item:** none; a change to how work proceeds (owner decision, 2026-10-18)

## Context

AGENTS.md said to take roadmap items one at a time and to wait for each merge before starting the next. On 2026-10-18 the owner asked to remove that rule. Many open items do not depend on each other, for example the review-queue screen and the foreign-company filings. Waiting on each merge leaves time unused.

## Decision

1. Several roadmap items may be in progress at once. Each still has its own branch from the latest main and one pull request, as before.
2. An item starts only if it needs nothing from a pull request that has not merged yet. An item that builds on unmerged work waits for that merge. This keeps the rule against stacked pull requests.
3. Earlier roadmap items go first when there is a choice.
4. At most three item pull requests are open at a time. Each push runs the Claude review, which spends on the review key, and the build overseer runs at most 8 times a UTC day (`config/overseer.toml`). Owner-path pull requests do not count toward the three.
5. When one pull request merges, main is merged into each other open pull request with a merge commit, conflicts are fixed, and the local checks run again before the push.
6. A checkpoint's review runs once every item that leads to it has merged, on main as it then stands.

## Alternatives

- **Keep one item at a time.** Fewer merge conflicts and the cheapest review spend. The owner chose speed.
- **No limit on open pull requests.** Faster on paper, but the overseer's daily limit would leave most of them unchecked, and every main merge would touch every open branch.

## Consequences

- More merge conflicts, mostly in the roadmap's decision log, which every item edits. These are fixed by merging main, never by rewriting history.
- Review and overseer spend per day goes up, within the existing caps.
- A checkpoint preview can come later than the first item that leads to it, because it waits for the last one.
