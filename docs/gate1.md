# Gate 1 runbook

Gate 1 (docs/quality-gates.md): 50 companies flow from source to the live database; the owner clicks 10 random records; every record has a working deep link. ADR 0010 explains the choices.

1. Choose the companies (already done; repeatable): `uv run catalyst gate1-select`
2. Collect them: `uv run catalyst collect --companies config/gate1_companies.toml --since 2026-07-01 --snapshot-store supabase`
3. Check every stored copy: `uv run catalyst snapshots-verify --snapshot-store supabase`
4. Check every link: `uv run catalyst check-links`
5. Make the owner's preview: `uv run catalyst sample-records`

Steps 2 to 5 read `CATALYST_DATABASE_URL` and the Supabase settings (setup Part 3, then Part 4). Without them, the same commands run against a local database and local copies, which is how the dress rehearsal in docs/gate1-rehearsal.md was made.
