# 0083 — A deal's other dated steps never replace its closing date

- **Status:** accepted
- **Date:** 2026-11-25
- **Roadmap item:** Phase 1, "Change history and outcome agent", sub-item "A deal's other dated steps never replace its closing date" (added by PR #168)
- **Amends:** ADR 0018 part 5b (`safe_to_write`)

## Context

A merger or acquisition has several dated steps before it closes: the stockholder vote at a special meeting, the end of the antitrust (HSR) waiting period, the expiry of a tender offer, the record date for the vote. The extractor can give any of them as a `merger_acquisition` claim, and the writer treats every such claim as the same event as the open deal entry. A newer source of the same tier outranks the one behind the window (spec: Which claim wins), and when the new window overlaps the old one and moves it less than 6 months, nothing held it.

The Opus review of PR #164 found the case: NextCure's entry said the merger would "close in the fourth quarter of 2026". A newer 8-K said "The special meeting … to vote on the merger will be held on December 9, 2026". 9 December lies inside the fourth quarter, so the vote date replaced the close. ADR 0069 (PR #164) keeps a merger's non-overlapping dates waiting for a person; it does not cover a date inside the window.

## Decision

1. **Where.** In the writer's own check, `safe_to_write` (`catalyst.publish.sanity`), for a claim that outranks the source behind an open `merger_acquisition` entry's window and would change it. Plain code (`catalyst.publish.deal_steps`), no model calls. Both the check job and the writer use it, so the decision job holds what the writer holds.
2. **Closing words, tied to the deal.** Such a claim may move the window only when its quote ties closing words to the deal, the way ADR 0080 ties completion words to it: a deal word (merger, acquisition, transaction, deal, business combination, purchase, sale, divestiture, tender offer) and then, in the same sentence, close, closing, consummate or complete ("The merger is expected to close in December 2026"); or those words and then the deal word ("close the acquisition", "closing of the merger", "completion of the acquisition"). "Close of business" and "closing conditions" are not closing words. A quote without them is held: "the quote has no closing words for the deal, so its date may not be the closing".
3. **Another step is held by name.** A quote that names a stockholder vote (vote, special or annual meeting, stockholders' meeting), a waiting period (HSR, Hart-Scott-Rodino, waiting period, antitrust clearance), a tender offer's expiry, or a record date (record date, "of record", "close of business") is held: "the quote dates another step of the deal (a stockholder vote), not its closing". When the quote also has closing words, it is still held ("… beside its closing, so its date may not be the closing"): plain code cannot tell which of the two the date belongs to.
4. **A condition after the date words is not read.** "The merger is expected to close in December 2026, subject to approval … at a special meeting and the expiration of the HSR waiting period" names those steps only as conditions; from "subject to", "conditioned on", "contingent on" or "conditions including", when that comes after the claim's date words, the rest of the quote is left out. When the date words sit inside the condition ("… subject to the expiration of the HSR waiting period on November 30, 2026"), the whole quote is read, and the claim is held.
5. **No person's approval lifts it.** These holds are not on `PERSON_MAY_APPROVE`: approving would put the step's date in place of the close, the error this fixes. A later release that states the close plainly moves the window.
6. **What does not change.** Every other event type; a claim that loses to the source behind the window (it writes nothing, so it needs no hold); a claim that creates a new entry; every other check and hold. A real slip of the close inside the window ("now expected to close in December 2026") still updates the entry.

## Alternatives

- **Hold every newer merger date for a person.** Simplest, but a genuine narrowing of the close would wait every time, and the spec says the newest source of equal rank wins.
- **Give each step its own entry.** The roadmap item allows it "when it is worth one". It needs new event types or a step field, a migration, and extractor prompt and gold-set changes. Left for later; today the step's claim is held and nothing is lost.
- **Ask a model which step the date is for.** Spends money nightly and is harder to test than fixed words.
- **Let a person approve the hold.** Rejected (decision 5).

## Consequences

- An open deal's close window moves only on a quote about the close. A release that words the close without these words waits until a plainer one comes, or until the window ends: a missed update, never a wrong one.
- A quote that names a step beside the close is held even when its date is the close; the next release usually states the close alone.
- A held step claim shows in the review queue with its reason, as other writer holds do; no stored data changes and no migration is needed. A window that a step's date already moved is not moved back by this change, since the writer never unpublishes on its own: a newer close statement moves it, or a reviewed withdrawal (ADR 0070) takes the claim back.
