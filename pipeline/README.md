# pipeline — the `catalyst` Python package

**Purpose.** Collectors, agents, verification, the publish gate, and the API (spec: Agent architecture). Today it holds the walking skeleton: one SEC filing from source to database.

**Owner.** Collectors are plain code; no AI agent runs in this package yet.

| Module | Job | Inputs | Outputs |
| --- | --- | --- | --- |
| `catalyst.settings` | Read settings from environment variables | Environment | Values, or an error naming the setup step |
| `catalyst.schemas` | Pydantic models for every entity; the source of truth for data | — | Validated records |
| `catalyst.db` | Tables generated from the models (`tables`) and idempotent writes (`repository`) | Records | Database rows |
| `catalyst.data_dictionary` | Writes `docs/data-dictionary.md` from the models | Models | Markdown |
| `catalyst.snapshots` | Private copies of source documents, named by SHA-256 | Bytes | Hash and storage key |
| `catalyst.collectors.ratelimit` | Request spacing, capped at the SEC's 10 per second | — | Waits |
| `catalyst.collectors.sec.edgar` | EDGAR filing lists, index pages, polite HTTP client | SEC responses | Filing and exhibit references |
| `catalyst.collectors.sec.press_release` | Walking skeleton: latest 8-K press release to one row | Company CIK | One `source_document` row |
| `catalyst.collectors.http` | Shared polite HTTP client: allowed hosts, per-host rate limits, robots.txt for web pages | URLs | Responses |
| `catalyst.collectors.sec.filings` | SEC 8-K press releases, 10-Q, 10-K, S-3, 424B documents | EDGAR filing list | Documents |
| `catalyst.collectors.sources` | ClinicalTrials.gov, openFDA, Federal Register, wire RSS | Sponsor names, dates, feeds | Documents |
| `catalyst.collectors.run` | Runs every collector for a company list; one failing source never stops the others | `config/pilot_companies.toml` | Rows plus counts |
| `catalyst.collectors.report` | CP3 preview: latest document per company and source | Database | Markdown |
| `catalyst.logs` | JSON log lines | Events | Standard error |
| `catalyst.llm` | `ModelClient`, provider adapters, prices, daily spend caps, prompt files | Role, prompt, document | Validated JSON plus cost; a `model_call` row per call |
| `catalyst.api` | Read-only data API (`catalyst api`, localhost, `/v1`) and MCP server (`catalyst mcp`, stdio) over published data only; free versus paid shaping (ADR 0025) | Published catalysts, their change rows and drug links | JSON with sources and the not-financial-advice notice |
| `catalyst.cli` | `uv run catalyst ...` commands | Arguments | JSON on standard output |

**Run it.** `scripts/demo.sh` (needs Docker, `uv`, and `SEC_CONTACT_EMAIL`). Every collector: `uv run catalyst collect --since 2026-07-01`, then `uv run catalyst report`. Check every stored copy against its hash: `uv run catalyst snapshots-verify` (add `--snapshot-store supabase` for the live bucket; ADR 0008).

**Test it.** `eval "$(scripts/local_db.sh)"` once, then `uv run pytest`. Tests use recorded copies of real responses in `tests/fixtures/`, never live websites; `scripts/record_fixtures.py` records them.
