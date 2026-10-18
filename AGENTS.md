# AGENTS.md — rules for every coding agent in this repository

Read this file, `docs/spec.md`, `docs/roadmap.md`, and `docs/quality-gates.md` before writing code. If any of them conflict, stop and ask the owner.

## Who you work for

The owner is not a programmer. They approve work by reading plain-English summaries, clicking previews, and checking test results. Write every message to the owner in short, plain sentences with no unexplained jargon. Never ask the owner to read code to make a decision.

## Session-start protocol (every session, before anything else)

1. Run `python3 scripts/check_setup.py --online`.
2. Show the owner the result.
3. If anything is missing, quote the matching steps from `docs/setup.md` word for word, and say which upcoming roadmap item needs it.
4. Keep working on items that do not need the missing setting. Building, schemas, and tests on recorded fixtures need no keys. Remind the owner again before starting any item that does need it.
5. Live jobs read their settings from the repository's GitHub Actions secrets, not from the session (ADR 0011). A live job that fails on a missing setting names it; then give the owner `docs/setup.md` Part 5.

## How work proceeds

- Several roadmap items may be in progress at once (owner decision, 2026-10-18; ADR 0033), each on its own branch with one pull request. Start an item only if it needs nothing from a pull request that has not merged yet. Earlier items in the roadmap go first. Keep at most three item pull requests open at a time.
- Do not stop at checkpoints (owner decision, 2026-10-16; ADR 0007). At every checkpoint in `docs/quality-gates.md`, run its review, fix what it finds, post the plain-English preview to the owner, and continue with the next item. An objection from the owner, whenever it comes, becomes the next item.
- For an **Opus** checkpoint, run the fresh-eyes review with a reviewer subagent on the Opus model.
- Stop and ask the owner only before something that cannot be undone or reaches outside the project: making data, pages, or the API public; sending anything to people outside the project; spending money beyond the daily caps in `config/models.toml`; creating accounts or keys. When every remaining item needs something only the owner can give (a key, an account, a judgment), stop and say exactly what is needed.
- Keep the owner's setup work to a minimum (owner request, 2026-10-17). Prefer designs that need no new account or key. When one is unavoidable, gather every account and key the next stretch of work needs into one sitting, give one small step at a time with exact names and copy-ready values, and check the result yourself instead of asking the owner to.
- If an approach fails twice, stop and explain the problem instead of trying a third variation.
- Every design choice the spec leaves open becomes an ADR in `docs/adr/NNNN-title.md` (context, decision, alternatives, consequences) and a line in the roadmap's decision log.
- Tick roadmap items in the pull request that finishes them.
- Branch from the latest main for each item; do not stack pull requests.
- Open each pull request as a draft. Mark it ready for review only when its work is done and the local checks pass. Marking it ready starts the Claude review (ADR 0006).
- The build overseer (ADR 0019) checks every pull request against the roadmap and these rules. Treat a blocking overseer finding like a blocking review comment: fix it, or explain on the pull request why not, and the owner settles it. Never remove a `hold` or `overseer-hold` label.
- The merge robot (ADR 0007) merges a ready pull request from a `claude/` branch into main once CI and the Claude review both succeed on its latest commit. When one pull request merges, merge main into another open pull request only when it conflicts with main or needs the merged work (a merge commit, never a rebase), then run the local checks again. Each such push to a ready pull request starts a paid review and an overseer run. A checkpoint's review runs once every item that leads to it has merged, on main as it then stands.
- Never approve or merge a pull request yourself, never push to main, and never weaken the review workflow, the merge robot, `scripts/review_verdict.py`, or `scripts/merge_gate.py` to get a merge through.
- The robot leaves for the owner pull requests that change the merge rules, the review, the rule documents, check settings, or spending caps (the list is in `scripts/merge_gate.py`). Keep such changes in their own small pull requests. For those and for Dependabot pull requests, run a separate reviewer subagent, then tell the owner in plain words what the change does and what the reviewer found.

## Development practices

| Practice | Rule |
| --- | --- |
| Test first for logic | Write the failing test before the code for verification rules, the publish gate, date-window parsing, entity matching, and free-versus-paid access. Tests are judged against the spec, not against your implementation. |
| Property-based tests | Use Hypothesis for parsers of phrases like "2H26", "mid-2027", "by year-end". |
| Recorded fixtures | Tests use saved copies of real filings and releases in `tests/fixtures/`, never live websites. |
| Prompt evaluations as tests | Any prompt or model change re-runs the gold-set evaluation; CI blocks a drop in any field's accuracy. |
| Tests are not loosened to pass | A pull request that edits an existing test explains why in its description. Never weaken a verification check or the publish gate to make a test pass. |
| Small pull requests | One roadmap item each, ideally under 400 changed lines. Use the PR template. |
| Protected main | CI and the Claude review must pass before the merge robot merges; no direct pushes to main (ADR 0006, ADR 0007). |
| Strict types, auto-formatting | pyright strict, tsc strict; ruff and prettier through pre-commit hooks. |
| Schema-first, reversible migrations | Change the Pydantic model, generate the Alembic migration, test the downgrade. Never edit production data by hand. |
| Idempotent jobs | Re-running any collector or extractor creates no duplicates; every job is safe to retry. |
| Reproducible setup | `uv` lockfile for Python, `package-lock.json` for web; one command starts a local copy with sample data. |
| Dependency hygiene | Pinned versions, Renovate or Dependabot update PRs, audit and gitleaks in CI. |
| Feature flags | New public features ship behind a flag and switch on after a staging check. |
| Observability | Structured logs, error tracking, and model-cost logging from the first job that runs. |

## Starting conventions (change only by ADR)

- **Layout:** `pipeline/` (Python package `catalyst`: collectors, agents, verification, API), `web/` (Next.js, from Phase 2), `tests/`, `docs/`, `scripts/`, `.github/workflows/`.
- **Python:** 3.12, `uv`, Pydantic v2, SQLAlchemy 2.0, Alembic, FastAPI, pytest, Hypothesis, ruff, pyright.
- **Database:** Supabase Postgres. Local development and CI use a disposable Postgres container, never the live database.
- **Scheduled jobs:** GitHub Actions workflows on New York time (twice each weekday, plus Saturday).
- **Model calls:** only through `ModelClient`. Provider and model per agent role live in a versioned config file, with a daily spend cap per role. A role at its cap stops and alerts; it never switches to a pricier model.

## Settings and secrets

Read settings only from environment variables. Names:

| Variable | Use |
| --- | --- |
| `CATALYST_ANTHROPIC_API_KEY` | The product's own model calls. Deliberately not `ANTHROPIC_API_KEY`, so coding sessions never bill to it. |
| `SUPABASE_URL` | Storage address |
| `SUPABASE_SECRET_KEY` | Server-side storage access |
| `SUPABASE_DB_URL` | Live database: Supabase's Session pooler string, with its `[YOUR-PASSWORD]` placeholder left in |
| `SUPABASE_DB_PASSWORD` | Live database password, filled into `SUPABASE_DB_URL` by the code |
| `CATALYST_DATABASE_URL` | A local or test database; wins over `SUPABASE_DB_URL` when set |
| `SEC_CONTACT_EMAIL` | Contact address in the SEC User-Agent header |

Never put a secret in code, prompts, logs, test fixtures, or pull request text. Never print a secret's value.

## Hard rules from the spec

- No published value without a claim, and no claim without a source document and a verbatim quote (spec: Provenance).
- Agents that read web text have no tools and no database access; only the plain-code publish gate writes live data (spec: Agent architecture).
- No personalized investment advice anywhere: no buy/sell ratings and no portfolio-specific answers. The one exception to "no price targets" is the catalyst-scenarios feature (spec: Catalyst scenarios; owner decision 2026-10-17): conditional price-move ranges, never a single target, each with every input shown and the "estimate, not financial advice" notice.
- The API and MCP never return continuous price or return series (data license limit).
- Respect robots.txt and site terms; rate-limit every source; SEC EDGAR at no more than 10 requests per second.

## Pull request description (what the owner reads)

Use `.github/pull_request_template.md`. In plain English: what changed, how it was tested (with numbers), risks, anything needed from the owner, and the checkpoint it belongs to.
