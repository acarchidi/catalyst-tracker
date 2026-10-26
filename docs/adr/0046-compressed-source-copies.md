# 0046 — Source copies are stored compressed, and the existing ones are recompressed in place

- **Status:** accepted
- **Date:** 2026-10-26
- **Roadmap item:** none; an upkeep change to the snapshot store (ADR 0008), at the owner's request
- **Amends:** ADR 0008, decision 1 (where a copy's bytes live) and its consequence "Copies are never deleted by this code"

## Context

The private `source-snapshots` bucket holds 1.11 GB against the free plan's 1.1 GB, and Supabase's Fair Use Policy applies from 2026-11-24. The owner chose to compress the stored copies and stay on the free plan (2026-10-26).

Provenance depends on these copies: the publish gate, the second reading, the sample check and the export find quotes in them. So no copy may be lost or changed, and every read must still give the exact original bytes, checked against their SHA-256.

What fills the bucket, from the code (every copy is written by `store_fetched`, `collectors/documents.py`):

| Collector | What it stores | Read back by anything but `snapshots-verify`? |
| --- | --- | --- |
| `sec` | 8-K Exhibit 99 press releases; the main document of every 10-Q, 10-K, S-3, S-3ASR and 424B filing | Only 8-K EX-99.1 (extraction, second reading, publish gate, export, sample check). The 10-Q, 10-K, S-3 and 424B documents, and 8-K exhibits other than 99.1, are stored and never read. |
| `ctgov`, `ctgov-linked` | One full registry record per trial version (about 35 KB each; ADR 0040 measured 3.4 to 5 MB per 100) | No. The trial table is filled from the fetched record before it is stored; the export reads only the row, not the copy. |
| `openfda` | One full Drugs@FDA application per version (about 10 KB), for the 9 companies with a manufacturer name | No. |
| `fedreg` | One advisory committee notice (about 3 KB) | Yes, `advisory_meeting_noticed` (ADR 0018). |
| `wire` | One RSS item (about 2 KB) | No, today. |

The drug list (`drug_list_store.py`, ADR 0035) stores no copies: it reads Drugs@FDA and ClinicalTrials.gov into memory and writes only the drug tables. The coverage list has 19,876 trials, so the registry records alone are roughly 0.7 GB, and each new version of a trial adds another full record. The 10-Q and 10-K documents for 466 companies are likely the other large part. `catalyst snapshots-usage --by-source` gives the real split.

Measured on the recorded fixtures in `tests/fixtures`, built the way each collector stores them, gzip level 9 gives: registry records 4.6 times smaller (13 records, 450 KB to 98 KB), Drugs@FDA applications 6.7 times (38 records), the SEC press release 13.1 times, Federal Register notices and wire items 2.5 times. All of them together: 4.6 times. A bucket mostly of registry records and SEC documents should fall from 1.11 GB to about 0.25 GB.

## Decision

1. **New copies are compressed.** `SupabaseSnapshotStore.put` stores one gzip member (level 9, no timestamp, so the same bytes always give the same object) at `<key>.gz`. The key is unchanged: `sha256/<first two>/<hash of the original bytes>`, and `source_document.snapshot_key` keeps naming it, so no database row changes. The `.gz` suffix means a compressed object can never be mistaken for a plain one. Storing the same bytes twice is still one object.
2. **Reads try the compressed copy, then the plain one.** `get` reads `<key>.gz`; only when there is none does it read `<key>`. It inflates with a cap of 256 MiB (Storage's free plan refuses uploads over 50 MB, so no real copy is near it), refuses a copy that is cut short, has extra bytes, or inflates past the cap, and then checks the original bytes against the key as before. A compressed copy that fails its check is an integrity error; it is never a reason to read the plain copy instead, so damage is always reported. When neither copy is found, `get` tries the compressed copy once more, because `snapshots-compress` may have stored it and deleted the plain copy between the two reads. Bytes over the cap are stored plain, so they stay readable.
3. **The existing copies are recompressed by `catalyst snapshots-compress`.** It pages through the bucket with Storage's list API (one folder level per request, 100 entries a page, walking sub-folders), and counts a name the listing returns twice only once. For each plain copy it: reads it and checks its hash; stores the compressed copy; reads that back, inflates it and checks the hash; and only then deletes the plain copy. The store makes the last two one call, `delete_plain`, which refuses to delete unless the compressed copy is there and good, so no caller can delete a plain copy first. A plain copy that fails its hash is left alone and counted. A compressed copy that reads back wrong is deleted again only when the plain copy is there and good (`discard_damaged_compressed`), so reads keep working, and the plain copy stays. A storage error leaves both. Every step can be stopped and re-run: a run stopped after the upload is finished by the next one, and a finished bucket gives "nothing to do".
4. **Dry run, time limit, counts only.** `--dry-run` reads every plain copy and prints the counts and the bytes before and after, and writes nothing. `--max-minutes` starts no new copy after that time, so a long first run ends cleanly before its job's limit and the next run carries on. `--workers` (default 8) handles copies side by side. The log and the printed summary hold counts and sizes only, never content or quotes. The command exits non-zero when any copy needs a person (changed, failed the read-back check, or could not be read). It names no copy; `snapshots-verify` is the follow-up that lists each copy needing a person.
5. **`catalyst snapshots-usage`.** Prints the bucket's object count and bytes, split into plain, gzip and other objects. With `--by-source` it also reads the database and totals by the collector that stored each copy, SEC documents by form ("sec 10-Q"); a copy shared by several documents is counted once, under the first; a copy no row names is "no database row".
6. **The local store is unchanged.** `LocalSnapshotStore` keeps plain files: it holds only development copies (ADR 0008, decision 6), and leaving it alone keeps its tests and tools as they are.
7. **Running it live.** Both commands need only `SUPABASE_URL` and `SUPABASE_SECRET_KEY` (and the database for `--by-source`), which live in the repository's Actions secrets (ADR 0011). They are started by a `mode` choice on the hand-started `Collect (live)` workflow, in its own pull request, because workflow files are the owner's to approve. That workflow shares the `live-database` concurrency group, so a compression never runs beside a collection. Only one `snapshots-compress` may run at a time: two runs that both meet a damaged compressed copy could, with unlucky timing, each delete what the other just stored. The concurrency group ensures this for workflow runs; the command is not to be started by hand beside one.
8. **Copies that nothing reads are kept.** The registry records, Drugs@FDA records, wire items, and SEC documents other than 8-K EX-99.1 are stored and only `snapshots-verify` reads them. Deleting or no longer storing them is a separate owner decision; this change only compresses them.

## Alternatives

- **Upgrade to the Pro plan.** Declined by the owner.
- **Compress with a `Content-Encoding: gzip` header and let Storage or the CDN inflate on download.** The bytes on the way back would depend on proxy behaviour, and a reader could get either form; a distinct object name and explicit inflation keep one exact path.
- **Keep the key and overwrite the plain object with compressed bytes.** Uploads never overwrite (ADR 0008), and the same name holding two formats is exactly the confusion the suffix avoids.
- **zstd or brotli.** About 10 to 20 percent smaller again, but a new dependency for a bucket that already falls well under the limit with the standard library's gzip.
- **Fall back to the plain copy when the compressed one is damaged.** It would hide damage from `snapshots-verify`; the compress command instead removes a compressed copy it finds bad, but only when a good plain copy is there.
- **Stop storing copies nothing reads.** Would save more, but changes what the project keeps as evidence; left to the owner (decision 8).

## Consequences

- After this merges, every new copy is stored compressed. Until `snapshots-compress` runs, a copy collected again whose plain copy already exists is stored a second time, compressed; the command then deletes the plain one.
- Run order live: `snapshots-usage --by-source`, then `snapshots-compress --dry-run`, then `snapshots-compress` (re-run until it reports nothing to do), then `snapshots-usage` and `snapshots-verify` to confirm.
- This code now deletes objects, but only a plain copy whose compressed copy was just read back and checked, or a compressed copy that fails its check while a good plain copy is there.
- A failed compression run shows as a failed workflow run; it opens no alert issue, because the alert steps run only in the collect mode.
- `get` makes one extra request for a copy not yet compressed. Once the bucket is recompressed, every read is one request again.
- Tests use the fake Storage API in `tests/fake_storage.py`, extended with listing and deleting from Supabase's published routes; no live request was made. If the live listing differs (for example in how it names folders), the fix is in `SupabaseSnapshotStore.list_objects`, and the dry run shows it before anything is written.
