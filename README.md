# catalyst-tracker

Tracks biotech and biopharma catalysts (PDUFA dates, trial readouts, advisory committees) for US-listed companies, with a primary-source link on every record, plus readout benchmarking, market sizing, and price-impact statistics.

- What we are building: [docs/spec.md](docs/spec.md)
- Build order and decision log: [docs/roadmap.md](docs/roadmap.md)
- Where work stops for review: [docs/quality-gates.md](docs/quality-gates.md)
- Rules for coding agents: [AGENTS.md](AGENTS.md)

## Owner checklist: setup steps only the owner can do

Claude Code checks these at the start of every session with `python3 scripts/check_setup.py` and reminds the owner of anything missing. Step-by-step instructions: [docs/setup.md](docs/setup.md).

- [x] Part 1 — Create the GitHub repository (done 2026-10-16)
- [ ] Part 2 — Anthropic API key in its own workspace, $50/month limit (5 min; needed for Phase 1)
- [x] Part 3 — Supabase account and project (done 2026-10-17)
- [x] Part 4 — Not needed (live jobs run in GitHub Actions, ADR 0011)
- [x] Part 5 — Keys in GitHub's secrets (Supabase and SEC done 2026-10-17; the Anthropic key joins in Phase 1)
- [x] Part 6 — Reviewer key, so pull requests merge themselves (done 2026-10-16)

Not needed yet; Claude Code says when: Vercel, Resend, Sentry, domain (Phase 2); business entity and Stripe (Gate 3); Tiingo price data (Phase 3).

None of these block the start: the first roadmap items need no keys.

## Run the walking skeleton locally

Needs Docker, [uv](https://docs.astral.sh/uv/), and `SEC_CONTACT_EMAIL` set in your shell.

```bash
scripts/demo.sh            # fetches Vertex Pharmaceuticals' latest 8-K press release
scripts/demo.sh 1682852    # or any company's SEC number (CIK)
```

It starts a throwaway local database, builds the tables, fetches one SEC filing, stores a copy, and prints the database row with its source link.

To run every collector for the ten pilot companies and see their latest documents:

```bash
uv run catalyst collect --since 2026-07-01
uv run catalyst report
uv run catalyst snapshots-verify   # proves every stored copy still matches its fingerprint
uv run catalyst coverage-build     # rebuilds config/coverage_companies.toml and docs/coverage-review.md (about 15 minutes)
uv run catalyst extract-catalysts --limit 5 --out var/catalysts.json   # AI step: needs CATALYST_ANTHROPIC_API_KEY; costs a few cents per release
```

Copies go to `var/snapshots/` unless a command gets `--snapshot-store supabase` (ADR 0008). Developer details: [pipeline/README.md](pipeline/README.md).

## How to start building

1. Start a Claude Code session on this repository.
2. Say: **start implementing**.

Claude Code runs the setup check, reminds you of anything missing, works through the roadmap, and sends you a preview at each checkpoint without stopping (ADR 0007). Reply with anything you want changed, at any time.
