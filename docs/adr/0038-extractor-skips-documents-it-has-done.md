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

1. **A finished read is kept.** When the extractor reads a document and makes no claim (it proposed nothing, or every proposal's quote was not in the source), the job stores a row in `document_reading` (agent `extractor`, prompt id and version, model, the number of proposals), in the same transaction as any claims. A re-run skips a document that has a claim or a model's reading from the current prompt id and version. A new prompt version makes every document eligible again. A failed call stores nothing, so it runs again. The row's unique key keeps a re-run from adding a second one; a model's reading replaces an earlier skip of the same document.
2. **Copies no model reads.** These get no model call; each is stored as an extractor reading with a reason in the new `document_reading.skipped` column:
   - `not_text`: the stored copy's media type is not `text/html` or `text/plain` (parameters such as `charset` ignored). Checked before the copy is even fetched. Done for good, like a model's reading.
   - `no_readable_text`: the visible text (what the model would read) has fewer than 50 words. The shortest release that ever gave a catalyst had about 1,200.
   - `too_large`: over the role's input limit (decision 3).

   `no_readable_text` and `too_large` are **not** done (Opus review of PR #79, B1): a fixed text reader or a higher limit must reach those documents without anyone editing live data. Each run checks them again, which costs a stored-copy fetch and a parse but no model call. They come in a second batch, after up to `limit` new documents and with its own `limit`, so re-checks never take the place of a new document. The batch takes the skips checked longest ago first: each re-check moves its skip row's `read_at` to that run, so more than `limit` skipped documents all come round in turn (review of PR #79). A skip is counted, and a too-large one alerted, only in the run that first finds that document with that reason, whatever the model: the row's unique key includes the model, but a model change does not count or alert the same copy again (review of PR #79). A new reason for the same copy is counted. Later re-checks are counted as `documents_rechecked_still_skipped`.
3. **Input limit per role.** `ModelClient.run` refuses, before the spend-cap check and without calling or billing anything, a document whose estimate is over the role's limit, raising `InputTooLargeError`. The estimate is characters divided by 4, a cheap ordinary figure for English text (the cap check keeps its own, more cautious divide-by-3). The limit is `max_input_tokens` on the role in `config/models.toml` when set; otherwise a default in code, `DEFAULT_MAX_INPUT_TOKENS` in `catalyst.llm.config`: 40,000 for the extractor, about twice the largest release that ever gave a catalyst, and none for other roles. This pull request adds the setting without editing the owner's file. The job logs a structured `document_too_large` alert event (source key, estimate, limit) when it first finds one.
4. **The owner sees the counts.** The run summary adds `documents_skipped_not_text`, `documents_skipped_no_readable_text`, `documents_skipped_too_large` (each newly found this run), and `documents_rechecked_still_skipped` beside `documents_skipped_already_done`. `catalyst alerts` raises the extraction alert when a document was too large, and every extraction alert lists, in one plain line, how many documents were skipped without a model call and why. Other skips alone raise no alert.
5. **A change to the quote check ships a migration (review of PR #79).** A model's reading with proposals but no claim (`skipped IS NULL AND catalysts > 0`) is a read whose every quote failed the quote check. It is done, so a fixed quote check would never reach it. Any change to the quote check (`find_quote`, `catalyst_claims`, or `TEXT_VERSION`) must therefore ship, in the same pull request, a migration that deletes the extractor's readings with `skipped IS NULL AND catalysts > 0`, so those documents are read again. `tests/extract/test_run.py` pins a hash of that code and fails, naming this rule, when it changes.
6. **Migration.** `b7e2c4d9a1f3` adds the nullable `skipped` column with a check on its three values. Its downgrade deletes the skipped rows first (without the column they would look like a model's reading; the next run checks those copies again for free), then drops the column.

## Alternatives

- **A new table for extractor readings.** `document_reading` already means "one reader's pass over one document, kept even when it found nothing", and the second reading uses it the same way. A second table would repeat it.
- **Count every skip as done until the prompt version changes** (the first version of this pull request). Rejected in review: a fixed `visible_text` or a raised limit would never reach those documents, and the only way back would be deleting rows by hand in the live database.
- **A `reader_version` column holding the text reader's version and the limit**, so a change to either makes the skips stale. Exact, but `visible_text` has no version of its own (ADR 0012 versions only `document_text`), so a fix to it would also need a version bump nobody is forced to make. Re-checking is free and needs no such discipline; the second batch keeps it from crowding out new documents.
- **Keep the skips unrecorded, so each run checks them again.** Free, but the too-large alert would then fire every run and never close. Recorded, each is counted once.
- **The limit in `config/models.toml` only.** Clearer, but an owner-path change for a setting that only lowers spending. The code default can move into the file later with one line.
- **Count tokens exactly with the provider's counting endpoint.** Exact, but a network call per document, and an estimate is enough for a limit set at twice the largest useful release.
- **Stop the collector keeping PDF copies.** Also worth doing, but the job must not trust every collector to get this right, and stored copies already include PDFs.

## Consequences

- Repeat reads of finished documents stop from the second run after this merges. The first run still reads each zero-catalyst release once more, since no earlier run kept its reading.
- A release whose proposals all lost their quotes is now kept as a reading too (review finding O1), so it is not paid for again until the prompt changes. A call that keeps giving invalid output still fails and runs again each run; it is reported as a failed document, which alerts.
- When `visible_text` is fixed for the four releases that now read as empty but read fine on 2026-10-17, the next run reads them; nobody edits data. Raising the input limit works the same way.
- Each run re-checks every `no_readable_text` and `too_large` document, up to `limit` of them: a stored-copy fetch and a parse each, no model call. If that list grows past `limit`, each run takes the ones checked longest ago, so every one is checked within a few runs.
- Revisit the 40,000 limit if a real release with a catalyst comes near it; a `document_too_large` alert is the signal.
