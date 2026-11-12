# 0054 — Gate 1's checks, sized for the grown store

- Status: accepted (agent's proposal, 2026-11-12)
- Amends: ADR 0010, decision 2 (which links Gate 1 checks; its HTTP 200 rule stays), and ADR 0043 (its line "Gate 1's check is unchanged")

## Context

The Gate 1 live run of 2026-10-26 (run 40361185828) failed two checks on time, not on a fault. Issues #104 and #105 were opened as a result.

- **Stored-copy check:** `snapshots-verify` re-reads every stored copy one at a time, and stopped at its 15-minute limit.
- **Link check:** `check-links` checks every stored source link. ClinicalTrials.gov allows about 45 requests a minute (ADR 0010). The drug list (ADR 0035) stored thousands of trial records. The check reached 2,031 links in its 45 minutes, all of them working (HTTP 200), and stopped.

Gate 1 itself passed on 2026-10-17.

ADR 0010 defines Gate 1's link check as every stored link of the collected companies, passing a link only on HTTP 200. ADR 0043 added a weekly check of the links published records cite, which fails only on a missing or moved link, and left Gate 1's check unchanged.

## Decision

1. `snapshots-verify` takes `--workers` (default 1). It reads that many copies at the same time and reports them in source-key order, whatever the number. Compression already reads the same store eight at a time (ADR 0046). Gate 1 runs it with 8, with a 30-minute limit.
2. Gate 1's link step runs `check-links --published --strict`. It checks the links that published records cite, as the read API shows them, and still passes a link only on HTTP 200, as ADR 0010 asks: a refusal, a site error, or no answer fails it. This narrows the scope of ADR 0010's check (decision 2) from every stored link to the links readers are shown. It keeps ADR 0010's pass rule. The weekly check keeps ADR 0043's rule.
3. `check-links` with no option still checks every stored link, for a person who wants the full audit and can wait.
4. The step limits add up to 145 minutes, under the job's 150, so a slow step fails on its own and the alert step still runs.

## Alternatives

- **Raise the time limits only.** Every stored link would take hours at 45 a minute, and keeps growing with every trial record. Gate 1's whole job is limited to 150 minutes.
- **A random sample of all stored links.** Repeatable with Gate 1's seed, but it checks links nothing publishes, and still misses some that published records cite.
- **Check ClinicalTrials.gov faster.** ADR 0010 keeps every source's rate limit; we do not exceed it.

## Consequences

- Gate 1's link check no longer covers stored copies that no published record cites. In the 2026-10-26 run, all 2,031 such links it reached worked. Every stored copy is still checked against its hash by `snapshots-verify`.
- A link a site refuses still fails Gate 1, so a run can turn red on a refusal that says nothing about the document; a re-run fills the gap (docs/gate1.md).
- Issues #104 and #105 close themselves after the next clean Gate 1 run. The weekly published-link check uses the same alert key, so a clean weekly check can also close #105.
