# 0066 — Every stored 10-Q, 10-K and 8-K exhibit is read, part by part; a read copy that backs no claim is deleted

- **Status:** accepted
- **Date:** 2026-11-24
- **Roadmap item:** none; a catch-up of Phase 1 "Extractor agents" and an upkeep change to the snapshot store, at the owner's request
- **Amends:** ADR 0008 (consequence "Copies are never deleted by this code"), ADR 0038 (which documents a run reads), ADR 0041 decision 9 (what the sample checker reads), ADR 0046 decision 8 (copies nothing reads are kept)

## Context

The owner (2026-11-23): "read 100% of [stored 10-Q, 10-K and other 8-K exhibits], and make sure all additional ones are read, we have no need to store them once they are fully read. clean up all technical messes preventatively please."

The SEC collector stores the main document of every 10-Q and 10-K, and every Exhibit 99 of an 8-K (ADR 0005, ADR 0046's table). Only `8-K EX-99.1` was ever read (`PRESS_RELEASE_TYPES`). An 8-K whose only press release is labelled `EX-99`, with no number, was never read either.

Facts that shape the design:

- A 10-K is 300,000 to 600,000 characters of visible text (75,000 to 150,000 tokens); a 10-Q about a third of that. The extractor's input limit is 40,000 estimated tokens (ADR 0038, decision 3), the sample checker's 30,000 (ADR 0041, decision 9), and the verifier has none. Most of a filing (financial statements, risk factors, exhibit lists, signatures) never states a dated future event.
- Companies write about their programs and plans in the Business section of a 10-K (the pipeline), in Management's discussion and analysis (with its "recent developments") in both, and sometimes in Other information.
- Every inline XBRL filing hides an `ix:header` (data tags) in a `display:none` block, as the SEC's filer manual requires. The hiding check (`hidden_letters`, ADR 0018 part 5c) would flag every claim from every filing as "the source hides words", so every one would wait for a person.
- The loose page reader (ADR 0044) gives up after 5 seconds and falls back to the strict one; on a large filing which one answers can change from run to run, so the visible text, and any parts cut from it, would too.
- Copies are stored by hash, so two documents with the same bytes share one copy (ADR 0008, decision 1). Nothing but `snapshots-verify` reads a copy that backs no claim: the publish gate, the checks, the second reading, the sample check and the export read only the copies of documents with claims.
- Spending: until 2026-12-08 the extractor's cap is $8 and the verifier's $4, raised for the press-release backfill on the owner's promotional credit; `backfill-status` keeps every model call since 2026-11-24 plus one more day within $96 (ADR 0065). From 2026-12-09 both are $2.

## Decision

1. **What is read.** `10-Q`, `10-K`, and every `8-K EX-99…` other than `8-K EX-99.1` (`is_filing`). S-3 and 424B offerings stay unread: they rarely state a catalyst, and the dilution flags of the spec will read them for their own fields.
2. **Same reader, same claims.** The extractor reads them with the same prompt version (`extract_catalysts` 1.0.0) and the same model input (the publication date, then the text), so the gold-set scores and floors are unchanged. Each proposal becomes the same claim as a press release's: its quote placed in the whole copy's `document_text`, checked against the whole visible text, with the same problems, and then the same matching, rule checks, second reading, decisions and publish gate. Nothing in the checks or the gate changes.
3. **Sections, then parts** (`catalyst.extract.sections`). For a 10-Q or 10-K, plain code finds the `Item` headings (title on the line or the next) and keeps Business, Management's discussion and analysis, and Other information; a table of contents names them too, so for each the longest occurrence is the section. A filing with none of these, and every exhibit, is read whole. Each section is cut at line ends (sentence ends inside a longer line) into parts of about equal size, none over 80,000 characters (about 20,000 tokens: about the largest release that ever gave a catalyst, half the input limit). Each part is one call. A part is a slice of the visible text, known by the SHA-256 of its words.
4. **Fully read, and resumed.** Each part read is a `document_part_reading` row, stored in one transaction with its claims. Once every part is read, `source_document.fully_read_at` is set in the same transaction as the last part's claims, with an extractor `document_reading` (the number of events proposed in all parts). A fully read filing is never read again, whatever the prompt version: the owner wants each read once, and its copy may be gone. A run that stops (time, cap, an error) goes on with the next part; started filings come first, then the newest. A PDF or an almost empty copy is kept as a reading with its reason, as for press releases (ADR 0038), and is never fully read.
5. **Steady readings.** Filings are read with the strict page reader alone (`strict_text.visible_text`), whose result does not depend on a clock, so parts come out the same on every run. The hiding check compares the visible text with the copy's text without its `ix:header` (`hide_text`); any other hidden word still holds every claim of that filing (a test).
6. **The second reading.** Once a filing is fully read and has claims, the verifier reads only the parts that hold an extractor claim's quote, each part a call kept like the extractor's, and its reading of the document is stored when they are done; the usual comparison then runs. A filing is not read a second time before it is fully read, so no claim is compared against an unfinished reading.
7. **The weekly sample.** For a claim from a filing, the sample checker is shown the part that holds the claim's quote, which fits its 30,000-token limit, instead of a whole filing it could never send (`reader_text`).
8. **Nightly, after the releases.** `extract-catalysts` reads filings right after the press releases, with the same `--limit` (60 a night, ADR 0064) counted separately, the same `--since`, and the same reading deadline of the time budget (ADR 0064) or `--max-minutes` (ADR 0065). A stop at the time limit or at the spending line below is a normal end; a filing that could not be read fails the run and alerts, as a release does.
9. **Spending line.** Filing reads, first and second, spend within each role's usual daily cap ($2). They go past it, up to the raised cap, only while the credit still covers a whole day (`credit_covers_a_day`, the backfill's own test), so a raised cap never pays for filings with paid money. Every filing call goes through `ModelClient` and is logged in `model_call`, so it counts toward the $96 credit check like any call. The client's daily caps apply as always.
10. **Store clean-up: `catalyst snapshots-clean`.** By default a dry run: it prints, by document type, the copies it would delete and why the others stay (`kept_not_fully_read`, `kept_backs_a_claim`, `kept_copy_shared`, `already_deleted`). With `--delete` it deletes a copy only when every document naming it is a filing of decision 1, is fully read, and backs no claim of any reader, published or held. Each copy is checked again with its rows locked just before it is deleted. The rows stay, with `copy_deleted_at` set: the link, the readings and the fact of the read remain, and the collector, which skips stored accession numbers, never fetches the filing again. Re-running deletes nothing twice. It logs and prints counts only. It never touches a press release, a registry record or any other source.
11. **Nightly clean-up.** The last step of `extract-catalysts` runs the same clean-up with deletion on, under the same rule. A fully read filing with no claim never gets one later: it is never read again, the verifier reads only filings with extractor claims, and the indication readers only published catalysts' sources.
12. **`snapshots-verify`** reads only kept copies and counts the deleted ones (`deleted_once_read`), which are not missing.
13. **Schema** (migration `1b7e8f9c6561`, reversible): the `document_part_reading` table, with row-level security; `source_document.fully_read_at` and `copy_deleted_at`, both empty; a check that a copy is never marked deleted before its document is fully read. The downgrade refuses while any copy is marked deleted, because older code would report each such copy as missing; otherwise it drops the table and the columns, and older code simply does not read filings.

## Alternatives

- **Read every word of every filing.** About 4 to 5 times the cost of the sections above for 10-Ks (risk factors alone are often longer than the rest), for text that states risks, not dated plans.
- **A filing-specific prompt.** Likely better at 10-K language, but a new prompt version needs its own gold set and paid evaluation before it may run (ADR 0014); the current prompt's instructions are not specific to press releases beyond its first sentence. Left for when the first filing reads show what it misses.
- **Mark "fully read" in `document_reading`.** That row is per prompt version and model, so a new version would make every filing unread again, and its copy may be gone. A column on the document says what the owner asked: read once.
- **Delete the document rows with their copies.** The collector would then fetch and store the same filings again on its next run, and the link and readings would be lost.
- **Delete press releases' copies too.** Not asked; a release is read whole and may be read again by a new prompt version (ADR 0038).
- **Keep the raised caps for filings.** About $6 a day more until 2026-12-08, but it would be paid money once the credit is used, which the owner approved only for the backfill.
- **Let the hiding check see the XBRL header.** Every filing claim would wait for a person, forever, for data tags the SEC requires filers to hide.

## Consequences

- Every stored and every new 10-Q, 10-K and text exhibit is read, once, within the usual caps. After the backfill's credit is used, the nightly run's leftover extractor money goes to filings.
- Copies of fully read filings with no claim are deleted the night they are fully read. A copy whose filing has a claim, published or held, stays.
- A claim lost to a later change of the quote check (ADR 0038, decision 5) cannot come back for a filing whose copy is gone. The filing is still on EDGAR at its link.
- PDF exhibits (decks) are never fully read, so their copies stay. Reading PDFs is a later decision.
- The first live reads should be checked against the section rules: a filing laid out in a way the headings miss is read whole, which costs more but loses nothing. The fixtures are hand-built in EDGAR's layout; a recorded filing should join them.

### Estimate, with the working

The live database could not be queried from the coding session, so the counts are estimated from the collection settings: collection since 2026-07-01 for 466 companies (`collect-live.yml`), the forms stored (ADR 0046), and the 8-K rates in ADR 0065.

- **10-Q:** two quarters (Q2, filed in August; Q3, filed in November) for most of 466 companies, less those that file 20-F or 6-K: **about 800 to 930**.
- **10-K:** fiscal years ending March to September, filed July to November: about 5 to 8 percent: **about 25 to 40**.
- **Other 8-K Exhibit 99s:** 6 to 12 press-release 8-Ks a year (ADR 0065) over about 5 months, so 1,150 to 2,300 8-Ks; perhaps 20 to 30 percent carry a second exhibit, about half of them PDF decks (skipped free): **about 125 to 350 to read**.
- **In all, about 950 to 1,300 documents to read.** `catalyst snapshots-clean` (a dry run) prints the real counts by type.

Cost per document, at Sonnet's $2 and $10 a million tokens, the prompt (about 1,800 tokens) on each call, and 1,000 to 2,000 output tokens a call with thinking:

- **10-Q:** discussion about 60,000 characters (15,000 tokens) in one part plus Other information in another: about $0.06 for the extractor, about $0.025 for the verifier (about 60 percent have a claim): **about $0.09**.
- **10-K:** Business about 150,000 characters in two parts, the discussion in one, Other information in one: about $0.19 and $0.08: **about $0.27**.
- **Text exhibit:** one part: **about $0.02**.
- **Total: about $88** (870 × $0.085 + 32 × $0.27 + 240 × $0.02), **range $55 to $150**; about $62 extractor, $26 verifier.

Days at the current caps:

- Until about 2026-11-30 the credit pays for the backfill's releases, which use the day's raised cap first; filings may use what is left of it, which is little, since the backfill reads until its cap or its time limit.
- After that, the backfill stops spending. Each nightly run reads about 20 to 40 new releases (about $0.4 to $0.8 of the extractor's $2), leaving about $1 to $1.5 for filings: 12 to 20 10-Qs a night. The reading step's time share (about 16 minutes) allows about 20 10-Qs after the releases, so money, not time, is the limit.
- **About 45 to 65 nightly runs, so about 8 to 11 weeks at six runs a week: done around late January to mid-February 2027 (range 6 to 16 weeks).** The verifier's $2 is enough for its share.
- The 10-K season (most covered companies file in February and March) then adds about 466 10-Ks, about $125, which at $1 to $1.5 a day would take about 3 months. A cap of its own for filings would shorten this; that is the owner's decision (ADR 0022) and this record changes no cap.
