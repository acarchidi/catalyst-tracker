# 0011 — Live jobs run in GitHub Actions; the live database is closed to Supabase's public API

- **Status:** accepted
- **Date:** 2026-10-17
- **Roadmap item:** Gate 1 (end of Phase 0): the live run

## Context

The owner created the Supabase project on 2026-10-17. From a Claude Code cloud session, Supabase's storage API answered (HTTP 401 without a key), but a direct database connection did not: the session's network allows HTTPS through a proxy, and both the Session pooler (port 5432) and the direct host were unreachable (the direct host has no IPv4 address). The spec already puts scheduled jobs in GitHub Actions. The owner also asked, the same day, never to face this much setup again.

Supabase serves tables in the `public` schema through its Data API to anyone holding the project's publishable key unless row-level security is on. That key is designed to be shared, so tables must not depend on it staying secret.

## Decision

1. **Where live jobs run.** Every job that touches the live database or storage runs in GitHub Actions. The first is `.github/workflows/gate1-live.yml`, started by hand: migrate, collect the 50 Gate 1 companies into the live database and bucket, check every stored copy and link, and print the 10-record sample. Jobs share one concurrency group, so two never write at once.
2. **Where the settings live.** Only in the repository's Actions secrets (setup Part 5). The coding session's settings (old Part 4) are no longer needed. `scripts/check_setup.py` reports the secrets as the source of truth and no longer sends the owner to set session variables.
3. **How the database is reached.** Through Supabase's Session pooler, which has an IPv4 address that GitHub's runners can reach. The owner copies the pooler string exactly as Supabase shows it, with `[YOUR-PASSWORD]` left in, into `SUPABASE_DB_URL`; the code fills in `SUPABASE_DB_PASSWORD` (URL-encoded) and selects the psycopg driver. A string with a password typed in is refused, so the password lives in one secret only. `CATALYST_DATABASE_URL` still wins for local and test databases.
4. **Row-level security on every table, with no policies.** Migration `c14b85b5863c` turns it on for all 17 tables. The pipeline connects as the tables' owner, and storage uses the secret key, so both are unaffected; the publishable key and signed-in users of the Data API see nothing. A test fails if any table in `public` is left without it, so future tables must turn it on in their own migration.
5. **Owner setup, from now on.** Prefer designs that need no new account or key; when one is unavoidable, gather everything the next stretch of work needs into one sitting, one small step at a time, and check the result in code (AGENTS.md).

## Alternatives

- **Ask the owner to widen the session's network.** The session's proxy carries HTTPS; a database connection is not HTTPS, so widening the allowed domains would likely not help, and it would add owner steps.
- **Talk to the database through Supabase's Data API instead of SQL.** Would work from a session, but the pipeline is built on SQL and migrations, and the Data API would need the tables open to it.
- **Direct connection with Supabase's IPv4 add-on.** Paid, and not needed.
- **Storing the pooler string with the password in it.** One secret instead of two, but the password would then live in two places, and a password with special characters would break the string.

## Consequences

- Gate 1's live run, and later the scheduled collections, are started from the Actions tab (or by the coding session through GitHub's API) and report in the job summary.
- Reading live data from a coding session needs a job, not a query; that suits the rule that live data changes only through jobs and the publish gate.
- The Anthropic key, when Phase 1 needs it, is one more Actions secret, not a session setting.
