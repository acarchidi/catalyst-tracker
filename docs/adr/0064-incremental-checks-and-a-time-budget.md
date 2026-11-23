# 0064 — Incremental checks and a time budget for the nightly extraction

- Status: accepted (agent's proposal, 2026-11-23)
- Amends: ADR 0018 (each check run re-checked every claim), ADR 0051 (the weekday read limit of 20)

## Context

The Saturday run of 2026-11-21 (run 44881511307) failed: the "Extract catalysts" step reached its 45-minute limit. GitHub cancelled it in the middle of the indication step, so its summary was never written and the alert said only that the report could not be read.

The run log shows where the time went. Reading 40 releases took 3 minutes. The rest went to matching and checking all 945 stored claims again, although almost none had changed:

| Step | Minutes |
| --- | --- |
| Matching names (`resolve_claims`) | 6.6 |
| Rule checks (`run_checks`) | 13.3 |
| Advisory check | 1.3 |
| Company check | 6.9 |
| Drug check | 4.8 |
| Type check | 1.9 |
| Safe-to-write check | 1.5 |
| Second reading (13 releases) and its comparison | 2.2 |
| Publish decisions | 2.8 |

Two things cost the time. First, each step wrote one database row at a time: about 19,400 statements a run through the Session pooler, at about 85 milliseconds each (13.3 minutes for the 9,457 statements of the rule checks). Second, six steps each read every claim's stored copy again from the storage bucket, one at a time: about 2,700 reads of 450 copies. Both grow with every claim, so the run would grow past any limit.

## Decision

1. **Each check row records the code that found it and what it read.** `claim_check` gains two empty columns (a reversible migration):
   - `rule_version`: a 16-character hash of the source of the check's module, of every module of ours it imports, directly or not, and of the Python version that runs it. Any change to that code, or a new Python, is a new version, with no number for anyone to remember to bump.
   - `input_digest`: for the checks that read stored copies (company, drug, type), a SHA-256 of everything else the check read for the claim: the copy's hash and format, the text version, the quote and its place, and the company or drug records it compares. Empty for every other check.
2. **The copy-reading checks skip only what is unchanged.** A claim is skipped only while both its stored code version and its input digest match. So a new claim, a claim whose source, quote, company link, or drug records changed, and every claim after a change to the check's code are checked again. This holds for held and published claims alike: a published claim is checked again when its source or its rule changes, and a held one is checked again once when a rule changes. An outcome that came from a copy that could not be read keeps no digest, so that copy is tried again next run. Skipped claims are still counted, with their stored outcome, in the run's summary.
3. **The other checks are computed in full every run, and only changes are written.** The rule checks depend on today's date (a window passes) and on other claims (a second source arrives), and they read no copy, so computing them takes seconds. Only rows whose outcome, reason, or code version changed are written, up to a thousand to a statement. `checked_at` keeps its meaning: it moves only when the outcome or reason changes (ADR 0042).
4. **Matching writes only what changed.** Each claim is first matched without writing. If its matches come out exactly as stored, nothing is written for it, and one statement marks all such claims' matches as made at this run's time, so `resolved_at` keeps its meaning. A claim whose matches differ, or whose matching would create or join a drug record (ADR 0015, ADR 0060), is matched the usual way inside its own savepoint.
5. **Each stored copy is read once a run, eight at a time.** After reading new releases, the run reads every claim's stored copy, eight at a time as `snapshots-verify` does (ADR 0054), and keeps them in memory for the rest of the run, up to 512 MB. The copies are checked against their hash when read, as always. A copy that cannot be read is not kept, and the step that needs it reports it as before.
6. **The publish gate is unchanged.** It still decides every claim on every run and still matches every quote against its stored copy (spec: Provenance), and the writer still checks again before it writes. With the copies already in memory this takes seconds, so nothing in the gate was narrowed.
7. **A time budget ends the run cleanly.** `extract-catalysts --time-budget-minutes` (default 35, ten minutes inside the step's 45-minute limit, for a model call already under way) gives each step a deadline:
   - Reading new releases starts no new release after 45% of the budget (about 16 minutes).
   - The second reading starts no new release after 70% (about 25 minutes).
   - The copy-reading checks stop after 90%; then nothing is decided or published that run, so no decision rests on a check that did not finish.
   - Past the whole budget nothing is decided or published; past 85% no indication is asked for.
   A step that stops says `"stopped_for_time": true` in the summary, and the run does not fail. Every step is safe to stop and re-run: a release already read is never read again, and a check row already renewed is skipped. So the next run carries on where this one stopped.
   Reading new releases stops through the same deadline as the backfill's `--max-minutes` (ADR 0065), so its summary also says "time limit reached" under `stopped_early`. A stop at the time limit is never a failure; a stop at a daily cap still is, unless `--stop-is-expected` is given. With `--max-minutes`, reading takes that long, and every later step keeps the same minutes after reading as on a nightly run.
8. **The nightly read limit goes from 20 to 60 releases** (in `.github/workflows/extract-live.yml`, its own pull request because workflows are owner paths, ADR 0036). Each new release costs about 10 seconds for the extractor and 4 for the second reading, so 60 take about 14 minutes, well inside the 16 and 25 minutes the budget gives those steps. 60 also fits today's daily caps of $2 each for the extractor and the verifier at a few cents a release; a higher limit waits for the caps work on its own branch. The caps are not changed here. Product model jobs still run at most once a day (ADR 0055).

## Measured effect

A recorded test setup with 945 claims on 450 stored copies (the live store's size on 2026-11-21), counting the statements and copy reads each step makes:

| | Before | After (nothing new) |
| --- | --- | --- |
| Database statements, all check steps | 19,356 | 64 |
| Stored-copy reads | 2,700, one at a time | 450, eight at a time |
| Estimated time on the live database | about 41 minutes (as measured) | about 1 minute |

After a change to a check's code, the copy-reading checks check every claim again once: still about a minute, since the copies are already in memory and the writes go a thousand at a time. With 60 new releases, a whole run is estimated at about 20 minutes.

## Alternatives

- **Raise the step's time limit.** The work grows with every claim, so any limit is reached again.
- **Check only claims extracted in this run.** A published claim must be checked again when its source, its records, or the rule changes, and the rule checks depend on today's date. Recording what each check read is what lets a run skip safely.
- **Skip the gate's quote match for unchanged claims.** The gate is the last check against the stored copy (spec: Provenance) and is cheap once copies are read once; it stays whole.
- **A hand-kept version number per check.** It is easy to forget. A hash of the code cannot be forgotten, at the cost of checking every claim again once after any change to that code.

## Consequences

- A nightly run that changes nothing takes a few minutes, not 45, and its length no longer grows with every stored claim.
- A run that runs short of time ends cleanly with its summary, and the alert step reports what happened.
- The first run after this change checks every claim once more (no stored row has a version yet), then skips what is unchanged.
- Any change to a check's module, or to a module it imports, checks every claim again once.
- The migration's downgrade drops the two columns and keeps every outcome; older code checks every claim every run, as before.
