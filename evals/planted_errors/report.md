# Planted-error test

We took the 20 hand-checked catalysts in the gold set, made copies with one realistic error each, and ran every check we have on each copy among the correct claims of its own press release. Plain code, no AI, the same result every run.

| Error planted | Planted | Caught | Caught without the second reading | Corrected | Missed |
| --- | ---: | ---: | ---: | ---: | ---: |
| Wrong quarter, half, month, or year | 17 | 17 | 17 | 17 | 0 |
| Date moved into the past | 17 | 17 | 17 | 17 | 0 |
| FDA decision date outside its review window | 1 | 1 | 1 | 1 | 0 |
| Wrong drug | 17 | 17 | 3 | 17 | 0 |
| Wrong event type | 17 | 17 | 0 | 17 | 0 |
| Wrong company | 17 | 8 | 8 | 0 | 9 |
| Wrong trial (another of the company's trials) | 17 | 1 | 1 | 0 | 16 |
| Wrong phase (registry phase contradicts the quote) | 6 | 6 | 6 | 0 | 0 |
| Quote that does not support the value | 17 | 17 | 17 | 0 | 0 |
| **All errors** | **126** | **101** | **70** | **69** | **25** |

- **Caught**: at least one check failed the copy, so it goes to the review queue, not the site.
- **Caught without the second reading**: a rule check failed it too.
- **Corrected**: the second reading gave the right value, with its own quote.
- **Missed**: every check passed the copy, so it would have published.

The second reading in this test is a stand-in that always reads the release correctly, so "caught" and "corrected" are best cases; the real second reading can misread too.

**Correct records wrongly flagged:** 2 of 19 correct claims checked on their own, and 0 of 396 checks of correct claims beside a planted copy. A claim that is flagged even when correct goes to review as it is, so no error is planted in it:

- Mirum Pharmaceuticals, Inc., Brelovitug, "Q3": left out; the extractor flags it (no_window).
- Spyre Therapeutics, Inc., no drug named, "2027": second_reading_agrees: the second reading differs: drug none, second reading none.
- Spyre Therapeutics, Inc., no drug named, "2027": second_reading_agrees: the second reading differs: drug none, second reading none.

## Missed cases

**Wrong company** (9 missed). Why: no check compares the linked company with the company that put out the release, and the second reading does not read the company. Only a company's own code name (such as SPY072) gives the error away; a generic or brand name matches under any company.

- TG THERAPEUTICS, INC., subcutaneous BRIUMVI, "year-end 2026/first quarter 2027": linked to Moderna, Inc.
- TG THERAPEUTICS, INC., azer-cel, "second half of 2026": linked to Mirum Pharmaceuticals, Inc.
- Mirum Pharmaceuticals, Inc., volixibat, "H1 2027": linked to Spyre Therapeutics, Inc.
- Mirum Pharmaceuticals, Inc., Brelovitug, "Q4 2026": linked to Aptevo Therapeutics Inc.
- Mirum Pharmaceuticals, Inc., LIVMARLI, "Q4 2026": linked to Spyre Therapeutics, Inc.
- Mirum Pharmaceuticals, Inc., volixibat, "Q1 2027": linked to Coherus Oncology, Inc.
- Mirum Pharmaceuticals, Inc., zilurgisertib, "September 26, 2026": linked to Spyre Therapeutics, Inc.
- ACADIA PHARMACEUTICALS INC, remlifanserin, "November 16-19, 2026": linked to Mirum Pharmaceuticals, Inc.
- Spyre Therapeutics, Inc., SPY072 + IL-17A/F, "late 2027 or early 2028": linked to TG THERAPEUTICS, INC.

**Wrong trial (another of the company's trials)** (16 missed). Why: only the phase check reads the linked trial, so the error shows only when the quote states a phase that the other trial does not have. The second reading does not compare trials (ADR 0018, part 2); part 3's cross-source checks should close this gap.

- TG THERAPEUTICS, INC., subcutaneous BRIUMVI, "year-end 2026/first quarter 2027": linked to trial NCT07220252, Phase 2/3
- AEON Biopharma, Inc., ABP-450, "fourth quarter of 2026": linked to trial NCT04849988, Phase 2
- Spyre Therapeutics, Inc., SPY003, "September 2026": linked to trial NCT06448247, Phase 1
- Spyre Therapeutics, Inc., SPY072, "September 2026": linked to trial NCT07148414 (SKYWAY-RD), Phase 2
- Spyre Therapeutics, Inc., SPY072, "fourth quarter of 2026": linked to trial NCT06448247, Phase 1
- Spyre Therapeutics, Inc., SPY072, "fourth quarter of 2026": linked to trial NCT06873724, Phase 1
- Mirum Pharmaceuticals, Inc., volixibat, "H1 2027": linked to trial NCT07290257, Phase 4
- Mirum Pharmaceuticals, Inc., Brelovitug, "Q4 2026": linked to trial NCT00007020, Phase 3
- Mirum Pharmaceuticals, Inc., LIVMARLI, "Q4 2026": linked to trial NCT04530994, no phase on file
- Mirum Pharmaceuticals, Inc., volixibat, "Q1 2027": linked to trial NCT02117713 (IMAGINE-II), Phase 2
- Mirum Pharmaceuticals, Inc., zilurgisertib, "September 26, 2026": linked to trial NCT02287779, Phase 1
- ACADIA PHARMACEUTICALS INC, remlifanserin, "November 16-19, 2026": linked to trial NCT05008835, Phase 2
- Spyre Therapeutics, Inc., SPY072, "Q4 2026": linked to trial NCT07766005, Phase 2
- Spyre Therapeutics, Inc., SPY072, "Q4 2026": linked to trial NCT07652294 (SKYLINE-LTE), Phase 2
- Spyre Therapeutics, Inc., SPY072 + IL-17A/F, "late 2027 or early 2028": linked to trial NCT07012395 (SKYLINE-UC), Phase 2
- Spyre Therapeutics, Inc., SPY003, "Sept 2026": linked to trial NCT07148414 (SKYWAY-RD), Phase 2

Checks run: date_words_in_quote, extractor_checks, has_window, names_matched, pdufa_date_plausible, phase_matches_registry, second_reading_agrees, window_matches_words, window_not_past.

Made by `uv run catalyst planted-errors --out evals/planted_errors/report.md --scores evals/planted_errors/scores.json` (ADR 0018, part 4).
