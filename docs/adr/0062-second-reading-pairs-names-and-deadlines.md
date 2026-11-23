# 0062 — The second reading: one drug written two ways, "by Q3" beside "Q3", and why an event was "not found"

- Status: accepted (owner decision 2026-11-23; the rules below are the agent's proposals)
- Amends: ADR 0018, part 2 (what agrees, and what a flagged second reading does); ADR 0034, decision 10 (two more fields on a held claim)

## Context

The owner, on 2026-11-23, about the 646 claims held back from publishing: "fix the checking reader and figure out what is wrong".

The checking reader is the second reading (ADR 0018, part 2). A second prompt reads the same release without seeing the first answer, and plain code compares the two. In the private export of 2026-11-16, 255 of the 646 held claims carry its reason: 206 say "the second reading did not find this event" and 49 say "the second reading differs".

### What the code proves about the 206 "not found"

"Not found" never meant only "the second reader listed no such event". It also covered every case where the second reader's own copy of the event was thrown away before the comparison, because only a second-reader event with no problem of its own may vouch.

| Group | Claims | Cause |
| --- | --- | --- |
| The claim has no date window | 109 | A missing window can never agree, so these could not pass whatever the second reader said. When the second reader copies the same date words, code cannot read them either, its copy gets the same problem, and it is thrown away. The date words are 84 different phrasings that the date reader does not know. Both prompts ask only for events with a time, so the second reader was asked about them. |
| The claim's window had already passed | 13 | The second reader's copy gets "date passed" and is thrown away. The claim is held by `window_not_past` anyway. |
| The page hides words from readers | 3 (7 with those above) | Every second-reader event from such a page is flagged and thrown away (ADR 0044). |
| A clean claim with a window | 81 | See below. |

The 81 clean claims:

- 45 are in 27 documents where the second reading confirmed nothing at all. In 8 of those documents two or more clean claims were all "not found" (26 claims; one document has 11 claims, 8 of them clean). Elsewhere the second reader misses about one clean claim in sixteen (36 of 586), so this is not chance: its reading of those documents gave nothing usable. Two causes fit the code, and the export cannot tell them apart. Both come from the same fact: **a stored claim keeps the checks of the day it was read.** The first full extraction ran on 2026-10-17, before the code read only visible text; that came on 2026-10-18 and was corrected on 2026-10-20 (ADR 0044, which found about one page in five hidden whole). A second reading made on 2026-10-18 or 19 saw such a page as empty, listed nothing, and is kept "so a re-run spends nothing". One made later flags every event from it, while the first reader's older claims carry no flag. Of the documents fetched on 2026-10-17, 24% of clean claims were "not found", against 11% for the next day's.
- 36 are in documents where other claims were confirmed. 10 of them share a sentence with, or repeat, another claim, and one second-reader event vouches for one claim only. The rest fit the second reader's measured quality: on the gold set it finds 85% of events and 95% of its quotes are the release's words (`evals/verify_catalysts/scores.json`). Some are right: the first reader had typed a trial start as an enrollment completion, and the second reader left it out.
- 23 of the 81 have no other reason, and 16 of those 23 are in the documents where nothing was confirmed.

The second reader gets the same text as the first, from the same function. Nothing is cut: a document over the limit is not read, and the claim then says the check "has not run", which no held claim does.

### What the 49 "differs" were

- 33 name the drug differently. 15 are one drug written two ways: a second name in brackets, a dose form, or the word "program". 8 have a trial's name where the drug belongs. 3 are a drug alone against the same drug in a combination. 7 name no drug in one or both readings.
- 17 differ in the window. 5 are a deadline beside its last period ("by Q3 2027" beside "Q3 2027"). 8 have no window in the claim and a real one in the second reading; for 5 of those, today's date reader reads the claim's words, but the claim was stored on 2026-10-17 with that day's reader. In the other 4 the claim's window opens later than the second reading's.
- 2 differ in type: a presentation against a readout.

## Decision

1. **A flagged second-reader event is compared, and never vouches.** It is paired like any other, so the reason now says "the second reading found this event, but its own checks flagged it:" with the short codes (`no_window`, `date_passed`, `source_hides_text`). The claim still fails. A clean event is always taken first.
2. **One drug written two ways agrees.** Besides spacing, case, and the reference tables:
   - a second name in brackets after a name and a space: "Zentra (zentocel)" agrees with "Zentra" and with "zentocel". At least one side's main name must be the match; two names that share only what is in brackets do not agree, since brackets also hold form words ("(ER)"). Brackets inside a name are part of it: "(R)-zentocel" is not "(S)-zentocel";
   - form and route words at either end ("gel 1%", "oral", "nasal spray", "program"), from a fixed list. A drug's forms share one drug record, so no published value changes. Short forms such as "IV" and bare numbers are never dropped;
   - a combination written with another joining word: "X + Y" agrees with "X in combination with Y", the same drugs in the same order;
   - the reference tables are asked about each of these names too, not only the whole string.
3. **"X with Y" is not "X".** A drug alone and in a combination are two programmes with their own dates. The planted-error test proved it: with this allowed, 5 planted errors passed (4 wrong drugs and 1 wrong date) and a correct claim beside one was held. It stays a disagreement.
4. **A deadline agrees with its last period.** The windows agree when both close on the same day and the second reading's opens no earlier than the claim's. "By Q3 2027" runs from the release to 30 September; "Q3 2027" closes the same day and only opens later, so the claim says nothing the second reading does not confirm. The other way round stays a disagreement: a claim that opens later than the second reading, or closes on another day. A plain "inside" rule was tried first; the planted-error test showed it let a whole year agree with four days inside it, so the closing day must match. Across sentences, only the very same window pairs, as before.
5. **A claim with no window stays held when the second reading has one.** The published window is always the claim's own, read by code from the claim's own date words. Borrowing the second reader's would publish a value the claim does not state. When the date reader learns the words, the claim gets its window and this check compares again for free.
6. **Types must be the same.** A presentation and a readout stay a disagreement: for data shown at a meeting the type check passes either, so the second reading is the only check that tells them apart.
7. **The run's summary counts each outcome**: agrees, differs, flagged, not found, and no second-reader events in the document. Counts only. A reading also keeps how many events the second reader proposed, not how many kept their quote, so lost quotes can be counted.
8. **The sealed export shows what the second reader said.** Each held claim with a second-reading reason gains `second_reading`: when the document was read, how many events were proposed, and each stored event's values with whether its quote overlaps the claim's. Never the second reader's quote. It is part of the held list, which leaves a job only sealed (ADR 0034, decision 9); summaries and logs carry counts only, and tests assert both.
9. **The export's `review_decision` is filled.** It was a placeholder that always gave nothing. It now gives each held claim's latest decision by a person: action, who, when, and the reasons seen.
10. **Held claims are compared again for free.** The second reader's events are stored as claims, and every run compares every read document again from them, with no model call. So these rules reach the claims already held on the next run. A test shows a stored failure turning into a pass without a new reading.

## Alternatives

- **Let the second reading's window stand in for a missing one.** Not chosen (decision 5).
- **Treat presentation and readout as one family.** Not chosen (decision 6); it would also gain nothing now, since both such claims have another reason.
- **Let one second-reader event vouch for two claims that agree with it.** It would release about 10 claims. Not chosen: two claims of one type, drug, and window in one release may be two trials, and the second reader then confirmed only one.
- **Read again every document whose second reading confirmed nothing.** Likely right for the documents first read on 2026-10-17, and it costs little. Not done here, because the export cannot yet show which of the two causes applies; decision 8 will show it in the next export, and the next run's counts (decision 7) will show it sooner.
- **Change the second reader's prompt.** Not in this change: a prompt change needs a paid run of the gold set. The proposal is below.

## Consequences

- Measured on the export of 2026-11-16: 17 of the 49 "differs" claims would agree now (13 by drug name, 4 by window). 1 of them has no other reason and would publish; the other 16 wait on other checks. The 206 "not found" keep failing; those whose second-reader copy was flagged will say so.
- The planted-error test gives the same counts as before (1,162 of 1,163 caught), so no floor moves.
- Still held, on purpose (32 of the 49): a trial's name in the drug field (8), a drug alone against a combination (3), no drug in a reading (7), a claim's window that opens later than the second reading's (4), a deadline whose last period began before the release (1), no window in the claim (8), another type (2); one claim is in two of these groups.
- Proposed prompt change for `verify_catalysts`, to be scored on the gold set before use: in the `drug` line, add "Give the drug's own name, never the name or number of a trial; if the sentence names only the trial, give the drug that trial tests, as the document names it." And in the first pass, add to the time expressions: "a time counted from now (this year, next quarter, later this year)" and "first half, H1, H2".
- Two things this change does not fix, for the lead: stored claims keep the problems and window of the day they were read, so a later fix to the date reader or the text reader never reaches them; and a kept second reading that found nothing is never made again.
