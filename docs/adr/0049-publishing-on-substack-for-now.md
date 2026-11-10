# 0049 — Substack is where the digest is published, for now

- **Status:** accepted
- **Date:** 2026-11-09
- **Roadmap item:** Phase 2, "Marketing and outreach agent" (owner decisions 2026-11-09); builds on ADR 0020 and ADR 0047

## Context

ADR 0047 closed the first distribution loop: plain code builds a weekly digest. It left the second loop open, because readers need a place to sign up and a way to receive each issue. The roadmap planned our own sign-up form, subscriber list, and sender, all waiting for a domain, a sending service, and a postal address.

On 2026-11-09 the owner decided:

- Substack is the place to publish for now.
- The owner approves each issue (ADR 0047, question 6a). ADR 0020 stands.
- Date moves outside the next 30 days are counted in the free issue, never named (question 6b).

Facts checked on 2026-11-09, with their quality:

- Substack publishes no official way for a program to post. Community tools exist, but they use a signed-in browser's cookies and undocumented addresses. (Source: third-party write-ups; Substack's own pages were not found saying either way.)
- Substack keeps 10% of paid subscription revenue, and Stripe charges card fees on top. (Source: Substack's FAQ as quoted by third parties; the exact Stripe percentages differ between sources.)
- Substack does not collect or pay sales tax for writers; a writer may switch on Stripe Tax. (Source: Substack product notes and creator posts; not tax advice.)
- Substack says its own network brings a large share of new subscriptions, between 30% and 50% depending on the post. (Source: Substack's own posts; undated in the search results.)

## Decision

1. **Each issue is published on Substack by the owner's own hand.** The owner pastes the issue into Substack's editor and presses publish. That act is the approval ADR 0020 requires: the exact text, the recipients (the publication's subscribers), and the channel. No program posts to Substack, signs in to it, or uses its undocumented addresses.
2. **Substack is the sign-up form, the subscriber list, and the sender for now.** Our own form, list, and sender (ADR 0020) wait until the public site exists. The roadmap item for them is not dropped.
3. **The issue reaches the owner privately, ready to paste.** `catalyst digest --page` writes a complete web page of the issue, so the owner can open it, select all, copy, and paste. The repository is public, so an issue the owner has not approved never appears in a job log, a job file, an issue, or a pull request. Jobs print counts only. A coding session builds the page and hands it to the owner, the same way the review queue reaches the owner (ADR 0042). A coding session cannot reach the live database, so it builds the issue from the private export (`catalyst digest --from-export`, on the file ADR 0034 describes); a test proves that gives the same issue as the database does. The export must have been made after the issue's week ended; an older one is refused, because it would miss the last days' changes and give a short issue without saying so.
4. **A fixed weekly rhythm.** The issue is dated Monday and covers the week before (ADR 0047). The owner's part is one sitting a week: read the issue, click the source links they choose to check, paste, publish.
5. **Free and paid issues.**
   - The **free issue** is ADR 0047's digest: the next 30 days, and counts for everything outside it.
   - The **paid issue** is a second post for paid subscribers only. The coding agent proposed it to meet the owner's request of 2026-11-09 for subscription revenue; the owner may change or drop it. It lists the calendar from the issue date on (the first 200 entries, with the rest counted), every date move of the week wherever its window falls, and the date history of every catalyst it lists. It is built from the paid shape (`CatalystDetail`), by the same plain code (`catalyst digest --access paid`), under the same Monday rule. An issue refuses to be built or read back as a free issue that holds the paid shape, and tests prove it. Code that changes an issue after it is built must build it again through the same check. It is never printed anywhere public.
   - The price is the spec's: $15 a month, with an annual plan. The owner sets both in Substack.
   - **Payment (owner decision 2026-11-09).** Asked how to take payment before the business entity exists, the owner chose: charge now as an individual, through Substack and the owner's own Stripe account, and form the entity afterwards.
   - **This conflicts with the spec as it stands.** The spec (Compliance and legal, "Business setup") says the entity, terms of service, Stripe, and sales tax are required before taking payment, and Gate 3 and CP6 say the same. AGENTS.md says to ask the owner when the documents conflict; the owner was asked and this is the answer. `docs/spec.md` and `docs/quality-gates.md` are owner-only files, so their lines were changed in their own owner-only pull request the same day: they now name paid Substack subscriptions as the one exception.
   - **What the decision covers:** paid Substack subscriptions only. Our own checkout (Phase 3) still waits for Gate 3 and CP6.
   - **What is still the owner's to settle, entity or not:** a sales-tax decision (Substack does not collect or pay sales tax for writers; this is a question for an accountant); a postal address in every email (decision 7); and whether Substack's own terms and privacy policy are enough for the publication, which was not checked here. Until the entity exists the owner carries personal liability.
   - **The coding agent takes no payment and flips no switch.** Paid plans, the founding-member plan, and the referral program are the owner's switches in Substack.
6. **Every issue keeps the product's rules.** The notice stands above and below the records. Every entry has its quote and deep link. No advice words. Nothing tailored to a reader. The issue is never moved to follow a market event.
7. **Before the first issue is sent, the owner checks one test email** for a postal address in its footer and a working unsubscribe link (CAN-SPAM). The first issue waits for this check. Whether Substack adds a postal address by itself was not confirmed, so the check is made on a real test email. CP7 in `docs/quality-gates.md` stands as written for our own sender; this check for Substack is checkpoint CP7b there.
8. **Substack's social features stay off.** Notes and Chat are social posts, which the owner left out on 2026-10-17. Recommendations between publications are not posts; the owner may use them.
9. **Counts for the growth report come from the owner, monthly and in private.** Substack offers no way for a program to read subscriber counts. Once a month the owner gives four numbers (free subscribers, paid subscribers, new paid, cancelled). They are business figures, so they stay in the owner's private page, never in the public repository.
   - This changes the roadmap item ADR 0047 added ("weekly growth report as a GitHub issue"): business figures do not go into public issues. Counts our own site can make (visits, sign-ups, MCP calls) come back as plain-code counts when the site exists.
   - In the same way, the "digest on a schedule" item becomes a Monday hand-off by a coding session (decision 3), because an unapproved issue must not appear in a public job.
10. **An accuracy page.** Plain code writes a short page from stored results: the weekly sample's error rate with its bound, the dead-link rate, and each correction. The owner posts it on Substack as a page, and again when the numbers change. It starts once the sample checker has results.

## Alternatives

- **Post to Substack by program.** No official way exists, the unofficial ways break without notice and may breach Substack's terms, and it would skip the owner's approval.
- **Our own site and sender first.** Full control and no 10% cut, but it needs a domain, hosting, a sending service, accounts, and billing code before the first reader. Substack needs none of those to start; a postal address in each email is needed either way (decision 7).
- **Another newsletter service with a posting interface.** Possible later. The owner already has the Substack.

## Consequences

- The first reader can arrive as soon as one issue is published, once the test email of decision 7 has been checked. Our own site is no longer on the path to the first subscriber.
- Substack's cut and card fees leave roughly $12.70 of each $15 monthly charge (10% to Substack, about 2.9% plus $0.30 to Stripe, and a recurring-billing fee near 0.5%). This is an estimate from the sources above.
- Subscribers belong to the publication and can be exported, so a later move to our own sender does not lose them.
- Paid Substack readers and paid site accounts will be two lists once accounts exist. A later decision joins them.
- The paid issue adds a paid variant to the digest code and a test that the free issue can never carry it.
- Revisit when the public site and accounts ship, or if Substack publishes an official way to post.
