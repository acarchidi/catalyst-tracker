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

   A plain-code sender sends or submits an item only when an approval from the owner matches all three of these:
   - the item's exact text;
   - its exact list of recipients;
   - its channel.

   A changed word or a changed recipient needs a new approval. The approval screen shows each recipient and the page where the address was found. Only the owner's own signed-in action can create an approval; no agent or job can. At send time the sender checks every recipient against the opt-out list again. The agent cannot send, post, or submit anything itself.
2. **Inputs are published, checked data.** Every fact in a draft comes from published catalysts and carries a source link. Drafts contain no advice, no buy or sell language, and no return claims. The newsletter keeps its fixed weekly schedule and is never sent early because of a market event.
3. **Contacts come from public professional sources only.**
   - Plain-code collectors fetch the pages that list contacts, following robots.txt and rate limits. The agent reads the text with no tools, as every agent that reads web text does.
   - Each contact keeps only a name, outlet, public work address, and the source address where it was found.
   - United States contacts only for now. Canada's and the EU's email rules are stricter than CAN-SPAM, so contacts there wait until those rules are checked.
   - "Investor communities" means the organizers or editors of investor groups and forums, written to one at a time. The agent never posts to a group itself.
   - No bought lists, and no addresses taken from behind a login.
   - A contact is deleted on request.
4. **Outreach limits:**
   - one note per person, plus at most one follow-up unless they reply;
   - every note says who is writing and that it is a promotional message, includes the postal address, and gives a working opt-out;
   - opting out takes only a reply or one web page, with no fee and no other information asked;
   - an opt-out or "no thanks" stops all further mail to that address before the next send, well inside the 10-business-day limit;
   - an opted-out address is never sold or passed on;
   - the business stays responsible for what the email-sending service sends in its name. The FTC guide lists fines of up to $53,088 per email.
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

- **The sender stays switched off until business setup is done.** The drafting can be built first. Sending needs:
  - a physical postal address (a registered PO box or private mailbox is enough);
  - an email-sending service account;
  - a sending domain with its email-authentication records (SPF, DKIM, DMARC);
  - a newsletter sign-up form and a subscriber list.

  These are gathered with the business setup, so all setup happens in one sitting. The new email setting gets a name in `AGENTS.md` and a line in `scripts/check_setup.py` when this is built.
- Before the first newsletter or note goes out, checkpoint CP7 runs (`docs/quality-gates.md`).
- The approval step uses the review-queue screen (Phase 1), so the owner approves drafts in the same place as records.
- **The prospect list lives at `outreach/prospects.csv` (added 2026-10-18, owner request).** Plain code (`catalyst.outreach.prospects`, run as `catalyst prospects-check`) checks every row: a fixed category, https source and contact pages, a lower-case work email or none (no personal mailboxes), no duplicate email or page, and a status (`new`, `approved`, `contacted`, `replied`, `declined`, `do_not_contact`) with its date once it changes. Later research adds rows only through `merge_prospects`, which skips anyone already listed and never changes a status. A deletion request becomes `do_not_contact`: the row stays, so no research run can add the person back, and it is never used for sending. `outreach/README.md` explains the file for the owner.
- The owner's time, not the agent, sets the outreach pace. The agent keeps a short queue of drafts, so nothing waits on it.
