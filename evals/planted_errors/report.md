# Planted-error test

We took the 20 hand-checked catalysts in the gold set, made copies with one realistic error each, and ran every check we have on each copy among the correct claims of its own press release. Where an error has several forms (every other trial, company, drug, or type), each form is its own copy. Plain code, no AI, the same result every run (seed 2026).

| Error planted | Planted | Caught | Caught without the second reading | Corrected (best case) | Missed |
| --- | ---: | ---: | ---: | ---: | ---: |
| Wrong quarter, half, month, or year (date words left as they were) | 48 | 48 | 48 | 48 | 0 |
| Another date from the same quote, stored consistently | 11 | 11 | 1 | 11 | 0 |
| Date moved into the past | 17 | 17 | 17 | 17 | 0 |
| FDA decision date outside its review window | 1 | 1 | 1 | 1 | 0 |
| Wrong drug (another in the same release) | 29 | 29 | 26 | 29 | 0 |
| Wrong drug (another company's drug) | 150 | 150 | 149 | 150 | 0 |
| Wrong event type | 221 | 221 | 17 | 221 | 0 |
| Wrong company | 153 | 153 | 153 | 0 | 0 |
| Wrong trial (another of the company's trials) | 383 | 383 | 383 | 0 | 0 |
| Wrong phase (registry phase contradicts the quote) | 128 | 128 | 128 | 0 | 0 |
| Quote from an unrelated sentence | 17 | 17 | 17 | 0 | 0 |
| Quote from another event with the same date words | 5 | 4 | 4 | 0 | 1 |
| **All errors** | **1163** | **1162** | **944** | **477** | **1** |

- **Caught**: at least one check failed the copy, so it goes to the review queue, not the site.
- **Caught without the second reading**: a rule check failed it too.
- **Corrected (best case)**: the second reading gave the right value, with its own quote. The stand-in reads every event right, so its quote is the original's by construction.
- **Missed**: every check passed the copy, so it would have published.
- **Wrong quarter, half, month, or year (date words left as they were)**: Only the stored window changes, so it no longer matches its own date words; a rule catches that by construction. The next row is the harder case.
- **Another date from the same quote, stored consistently**: Only the second reading catches these (a rule only when the other date has passed), and in this test it never misreads.
- **Wrong drug (another in the same release)**: The drug check catches these unless the quote names both drugs (one Spyre sentence names SPY072 alone and with IL-17A/F); then only the second reading does, and in this test it never misreads.
- **Wrong phase (registry phase contradicts the quote)**: Built to be exactly what the phase check looks for, so this row shows that the check works, not how often such errors are caught.
- **Quote from another event with the same date words**: The drug check (`drug_named_near_quote`) catches every copy whose sentence is about another drug.

The second reading in this test is a stand-in that always reads the release correctly, so "caught" and "corrected" are best cases; the real second reading can misread too.

**Correct records wrongly flagged:** 2 of 19 correct claims checked on their own, and 0 of 3486 checks of correct claims beside a planted copy. A claim that is flagged even when correct goes to review as it is, so no error is planted in it:

- Mirum Pharmaceuticals, Inc., Brelovitug, "Q3": left out; the extractor flags it (no_window).
- Spyre Therapeutics, Inc., no drug named, "2027": no drug is named (the trial tests several), so the second reading cannot confirm it.
- Spyre Therapeutics, Inc., no drug named, "2027": no drug is named (the trial tests several), so the second reading cannot confirm it.

## Missed cases

**Quote from another event with the same date words** (1 of 5 missed). Why: the other sentence is about the same drug: SPY072's other sub-study, whose sentence comes just after the drug's name and holds the same date words. So the date and drug checks pass, and the second reading pairs two readings in different sentences when their type, window, and drug agree. The stored values (SPY072, topline data, Q4 2026) are true of that sentence too.

- Spyre Therapeutics, Inc., SPY072, "fourth quarter of 2026": 1 of 1 missed, for example quote swapped for "Topline proof-of-concept data are expected in the fourth quarter of 2026."

Checks run: advisory_meeting_noticed, company_named_in_source, date_words_in_quote, drug_named_near_quote, extractor_checks, has_window, independent_source, names_matched, pdufa_date_plausible, phase_matches_registry, second_reading_agrees, trial_named_in_quote, window_matches_words, window_not_past. Every gold release is an SEC filing, which the publish rule lets stand alone, so independent_source passes every claim here and can catch nothing yet.

Made by `uv run catalyst planted-errors --out evals/planted_errors/report.md --scores evals/planted_errors/scores.json` (ADR 0018, part 4).
