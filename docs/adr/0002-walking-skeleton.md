# 0002 — Walking-skeleton choices

- **Status:** accepted
- **Date:** 2026-10-16
- **Roadmap item:** Repository skeleton (checkpoint CP1)

## Context

CP1 asks for one SEC filing fetched, stored, and written as one database row with a clickable source link. The spec leaves several small choices open for that path.

## Decision

1. **Which document.** The newest 8-K among a company's 10 latest that has a press-release exhibit (EX-99.1, else EX-99). The stored link is the exhibit itself, never the filing index or a homepage.
2. **Duplicates.** A source document is unique by URL plus SHA-256 hash. Re-fetching an unchanged document changes nothing. If the publisher changes the document at the same URL, the new copy becomes a new row, so we keep proof of both versions.
3. **Publication time.** `published_at` is EDGAR's acceptance time from the company's filing list. It is true UTC: Vertex's 8-K 0000875320-26-000256 shows "Accepted 2026-08-03 16:09:43" (New York time) on its index page and `2026-08-03T20:09:43.000Z` in the filing list. A test pins this.
4. **Copies.** Stored on local disk under `var/snapshots/`, named by hash, until the "Source snapshot store" item moves them to Supabase Storage behind the same `put` and `get` methods.
5. **Database address.** A new setting, `CATALYST_DATABASE_URL`, defaulting to the local Docker database. It is not a secret locally. The production value comes from the Supabase settings in a later item.
6. **Request rate.** 8 requests per second to SEC hosts, below the SEC's limit of 10, with no bursts. The client refuses any non-SEC host.
7. **Logs.** One JSON object per line on standard error, from the Python standard library. Error tracking (Sentry) arrives with the first scheduled job; setup.md lists Sentry for Phase 2. Until then, a failed run shows in its GitHub Actions log.
8. **Coverage.** CI requires 80% line coverage across the package now. The 90% bar for core packages (verification, publish gate, parsers, entitlements) is added when the first of them lands.

## Alternatives

- Uniqueness by URL alone: simpler, but a silently edited press release would overwrite our proof of what it first said.
- Supabase Storage now: needs keys the owner has not created yet, and belongs to the next roadmap item.
- `structlog` for logs: nicer API, one more dependency for no gain at this size.
- Hand-written SEC pages for tests: faster to make, but they test our assumptions, not EDGAR. Tests use real responses saved by `scripts/record_sec_fixtures.py`; pre-commit hooks and git line-ending rules skip `tests/fixtures/` so the copies stay byte-exact.

## Consequences

The skeleton runs with no keys except `SEC_CONTACT_EMAIL`. The snapshot-store item must migrate any local copies it needs to keep.
