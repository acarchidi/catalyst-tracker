# 0020 — Marketing and outreach agent: drafts only, the owner approves every item

- **Status:** accepted
- **Date:** 2026-10-17
- **Roadmap item:** Phase 2, "Marketing and outreach agent"

## Context

The spec had a marketing agent that drafts newsletter issues and posts from published data, with the owner approving each item. On 2026-10-17 the owner asked to provision a marketing and outreach automation agent and chose:
- **Channels:**
  - the newsletter;
  - direct outreach to named people (journalists, newsletter writers, podcast hosts, investor communities);
  - listings and search-friendly pages.

  Social posts are not included for now.
- **Approval:** the owner approves every item before it goes out.

Commercial email in the United States falls under the CAN-SPAM Act. That includes business-to-business email. The Act requires:
- accurate headers and subject lines;
- a valid physical postal address;
- a clear way to opt out that keeps working for at least 30 days;
- opt-outs honored within 10 business days.

Penalties are set per email ([FTC compliance guide](https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business)). The product's publisher standing also depends on content that stays impersonal, is published on a regular schedule, and is not timed to specific market events (spec: Legal and compliance).

## Decision

1. **The agent drafts; plain code sends.** The marketing agent writes three kinds of draft:
   - newsletter issues;
   - outreach notes to named people;
   - directory submissions and the text of public pages.

   A plain-code sender sends or submits an item only when an approval row from the owner matches that item's exact text. A changed word needs a new approval. The agent cannot send, post, or submit anything itself.
2. **Inputs are published, checked data.** Every fact in a draft comes from published catalysts and carries a source link. Drafts contain no advice, no buy or sell language, and no return claims. The newsletter keeps its fixed weekly schedule and is never sent early because of a market event.
3. **Contacts come from public professional sources only.**
   - Plain-code collectors fetch the pages that list contacts, following robots.txt and rate limits. The agent reads the text with no tools, as every agent that reads web text does.
   - Each contact keeps only a name, outlet, public work address, and the source address where it was found.
   - No bought lists, and no addresses taken from behind a login.
   - A contact is deleted on request.
4. **Outreach limits:**
   - one note per person, plus at most one follow-up unless they reply;
   - every note says who is writing, includes the postal address, and gives a working opt-out;
   - an opt-out or "no thanks" stops all further mail to that address before the next send, well inside the 10-business-day limit.
5. **The newsletter goes only to people who signed up.** Every issue carries:
   - the postal address;
   - a one-click unsubscribe;
   - the "estimate, not financial advice" notice.
6. **The orchestrator watches this agent like the others (ADR 0019).** It can pause the marketing role. It cannot approve or send anything.

## Alternatives

- **Templates approved once, then sent automatically.** This means less owner work, but every routine mailing would go out unseen. The owner declined it.
- **Fully automatic outreach.** Messages to named people would go out without review, and the legal, platform, and reputation risk would fall on the owner's name. The owner declined it.
- **Scheduled social posts.** The owner left these out for now. If they are added later, the spec's existing rule stands: original scheduled posts only, with no automated replies or direct messages.

## Consequences

- The owner needs a physical postal address before the first commercial email. A registered PO box or private mailbox is enough. This joins the business-setup step, so all setup happens in one sitting.
- An email-sending service needs an account. It is chosen when this item is built, and it is gathered with the other Phase 2 accounts.
- The approval step uses the review-queue screen (Phase 1), so the owner approves drafts in the same place as records.
- The owner's time, not the agent, sets the outreach pace. The agent keeps a short queue of drafts, so nothing waits on it.
