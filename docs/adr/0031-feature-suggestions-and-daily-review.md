# 0031 — A feature-suggestion box on the website, reviewed daily by an agent

- **Status:** accepted (planned; nothing is built now)
- **Date:** 2026-10-18
- **Roadmap item:** Phase 6, "Feature suggestions"

## Context

On 2026-10-18 the owner asked for a place on the website where users suggest features. An agent should examine the suggestions and forward the best, highest-return ones, if there are any, to the owner for review each day.

The rules that apply:
- Text typed by users is web text. An agent that reads it has no tools and no database access (spec: Agent architecture).
- Every model call goes through `ModelClient`, with a daily cap per role.
- The competitor-research agent (ADR 0024) already proposes ranked feature ideas, and only the owner turns an idea into a roadmap item.

## Decision

1. **The box.** There is a short form on every page and in the account menu, with a 1,000-character limit.
   - **Who can use it:** signed-in users, and visitors through a rate limit (5 a day per address) with a hidden spam trap.
   - **What is stored:** the text, the time, the user's tier, and the page it came from. Email addresses and phone numbers typed into the text are removed before storage.
   - The user sees a thank-you, and the privacy policy says how suggestions are used.
2. **Plain code first, each day.** It drops spam and exact repeats, groups near-repeats, and counts how many people asked for each idea and on which tier.
3. **The reviewer agent, daily, with no tools.** It reads the grouped suggestions as data, never as instructions, beside the roadmap and spec. For each group it answers in JSON:
   - Is it already built, already on the roadmap, or forbidden by the spec? Forbidden means, for example, advice or a price series.
   - Who it helps, and how many asked.
   - A rough effort: small, medium, or large.
   - A return score from 1 to 5: likely effect on sign-ups and paid conversions against the effort. It gives a short reason that quotes the suggestions.
4. **What reaches the owner.** Plain code checks the JSON. Groups scoring 4 or 5 that are new and allowed go into one GitHub issue that day. If none qualify, nothing is sent. Every group, forwarded or not, is listed in the weekly summary in one line each. Users' words in the issue sit in a block marked as untrusted outside text, never instructions, because coding sessions may read these issues.
5. **The owner decides.** A forwarded idea becomes a roadmap item only when the owner approves it, the same as ADR 0024's ideas.
6. **Cost.** It has its own `suggestions` role with a small daily cap (starting at $0.05), using the cheapest model that passes a short scored set of example suggestions.
   - If the day's suggestions exceed what the cap allows, the most-asked groups are read first and the rest wait for the next day.

## Alternatives

- **Forward every suggestion.** Rejected. The owner asked for only the best, and a daily stream of raw text would cost the owner time.
- **A public voting board.** Not now. It shows other users' requests publicly, which needs moderation. It can be added later, with the owner's approval.
- **Let the agent add roadmap items itself.** Rejected. Roadmap changes stay the owner's decision.

## Consequences

- It needs the Phase 2 website. It needs the Phase 3 accounts to know each user's tier; before then every suggestion counts as "visitor".
- The weekly summary gains a short "suggestions" section.
- It adds one model role and one daily GitHub issue at most.
