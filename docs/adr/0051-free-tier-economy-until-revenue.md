# 0051 — Stay on free plans until revenue: a weekday schedule keeps the database awake

- **Status:** accepted
- **Date:** 2026-11-10
- **Roadmap item:** Phase 1, "Orchestrator controls" groundwork, and Phase 2 hosting (owner request 2026-11-10)

## Context

On 2026-11-10 the owner asked to run without paid plans, and without outages, until the product brings in revenue.

What happened on the free plans:

- **The database paused.** Supabase pauses a free project after 7 days of low activity ([Supabase docs](https://supabase.com/docs/guides/platform/free-project-pausing)). The live jobs are started by hand only, the last ran about 26 October, and every job from 2026-11-09 failed with "tenant/user ... not found". The owner resumed the project on 2026-11-10.
- **Storage went over quota once.** The bucket of source copies reached 1.11 GB of the free 1 GB in October. Compression (ADR 0046) brought it back under. The dashboard on 2026-11-10 showed the cycle's average at 0.569 GB, egress at 2.5 of 5 GB, and a grace period to 23 November that applies only if usage stays over quota.
- **Pro costs $25 a month.** It adds daily backups kept 7 days, never pauses, and raises the limits to 8 GB of database and 100 GB of storage ([Supabase pricing](https://supabase.com/pricing)). The free plan has no automatic backups.

The spec already plans collection twice each weekday. No schedule was ever switched on.

## Decision

1. **A weekday schedule for the two live jobs.**
   - Collection runs at 22:13 UTC, Monday to Friday (5:13 pm New York in winter, 6:13 pm in summer). It collects SEC filings of the last 10 days for every covered company. A missed day is caught by the next run; collectors skip what they already stored.
   - Extraction runs at 00:43 UTC, Tuesday to Saturday, and reads at most 20 new releases.
   - Both share one concurrency group, so they never overlap; a late collection only delays the extraction.
   - A scheduled run has no inputs. Each input falls back to its default. The export and the gold-set scoring never run on the schedule.
   - This is once each weekday, not the spec's twice: it is enough to keep the database awake and the calendar a day fresh, at half the model cost.
2. **Cost.** GitHub Actions minutes are free for a public repository. SEC collection makes no model calls. Extraction stays inside the existing daily caps in `config/models.toml`, which this record does not change. At a few cents a release for each reading, 20 releases a weekday is roughly $1 a day; the owner's Anthropic workspace limit of $50 a month stays the ceiling.
3. **Failures reach the owner.** Both jobs already turn a failure into an alert issue (ADR 0023). If the database pauses again, the next weekday's run says so the same evening.
4. **When to upgrade Supabase to Pro**, at the first of:
   - the first paying subscriber (from then, losing the database without a backup would lose what subscribers pay for);
   - the bucket nearing 1 GB;
   - the database pausing again despite this schedule.
5. **The stop rule, once Pro is bought.** Three months after the first issue, if net revenue is below running costs for two months in a row, the owner is reminded to cancel Pro. The coding agent sets that monthly check when the owner says Pro is bought.

## Alternatives

- **Buy Pro now.** Simplest, and backups are worth having, but it spends $300 a year before any revenue. Nothing it fixes is urgent once the project is awake and used.
- **A keep-alive query every few days.** Free and small, but it keeps the database awake without making the calendar fresher. The schedule does both.
- **Twice-daily runs, as the spec says.** Twice the model cost for little gain while the calendar has no readers. Revisit at the first paying subscriber.

## Consequences

- The database should no longer pause, and the calendar refreshes each weekday without anyone starting a job.
- Model spend becomes regular: about $20 a month at the start, inside the caps.
- The bucket grows with each weekday's new filings. The upgrade trigger in decision 4 watches for it; a weekly size alert is a follow-up.
- Scheduled workflows on a public repository stop after 60 days with no repository activity. Ordinary work keeps it active.
