# Evaluation set: extract_catalysts

The extractor's prompt is scored here (ADR 0014). AGENTS.md: a prompt or model change re-runs this evaluation, and CI blocks a drop in any score.

| File | What it is |
| --- | --- |
| `documents/<sha256>.htm` | 12 recorded press releases (SEC 8-K Exhibit 99.1) from the Gate 1 rehearsal |
| `gold.jsonl` | One line per release: the events a person should find, each with its type, drug, date words and exact quote. 20 events in 6 releases; 6 releases have none. Prepared by an agent; `confirmed_by_owner` turns true as the owner confirms each one |
| `scores.json` | The latest scores and exactly what earned them (prompt version, fingerprint of the prompt's words, model and its settings, gold set fingerprint). First earned 2026-10-17 |
| `baseline.json` | The lowest score allowed for each measure. Absent until the first scores are accepted |
| `unscored.json` | Retired on 2026-10-17, when the first scores were earned. Before that it named the exact prompt words and model allowed to ship unscored |

To score the current prompt (about 12 model calls, a few dimes): `uv run catalyst eval-extractor --out evals/extract_catalysts/scores.json`, with `CATALYST_ANTHROPIC_API_KEY` set. This runs in GitHub Actions, never in a coding session.

## Counting rules (owner rulings, 2026-10-17)

The owner answered five questions about how events are counted. These rules apply to the gold set and to the extractor's prompt.

1. **Two trials in one sentence are two events.** Mirum: "AZURE-1 and AZURE-4 ... topline results expected in Q3 and Q4 2026, respectively" gives one event per trial, each with its own date.
2. **One drug tested in two diseases is two events.** Spyre: SPY072 "in psoriatic arthritis (PsA) and axial spondyloarthritis (axSpA)" gives one event per disease.
3. **A trial of several drugs names every drug; the drug field is never left empty for it.** Spyre's SKYLINE Part B tests SPY001, SPY002 and SPY003 alone and in pairwise combinations, "six investigational agents in total" in the company's words. The record:
   - names every drug and combination, each from a quoted sentence in the source;
   - says which are tested alone and which in combination;
   - shows the company's own wording first, with a control that expands the full list without letting one cell take over the page;
   - is found by a search for the company, for any one of the drugs, or for any combination; a search for one drug shows that drug first.
   The gold set still has this event's drug empty. It changes when the drug field becomes a list (roadmap: entity resolver), which needs a new prompt version and a new scoring run.
4. **Data with no named meeting is a trial readout, not a conference presentation.** TG Therapeutics: "Present preliminary Phase 1 azer-cel data in Progressive MS in the second half of 2026."
5. **The dated event is the data, not a later approval.** Mirum: "LIVMARLI EXPAND Phase 3 study ... topline results expected in Q4 2026" is a trial readout, not a label expansion.

These rulings confirm rules, not every gold entry, so `confirmed_by_owner` stays false until the owner checks each release's full list of events.
