# 0014 — The extractor's gold set, its scores, and the gate that blocks a drop

- **Status:** accepted
- **Date:** 2026-10-17
- **Roadmap item:** Phase 1, "Extractor agents and prompt evaluation sets" (third part)

## Context

AGENTS.md: "Any prompt or model change re-runs the gold-set evaluation; CI blocks a drop in any field's accuracy." The spec's gold set is 200 hand-verified catalysts that agents prepare and the owner confirms; that is its own roadmap item. CI has no product key, by design (the coding session and CI never bill to `CATALYST_ANTHROPIC_API_KEY`), so CI cannot call the model itself.

## Decision

1. **A first evaluation set** in `evals/extract_catalysts/`:
   - 12 recorded press releases (SEC 8-K Exhibit 99.1) from the Gate 1 rehearsal, stored as `documents/<sha256>.htm`.
     - 8 were drawn with seed 2026 from releases with many forward-looking words.
     - 4 were drawn from releases with few.
     - All are under 30,000 characters of text.
   - `gold.jsonl` holds 20 events in 6 of the 12 releases; the other 6 have none.
   - A separate agent labeled them from the same event definitions as the prompt, without seeing the prompt's wording. A script checked every quote against the text.
   - Each entry has `confirmed_by_owner: false` until the owner confirms it. That happens in the "Gold set of 200 verified catalysts" item.
2. **Matching and scores** (`catalyst.extract.evaluate`). A proposal matches a gold event when their quotes overlap in the text. Failing that, it matches when both have the same type and the same window. Six scores, from 0 to 1, add up events across documents:
   - recall: gold events found;
   - precision: proposals that are gold events;
   - type, window, and drug: right on the matched pairs;
   - quote_found: proposals whose quote is in the source.
3. **Earning a score.** `catalyst eval-extractor --out scores.json` runs the extractor on the 12 releases with the product key, in GitHub Actions. It costs about 12 model calls. The record names the prompt, its version, the model, and the SHA-256 of `gold.jsonl`, and the coding session commits it as `evals/extract_catalysts/scores.json`.
4. **The gate** (a test in CI, `tests/extract/test_eval_gate.py`) fails when:
   - the prompt version is past 1.0.0 and has no scores;
   - `scores.json` was earned with another prompt version, another model, or another gold set;
   - any score is below `baseline.json`.
   The first version, 1.0.0, may ship unscored. Its first scores become the baseline.

## Alternatives

- **Replay recorded model answers in CI.** It checks the scorer, not the prompt: a new prompt has no recorded answers. Recorded answers are used only to test the scorer.
- **Give CI the product key.** Every pull request would spend money, and the product key would sit in a workflow that runs on code nobody has merged yet.
- **Wait for the 200-catalyst gold set.** That blocks the gate for weeks. Twelve releases are enough to catch a broken prompt, though not to measure small changes.

## Consequences

- Changing the prompt or the extractor's model now takes one paid evaluation run and a committed `scores.json`, or CI stays red.
- `baseline.json` is the floor. Lowering it would weaken the gate. It should become an owner-merged path in `scripts/merge_gate.py`, which is itself an owner-merged change. Until then, the reviewer is asked to flag any change to it.
- Twelve releases give coarse scores: one event is 5% of recall. The 200-catalyst set replaces them.
- Conference dates written as ranges ("November 16-19, 2026") get no window yet (ADR 0012), so a gold event with one scores its window only when the proposal also has none.
