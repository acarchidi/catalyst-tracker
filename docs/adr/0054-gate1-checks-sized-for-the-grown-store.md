# 0054 — Gate 1's checks, sized for the grown store

- Status: accepted (agent's proposal, 2026-11-12)
- Amends: ADR 0010 (which links Gate 1 checks), ADR 0043 (its line "Gate 1's check is unchanged")

## Context

The Gate 1 live run of 2026-10-26 (run 40361185828) failed two checks on time, not on a fault. Issues #104 and #105 were opened as a result.

- **Stored-copy check:** `snapshots-verify` re-reads every stored copy one at a time, and stopped at its 15-minute limit.
- **Link check:** `check-links` checks every stored source link. ClinicalTrials.gov allows about 45 requests a minute (ADR 0010). The drug list (ADR 0035) stored thousands of trial records. The check reached 2,031 links in its 45 minutes, all of them working (HTTP 200), and stopped.

Gate 1 itself passed on 2026-10-17. Gate 1's wording in `docs/quality-gates.md` and the roadmap is "every record with a working deep source link", where a record is a published one.

## Decision

1. `snapshots-verify` takes `--workers` (default 1). It reads that many copies at the same time and reports them in source-key order, whatever the number. Compression already reads the same store eight at a time (ADR 0046). Gate 1 runs it with 8, with a 30-minute limit.
2. Gate 1's link step runs `check-links --published` (ADR 0043). It checks the links that published records cite, as the read API shows them, and fails only on a missing or moved one. This is the same check the weekly job runs.
3. `check-links` with no option still checks every stored link, for a person who wants the full audit and can wait.

## Alternatives

- **Raise the time limits only.** Every stored link would take hours at 45 a minute, and keeps growing with every trial record. Gate 1's whole job is limited to 150 minutes.
- **A random sample of all stored links.** Repeatable with Gate 1's seed, but it checks links nothing publishes, and still misses some that published records cite.
- **Check ClinicalTrials.gov faster.** ADR 0010 keeps every source's rate limit; we do not exceed it.

## Consequences

- Gate 1's link check no longer covers stored copies that no published record cites. Those copies are still checked against their hashes by `snapshots-verify`.
- A refusal or a site error no longer fails Gate 1's link step; only a missing or moved link does (ADR 0043's rule).
- Issues #104 and #105 close themselves after the next clean Gate 1 run.
