# 0008 — Source snapshot store

- **Status:** accepted
- **Date:** 2026-10-16
- **Roadmap item:** Source snapshot store with SHA-256 hashes (leads to Gate 1)

## Context

The spec requires a private copy and SHA-256 hash of every source document, "so we can prove what the page said on the day we read it" (Provenance, item 5), kept in Supabase Storage. ADR 0002 stored copies on local disk until this item and asked it to settle whether local copies move.

## Decision

1. **Key states the hash.** Every copy is stored at `sha256/<first two hex digits>/<hash>`. The same bytes always get the same key, so storing them again is harmless.
2. **Every read proves the copy.** `get` re-hashes the bytes and raises `SnapshotIntegrityError` when they no longer match the key; a missing copy raises `SnapshotNotFoundError`. Malformed keys are refused before any lookup.
3. **Two stores, one interface.** `LocalSnapshotStore` (disk, for development and tests) and `SupabaseSnapshotStore` (a private bucket named `source-snapshots`). Commands choose with `--snapshot-store local|supabase`; the default is `local`, and `supabase` without `SUPABASE_URL` and `SUPABASE_SECRET_KEY` stops with the setup step. There is no silent fallback from one to the other.
4. **Supabase details.** Requests go to `<SUPABASE_URL>/storage/v1` over https only, with the secret key in the `apikey` header and no `Authorization` header, because Supabase's new secret keys are not JWTs. Uploads never overwrite (`x-upsert: false`); Storage's "Duplicate" answer (HTTP 400 or 409 with status `409` in the body) counts as already stored. Before writing, the collector creates the bucket if needed and refuses to use it if it is public, since copies are for verification, not redistribution.
5. **Verification command.** `catalyst snapshots-verify` re-reads the copy behind every `source_document` row and lists the source keys whose copy is missing or changed; it exits non-zero if any are. The auditor agent (Phase 1) can run it on a schedule.
6. **Local copies are not migrated.** The only local copies are from development runs against the local database; the live database starts empty and fills from the live store.

## Alternatives

- **Supabase's Python client library.** Adds a dependency tree for three HTTP calls; the plain client keeps the request shape visible and testable.
- **Choosing the store automatically when Supabase settings exist.** Convenient, but a missing setting would silently send live copies to a disk that disappears with the machine.
- **Overwriting on upload.** Content-addressed keys make overwrites pointless, and refusing them means a bug can never replace a stored copy.

## Consequences

- Tests use a fake of the Storage API built from Supabase's published routes; no live request has been made, because the project has no Supabase account yet (setup Part 3). The first live run should be `catalyst snapshots-verify --snapshot-store supabase` after a small collection.
- If Supabase Storage ever rejects the `apikey`-only header, the fix is in one place (`SupabaseSnapshotStore.__init__`).
- Copies are never deleted by this code. A retention rule, if needed, is a later decision.
