# 0080 — A stored completion document closes an open merger or acquisition entry

- **Status:** accepted
- **Date:** 2026-11-25
- **Roadmap item:** none; a root-cause fix after the first weekly issue (owner request, 2026-11-25)
- **Amends:** ADR 0018 part 5b (the writer may now close a deal entry, from plain code only), ADR 0066 decision 11 (a fully read filing may get a claim later, from this rule) and the collector table of ADR 0005 and ADR 0046 (an 8-K reporting Item 2.01 or 5.01 also keeps its main document)

## Context

In the first weekly issue, 4 of 9 stale entries were merger or acquisition catalysts whose "expected to close" window was still shown after the deal had closed:

| Entry | Closed | Completion document |
| --- | --- | --- |
| PTC, ST-920 | 21 Sep 2026 | 8-K, Item 2.01 |
| Viatris and Pacira | 17 Nov 2026 | "Viatris Completes Acquisition of Pacira BioSciences" release |
| Harrow, TYRVAYA | 1 Oct 2026 | 8-K, Item 2.01 |
| InMed and Mentari | 6 Nov 2026 | "completes acquisition" release |

They were taken out by hand through `config/claim_withdrawals.toml` (ADR 0070). The cause: the extractor reads forward-looking statements only, and the rule check `window_matches_words` fails a window that ended before its own release, so a "completed the acquisition" release never makes a claim that updates the entry. ADR 0069 ("What this does not do") named this gap. Loosening either check would weaken a verification check, which AGENTS.md forbids.

A filing states a completed deal plainly. Item 2.01 of Form 8-K is "Completion of Acquisition or Disposition of Assets"; Item 5.01 is "Changes in Control of Registrant". The SEC filing list names each 8-K's items. The company's release says the deal "was completed", "closed" or "was consummated". These are fixed phrasings, so plain code can read them; no model call is needed.

## Decision

1. **Who closes.** The writer (`publish_claims`), inside its lock and transaction, right after the reviewed withdrawals: `catalyst.publish.closed_deals.close_deals`. Plain code, no model calls, no new spending.
2. **What it looks at.** Every open (upcoming or slipped) catalyst of type `merger_acquisition` that the gate wrote. For each, the stored documents linked to **the same company**, published **after** the source behind the catalyst's current window, whose copy is still kept:
   - an **8-K main document** whose visible text holds the Item 2.01 or Item 5.01 heading;
   - an **8-K Exhibit 99** (the company's own release filed with the SEC).
3. **What counts as completion.** One visible sentence (as `strict_text.visible_text` reads the page, so hidden text never counts) that says the deal happened: "completed / completes / closed / consummated / finalized (its previously announced) acquisition / merger / transaction / sale / purchase / business combination / tender offer"; or "the merger … was / has been completed, closed, consummated" or "became effective"; or "announced the completion / closing of its acquisition". Nothing before the statement (and, for the passive, nothing between the deal word and its verb) may make it a plan, a condition or a denial: "expected", "anticipates", "will", "would", "may", "should", "to be", "upon", "once", "if", "until", "subject to", "before", "after", "following", "not", "agreement to", "proposed" and the like. A heading ("Item 2.01 Completion of Acquisition") is not a sentence that counts.
4. **Matching the counterparty or asset.** The names are the capitalised words and codes ("Mentari", "TYRVAYA", "ST-920") in the quotes and proposed titles of the extractor claims behind the catalyst, less the company's own names (its SEC name, display name, past names and tickers) and less common deal, corporate and date words. The completion sentence must name at least one of them. **A catalyst whose claims name no counterparty or asset is never closed**: plain code cannot tell which deal a bare "the transaction" is. When unsure, nothing happens.
5. **What it writes.** For a match, in one transaction:
   - a claim of its own: agent `deal_closer`, prompt id `closed_deals`, version `1.0.0`, provider and model `plain code`; the sentence as its **verbatim quote**, placed by `find_quote` in the stored copy's text (`text_version` recorded, like every claim); its value names the status, the matched names, the 8-K item and the document type; its field is `deal_closed:<catalyst id>`, so one sentence can close two entries of one deal;
   - a `status` change from the open status to `resolved`, citing that claim, later than the catalyst's last change;
   - the catalyst's status set to `resolved`, the existing status for an event that happened. **No migration.**
   The read layer then shows the completion document and its quote as the source of the status, and the closing in the entry's history.
6. **It only closes.** It never creates or moves a catalyst and never publishes anything new: the writer places only extractor claims, so a `deal_closer` claim is never a candidate, and no check, decision, second reading or review row is made for it.
7. **Out of the issue.** A resolved catalyst is in no section of the weekly issue (Due, Open, Later, moved or new) and in no calendar read, which list only upcoming and slipped catalysts (tested).
8. **The deal does not come back.** The writer's index keeps closed deals aside, as it keeps withdrawn ones (ADR 0070): a claim about the same company, type, drugs and trials from a document published before the closing was recorded is held as "the deal already closed; an older release cannot reopen it". A catalyst resolved any other way is left out, as before.
9. **Safe to re-run.** A closed catalyst is open no more, so a re-run reads nothing for it and writes nothing; the claim insert also ignores a duplicate.
10. **The collector keeps the 8-K itself when it reports Item 2.01 or 5.01.** The filing list's `items` field is now read for every filing. Such an 8-K's main document (type `8-K`) is stored beside its Exhibit 99s. No reader reads it (it is neither a press release nor a filing of ADR 0066), so it costs no model call, and the store clean-up never deletes it. Filings already stored are not fetched again, so for past deals only their Exhibit 99 release is available.

## Alternatives

- **Teach the extractor past events.** A prompt change needs new gold-set entries and a new score (AGENTS.md) and spends model money nightly; the phrasing is fixed enough for plain code.
- **Loosen `window_matches_words` so a passed window can publish.** Weakens a verification check; forbidden.
- **A new status `closed`.** `resolved` already means "the event happened" (spec: Status), and needs no migration.
- **An `outcome` row.** Its result categories (approved, endpoint met, …) have none for a closed deal; adding one is the outcome agent's work, with its own migration. The status change already carries the source and quote.
- **Close on a company match alone.** A company often has more than one deal; closing the wrong one would hide a live entry. Rejected for "when unsure, do nothing".
- **Keep withdrawing by hand.** Each one needs a person and a pull request.

## Consequences

- Of the four cases, the shapes tested close all four: the two Item 2.01 8-Ks, once their main document is stored, and the two releases. On live data a past case closes only if its completion document is stored for the same company, names the counterparty or asset its claims name, and the copy is still kept; the PTC and Harrow 8-Ks were stored before this change, so for them only an Exhibit 99 release saying the same can close the entry. The four withdrawals in `config/claim_withdrawals.toml` stay as they are.
- An entry whose claims name no counterparty, or a release that words the completion in a way the patterns miss, stays open until its window ends, as today: a missed closing, never a wrong one.
- A closing claim keeps its document's copy, since the copy now backs a claim (ADR 0066 decision 10).
- The writer reads the copies of a company's 8-Ks published after an open deal's source on each run until the deal closes or its window ends; there are few open deal entries, so this is small.
