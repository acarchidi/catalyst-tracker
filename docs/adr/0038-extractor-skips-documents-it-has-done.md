# 0038 — The extractor skips documents it has finished, and copies it cannot use

- **Status:** accepted
- **Date:** 2026-10-20
- **Roadmap item:** none; a cost fix from live run 39321573893 (2026-10-19), during Phase 1, "Verification checks and publish gate" (CP4)
- **Amends:** ADR 0013 (which documents a re-run skips)

## Context

ADR 0013 made a re-run skip a document only when it already had a claim from the current prompt. A press release with no catalysts never gets a claim, so every run read it again. Run 39321573893 re-read 153 releases that had given no catalysts before and spent $1.84 of the extractor's $2.00 cap for nothing. Across runs 7 to 9, repeat reads found no catalyst at all.

Three kinds of copy were also paid calls that could never give a catalyst:

- A PDF exhibit (`sec:0001193125-26-405954:aldx-ex99_1.pdf`) was decoded as raw bytes: 482,163 input tokens, $0.96 a run, paid in runs 7, 8, 9 and 39321573893. The collector keeps every EX-99 file, PDF copies included.
- 36 of the 153 had no readable text, about 18 input tokens each.
- Nothing limited a document's size before the call; only the daily cap did. The largest release that ever gave a catalyst was 19,089 tokens.

The size limit belongs with each role's model settings, but `config/models.toml` is an owner-only path (`scripts/merge_gate.py`), so changing it would take this fix out of the ordinary review and merge.

## Decision

1. **A finished read is kept.** When the extractor reads a document and proposes no catalysts, the job stores a row in `document_reading` (agent `extractor`, prompt id and version, model, 0 catalysts), in the same transaction as any claims. A re-run skips a document that has an extractor reading or a claim from the current prompt id and version. A new prompt version makes every document eligible again. A failed call stores nothing, so it runs again. The row's unique key keeps a re-run from adding a second one.
2. **Copies no model reads.** These get no model call; each is stored as an extractor reading with a reason in the new `document_reading.skipped` column, and counted in the run summary:
   - `not_text`: the stored copy's media type is not `text/html` or `text/plain` (parameters such as `charset` ignored). Checked before the copy is even fetched.
   - `no_readable_text`: the visible text (what the model would read) has fewer than 50 words. The shortest release that ever gave a catalyst had about 1,200.
   - `too_large`: over the role's input limit (decision 3).
3. **Input limit per role.** `ModelClient.run` refuses, before the spend-cap check and without calling or billing anything, a document whose estimate is over the role's limit, raising `InputTooLargeError`. The estimate is characters divided by 4, a cheap ordinary figure for English text (the cap check keeps its own, more cautious divide-by-3). The limit is `max_input_tokens` on the role in `config/models.toml` when set; otherwise a default in code, `DEFAULT_MAX_INPUT_TOKENS` in `catalyst.llm.config`: 40,000 for the extractor, about twice the largest release that ever gave a catalyst, and none for other roles. This pull request adds the setting without editing the owner's file. The job logs a structured `document_too_large` event (source key, estimate, limit).
4. **The owner sees the counts.** The run summary adds `documents_skipped_not_text`, `documents_skipped_no_readable_text`, and `documents_skipped_too_large` beside `documents_skipped_already_done`. `catalyst alerts` raises the extraction alert when a document was too large, and every extraction alert lists, in one plain line, how many documents were skipped without a model call and why. Other skips alone raise no alert.
5. **Migration.** `b7e2c4d9a1f3` adds the nullable `skipped` column with a check on its three values. Its downgrade deletes the skipped rows first (without the column they would look like a model's reading; the next run checks those copies again for free), then drops the column.

## Alternatives

- **A new table for extractor readings.** `document_reading` already means "one reader's pass over one document, kept even when it found nothing", and the second reading uses it the same way. A second table would repeat it.
- **Keep the not-text and too-large copies unrecorded, so each run checks them again.** Free, but the too-large alert would then fire every run and never close. Recorded, each is counted once; a new prompt version checks it again.
- **The limit in `config/models.toml` only.** Clearer, but an owner-path change for a setting that only lowers spending. The code default can move into the file later with one line.
- **Count tokens exactly with the provider's counting endpoint.** Exact, but a network call per document, and an estimate is enough for a limit set at twice the largest useful release.
- **Stop the collector keeping PDF copies.** Also worth doing, but the job must not trust every collector to get this right, and stored copies already include PDFs.

## Consequences

- Repeat reads of finished documents stop from the second run after this merges. The first run still reads each zero-catalyst release once more, since no earlier run kept its reading.
- A release whose proposals all lost their quotes makes no claim and no reading, so it is still read again; that is unchanged.
- A `no_readable_text` reading stays until the prompt version changes. When `visible_text` is fixed for the four releases that now read as empty but read fine on 2026-10-17, those rows should be deleted so the releases are read again.
- Revisit the 40,000 limit if a real release with a catalyst comes near it; a `document_too_large` alert is the signal.
