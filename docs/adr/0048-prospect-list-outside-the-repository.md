# 0048 — The outreach prospect list is kept outside the public repository

- **Status:** accepted
- **Date:** 2026-11-09
- **Roadmap item:** Phase 2, "Marketing and outreach agent" (owner decision 2026-11-09); amends ADR 0020

## Context

ADR 0020 put the outreach prospect list at `outreach/prospects.csv`, a checked file in the repository (added 2026-10-18). The repository became public on 2026-10-20. Since then anyone could read the list: 209 public work contacts, the notes on why each fits, and which of them are competitors. Every address on it is already published by its owner, so no private data leaked. The harm is that the people on it, and competitors, can read our outreach plan.

On 2026-11-09 the owner asked for the list to be taken off GitHub and kept.

## Decision

1. **The list is no longer a file in the repository.** `outreach/prospects.csv` is removed. The owner holds the list privately; a copy was handed to the owner on 2026-11-09 (209 rows, 115 with an email, all `new`; SHA-256 `c87ad408…9d0dc9`).
2. **Git ignores the place a working copy goes.** `.gitignore` lists `outreach/*.csv`, so a copy saved at `outreach/prospects.csv` for a check is left out by `git add`. A test fails if that line is removed.
3. **The checker works on any copy.** `catalyst prospects-check` reads `outreach/prospects.csv` by default, or the file named with `--file`. With no file there, it says the list is kept outside the repository and ends with code 2. The rules for rows, statuses, and `do_not_contact` are unchanged (ADR 0020).
4. **Tests use four made-up contacts** (`tests/fixtures/outreach/prospects_sample.csv`, all on `example` addresses). Two existing tests read the real list and counted its rows; they now read the sample. They check the same things: the file format loads, and a list is written back in its own order.
5. **No contact's details go into any committed file, pull request, issue, or job log from now on.** Counts are fine. The marketing agent's drafts and the approval record (ADR 0020) will name recipients only in the owner's private page, as the review queue does for held claims (ADR 0042).
6. **The old copy is still in the repository's history.** Removing a file does not remove it from earlier commits (it was added in commit `d3423e0`). Anyone can still read that version. Taking it out of history means rewriting main's history and asking GitHub to drop its cached copies, or making the repository private. Both are the owner's decision; neither is done here.

## Alternatives

- **Keep the file, encrypted, in the repository** (`catalyst seal`, as for the private export). It would stay beside the code, but the owner would have to keep a private key safe, and every update would need the coding session to open and re-seal it.
- **Store it in the private Supabase bucket.** A good later home, once a job can read and update it there. It needs a workflow change and a working live database first.
- **Rewrite history now.** It breaks every open branch and cannot be undone, and copies may already exist elsewhere. Left for the owner (decision 6).
- **Leave it public.** Every address is public anyway. The owner declined.

## Consequences

- CI no longer checks the real list on every pull request. The owner's copy is checked when a session has it: `uv run catalyst prospects-check`.
- A coding session cannot read the list unless the owner hands it over. Drafting notes to named people (ADR 0020) starts with that step.
- If the owner's copy is lost, the 2026-10-18 version can be recovered from history, unless history is rewritten (decision 6).
- Revisit when the marketing agent is built: the private bucket is the likely home.
