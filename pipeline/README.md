# pipeline — the `catalyst` Python package

**Purpose.** Collectors, agents, verification, the publish gate, and the API (spec: Agent architecture). Today it holds the walking skeleton: one SEC filing from source to database.

**Owner.** Collectors are plain code; no AI agent runs in this package yet.

| Module | Job | Inputs | Outputs |
| --- | --- | --- | --- |
| `catalyst.settings` | Read settings from environment variables | Environment | Values, or an error naming the setup step |
| `catalyst.schemas` | Pydantic models; the source of truth for data | — | Validated records |
| `catalyst.db` | Tables (`models`) and idempotent writes (`repository`) | Records | Database rows |
| `catalyst.snapshots` | Private copies of source documents, named by SHA-256 | Bytes | Hash and storage key |
| `catalyst.collectors.ratelimit` | Request spacing, capped at the SEC's 10 per second | — | Waits |
| `catalyst.collectors.sec.edgar` | EDGAR filing lists, index pages, polite HTTP client | SEC responses | Filing and exhibit references |
| `catalyst.collectors.sec.press_release` | Walking skeleton: latest 8-K press release to one row | Company CIK | One `source_document` row |
| `catalyst.logs` | JSON log lines | Events | Standard error |
| `catalyst.cli` | `uv run catalyst ...` commands | Arguments | JSON on standard output |

**Run it.** `scripts/demo.sh` (needs Docker, `uv`, and `SEC_CONTACT_EMAIL`).

**Test it.** `eval "$(scripts/local_db.sh)"` once, then `uv run pytest`. Tests use recorded copies of real SEC documents in `tests/fixtures/`, never live websites.
