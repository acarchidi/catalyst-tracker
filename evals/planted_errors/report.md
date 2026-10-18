# Planted-error test

We took the 20 hand-checked catalysts in the gold set, made copies with one realistic error each, and ran every check we have on each copy among the correct claims of its own press release. Where an error has several forms (every other trial, company, drug, or type), each form is its own copy. Plain code, no AI, the same result every run (seed 2026).

| Error planted | Planted | Caught | Caught without the second reading | Corrected (best case) | Missed |
| --- | ---: | ---: | ---: | ---: | ---: |
| Wrong quarter, half, month, or year (date words left as they were) | 48 | 48 | 48 | 48 | 0 |
| Another date from the same quote, stored consistently | 11 | 11 | 1 | 11 | 0 |
| Date moved into the past | 17 | 17 | 17 | 17 | 0 |
| FDA decision date outside its review window | 1 | 1 | 1 | 1 | 0 |
| Wrong drug (another in the same release) | 29 | 29 | 0 | 29 | 0 |
| Wrong drug (another company's drug) | 150 | 150 | 34 | 150 | 0 |
| Wrong event type | 221 | 221 | 17 | 221 | 0 |
| Wrong company | 153 | 72 | 72 | 0 | 81 |
| Wrong trial (another of the company's trials) | 383 | 128 | 128 | 0 | 255 |
| Wrong phase (registry phase contradicts the quote) | 128 | 128 | 128 | 0 | 0 |
| Quote from an unrelated sentence | 17 | 17 | 17 | 0 | 0 |
| Quote from another event with the same date words | 5 | 0 | 0 | 0 | 5 |
| **All errors** | **1163** | **822** | **463** | **477** | **341** |

- **Caught**: at least one check failed the copy, so it goes to the review queue, not the site.
- **Caught without the second reading**: a rule check failed it too.
- **Corrected (best case)**: the second reading gave the right value, with its own quote. The stand-in reads every event right, so its quote is the original's by construction.
- **Missed**: every check passed the copy, so it would have published.
- **Wrong quarter, half, month, or year (date words left as they were)**: Only the stored window changes, so it no longer matches its own date words; a rule catches that by construction. The next row is the harder case.
- **Another date from the same quote, stored consistently**: Only the second reading catches these (a rule only when the other date has passed), and in this test it never misreads.
- **Wrong drug (another in the same release)**: No rule catches these; only the second reading does, and in this test it never misreads.
- **Wrong phase (registry phase contradicts the quote)**: Built to be exactly what the phase check looks for, so this row shows that the check works, not how often such errors are caught.

The second reading in this test is a stand-in that always reads the release correctly, so "caught" and "corrected" are best cases; the real second reading can misread too.

**Correct records wrongly flagged:** 2 of 19 correct claims checked on their own, and 0 of 3486 checks of correct claims beside a planted copy. A claim that is flagged even when correct goes to review as it is, so no error is planted in it:

- Mirum Pharmaceuticals, Inc., Brelovitug, "Q3": left out; the extractor flags it (no_window).
- Spyre Therapeutics, Inc., no drug named, "2027": no drug is named (the trial tests several), so the second reading cannot confirm it.
- Spyre Therapeutics, Inc., no drug named, "2027": no drug is named (the trial tests several), so the second reading cannot confirm it.

## Missed cases

**Wrong company** (81 of 153 missed). Why: no check compares the linked company with the company that put out the release, and the second reading does not read the company. Only a company's own code name (such as SPY072) gives the error away; a generic or brand name matches under any company.

- TG THERAPEUTICS, INC., subcutaneous BRIUMVI, "year-end 2026/first quarter 2027": 9 of 9 missed, for example linked to ACADIA PHARMACEUTICALS INC
- TG THERAPEUTICS, INC., azer-cel, "second half of 2026": 9 of 9 missed, for example linked to ACADIA PHARMACEUTICALS INC
- Mirum Pharmaceuticals, Inc., volixibat, "H1 2027": 9 of 9 missed, for example linked to ACADIA PHARMACEUTICALS INC
- Mirum Pharmaceuticals, Inc., Brelovitug, "Q4 2026": 9 of 9 missed, for example linked to ACADIA PHARMACEUTICALS INC
- Mirum Pharmaceuticals, Inc., LIVMARLI, "Q4 2026": 9 of 9 missed, for example linked to ACADIA PHARMACEUTICALS INC
- Mirum Pharmaceuticals, Inc., volixibat, "Q1 2027": 9 of 9 missed, for example linked to ACADIA PHARMACEUTICALS INC
- Mirum Pharmaceuticals, Inc., zilurgisertib, "September 26, 2026": 9 of 9 missed, for example linked to ACADIA PHARMACEUTICALS INC
- ACADIA PHARMACEUTICALS INC, remlifanserin, "November 16-19, 2026": 9 of 9 missed, for example linked to AEON Biopharma, Inc.
- Spyre Therapeutics, Inc., SPY072 + IL-17A/F, "late 2027 or early 2028": 9 of 9 missed, for example linked to ACADIA PHARMACEUTICALS INC

**Wrong trial (another of the company's trials)** (255 of 383 missed). Why: only the phase check reads the linked trial, so the error shows only when the quote states a phase that the other trial does not have. The second reading does not compare trials (ADR 0018, part 2).

- TG THERAPEUTICS, INC., subcutaneous BRIUMVI, "year-end 2026/first quarter 2027": 14 of 40 missed, for example linked to trial NCT02301156 (GENUINE), Phase 3
- TG THERAPEUTICS, INC., azer-cel, "second half of 2026": 19 of 40 missed, for example linked to trial NCT01647971, Phase 1/2
- AEON Biopharma, Inc., ABP-450, "fourth quarter of 2026": 4 of 4 missed, for example linked to trial NCT04845178, Phase 2
- Spyre Therapeutics, Inc., SPY003, "September 2026": 8 of 8 missed, for example linked to trial NCT06448247, Phase 1
- Spyre Therapeutics, Inc., SPY072, "September 2026": 8 of 8 missed, for example linked to trial NCT06448247, Phase 1
- Spyre Therapeutics, Inc., SPY072, "fourth quarter of 2026": 8 of 8 missed, for example linked to trial NCT06448247, Phase 1
- Spyre Therapeutics, Inc., SPY072, "fourth quarter of 2026": 8 of 8 missed, for example linked to trial NCT06448247, Phase 1
- Mirum Pharmaceuticals, Inc., volixibat, "H1 2027": 39 of 39 missed, for example linked to trial NCT00007020, Phase 3
- Mirum Pharmaceuticals, Inc., Brelovitug, "Q4 2026": 16 of 39 missed, for example linked to trial NCT00007020, Phase 3
- Mirum Pharmaceuticals, Inc., LIVMARLI, "Q4 2026": 16 of 39 missed, for example linked to trial NCT00007020, Phase 3
- Mirum Pharmaceuticals, Inc., volixibat, "Q1 2027": 23 of 38 missed, for example linked to trial NCT01903460 (IMAGO), Phase 2
- Mirum Pharmaceuticals, Inc., zilurgisertib, "September 26, 2026": 39 of 39 missed, for example linked to trial NCT00007020, Phase 3
- ACADIA PHARMACEUTICALS INC, remlifanserin, "November 16-19, 2026": 21 of 41 missed, for example linked to trial NCT00087542, Phase 2
- Spyre Therapeutics, Inc., SPY072, "Q4 2026": 8 of 8 missed, for example linked to trial NCT06448247, Phase 1
- Spyre Therapeutics, Inc., SPY072, "Q4 2026": 8 of 8 missed, for example linked to trial NCT06448247, Phase 1
- Spyre Therapeutics, Inc., SPY072 + IL-17A/F, "late 2027 or early 2028": 8 of 8 missed, for example linked to trial NCT06448247, Phase 1
- Spyre Therapeutics, Inc., SPY003, "Sept 2026": 8 of 8 missed, for example linked to trial NCT06448247, Phase 1

**Quote from another event with the same date words** (5 of 5 missed). Why: the other sentence holds the same date words, so the date check passes, and the second reading pairs two readings in different sentences when their type, window, and drug agree. No check asks whether the quote names the claim's drug.

- Spyre Therapeutics, Inc., SPY003, "September 2026": 1 of 1 missed, for example quote swapped for "Topline proof-of-concept data are expected in September 2026."
- Spyre Therapeutics, Inc., SPY072, "September 2026": 1 of 1 missed, for example quote swapped for "Enrollment for Part A has completed, with SPY001 and SPY002 Part A topline induction data "
- Spyre Therapeutics, Inc., SPY072, "fourth quarter of 2026": 1 of 1 missed, for example quote swapped for "Topline proof-of-concept data are expected in the fourth quarter of 2026."
- Mirum Pharmaceuticals, Inc., Brelovitug, "Q4 2026": 1 of 1 missed, for example quote swapped for "•LIVMARLI EXPAND Phase 3 study in cholestatic pruritus due to additional rare cholestatic "
- Mirum Pharmaceuticals, Inc., LIVMARLI, "Q4 2026": 1 of 1 missed, for example quote swapped for "•Brelovitug AZURE-1 and AZURE-4 Phase 3 studies in chronic hepatitis delta virus (HDV) top"

Checks run: advisory_meeting_noticed, date_words_in_quote, extractor_checks, has_window, independent_source, names_matched, pdufa_date_plausible, phase_matches_registry, second_reading_agrees, window_matches_words, window_not_past. Every gold release is an SEC filing, which the publish rule lets stand alone, so independent_source passes every claim here and can catch nothing yet.

Made by `uv run catalyst planted-errors --out evals/planted_errors/report.md --scores evals/planted_errors/scores.json` (ADR 0018, part 4).
