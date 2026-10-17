# Evaluation set: extract_catalysts

The extractor's prompt is scored here (ADR 0014). AGENTS.md: a prompt or model change re-runs this evaluation, and CI blocks a drop in any score.

| File | What it is |
| --- | --- |
| `documents/<sha256>.htm` | 12 recorded press releases (SEC 8-K Exhibit 99.1) from the Gate 1 rehearsal |
| `gold.jsonl` | One line per release: the events a person should find, each with its type, drug, date words and exact quote. 20 events in 6 releases; 6 releases have none. Prepared by an agent; `confirmed_by_owner` turns true as the owner confirms each one |
| `scores.json` | The latest scores and exactly what earned them (prompt version, model, gold set fingerprint). Absent until the first paid run |
| `baseline.json` | The lowest score allowed for each measure. Absent until the first scores are accepted |

To score the current prompt (about 12 model calls, a few dimes): `uv run catalyst eval-extractor --out evals/extract_catalysts/scores.json`, with `CATALYST_ANTHROPIC_API_KEY` set. This runs in GitHub Actions, never in a coding session.
