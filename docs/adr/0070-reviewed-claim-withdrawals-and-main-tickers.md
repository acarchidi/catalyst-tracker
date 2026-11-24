# 0070 — Reviewed claim withdrawals, and the main ticker of a company with several

- **Status:** accepted
- **Date:** 2026-11-24
- **Roadmap item:** none; an urgent fix before the first weekly issue (pre-publication review, 2026-11-24)
- **Amends:** ADR 0018 part 5b (the writer may now unpublish, only from the reviewed list) and ADR 0009 (main ticker rule)

## Context

A review before the first weekly issue found eight published claims that are stale (the event already happened or was superseded) or misread (the quote states another event). Data is never edited by hand, and the review queue (ADR 0042) only decides held claims; nothing could take back a published one.

The same review found two companies shown under a warrant or rights ticker: Kiora (CIK 1372514) as KPHMW instead of KPRX, and Ligand (CIK 886163) as LGNZZ instead of LGND. SEC's company record lists every ticker of a company, over-the-counter warrants and rights included. All are stored as current tickers, and the read layer showed whichever row the database returned last.

## Decision

1. **`config/claim_withdrawals.toml`**, reviewed in pull requests like code. Each `[[withdrawal]]` has `claim` (a full claim id or a prefix of at least 8 hex characters), `reason` (plain English), `evidence` (a web address, or "own source" when the claim's own document shows the error) and `added` (a date).
2. **It only ever withholds.** The decision job and the writer hold a listed claim with "withdrawn after review: <reason>", applied after any review-queue approval, so no approval lifts it.
3. **Unpublishing.** Before placing claims, the writer sets every catalyst that a listed claim stands behind (any change row or trial link citing it) to the new status `withdrawn`, with a `status` change row citing that claim, marked as a correction, later than the catalyst's last change. A withdrawn catalyst takes no more claims. The writer also matches each claim it places against the withdrawn catalysts (company, type, drugs, trials; the indication is ignored) and holds any match as "the event was withdrawn after review", unless its document was published after the withdrawal itself (the time of the status change); so a lower-ranked claim, or a later release repeating the stale date, never brings the event back as a new catalyst (Opus reviews of PR #151). Only a release published after the withdrawal makes a new catalyst. A correct later claim that was already recorded against a catalyst before it was withdrawn stays withdrawn with it; a newer release brings the event back. The indication step never writes to a withdrawn catalyst. Safe to re-run.
4. **Never shown.** Every read (API, MCP, export, and so the digest) leaves out withdrawn catalysts. The export's held list also leaves out withdrawn claims.
5. **Loud failure.** An entry that names no claim or several, or a file that cannot be read, stops the decision job and the writer with an error; the nightly run's alert then says the publish decisions did not finish, and nothing is published until the list is fixed. A failed check already raises its own alert. When the decisions are skipped for time (ADR 0064) while listed claims are still marked to publish or still shown, the run's summary counts them and the alert says so; if the count cannot be read, the alert says that instead.
6. **Main ticker** (`catalyst.tickers.main_ticker`): the first ticker on Nasdaq, NYSE, Cboe or the Texas Stock Exchange without a warrant, unit, right or other suffix (a dash or dot, or a fifth letter W, U, R or Z); an over-the-counter ticker never wins while an exchange-listed one exists; ties keep SEC's order. The coverage build and the read layer both use it. The read layer chooses on every read from the stored current tickers, which are all genuine, so no stored row needs a recompute: the correction takes effect on the next read.

## Alternatives

- Editing or deleting the live rows: forbidden (AGENTS.md), and leaves no record of why.
- Rejecting the claims in the review queue: the queue decides held claims only and has no evidence field.
- A stored "main ticker" column with a recompute job: a migration and a job for a value code can choose on every read.

## Consequences

- One reversible migration: `catalyst.status` widens to 9 characters and allows `withdrawn`; the downgrade refuses while any catalyst is withdrawn.
- A withdrawal reaches live data on the next run of the writer: the nightly "Extract catalysts (live)" run, or a manual dispatch of it.
- A typo in the list blocks publishing until fixed, by design.
