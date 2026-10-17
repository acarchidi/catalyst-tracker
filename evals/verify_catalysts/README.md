# Second-reading evaluation

The verifier prompt (`prompts/verify_catalysts/`) is scored on the extractor's gold set, `evals/extract_catalysts/gold.jsonl`, with the same scorer and counting rules (see that folder's README).

Its scores live here, in `scores.json`, and its floor in `baseline.json`. Run `catalyst eval-extractor --prompt verify_catalysts --out scores.json` (costs money; about 12 model calls). Until this folder holds scores that match the current prompt and model, the second reading does not run (ADR 0018, part 2).
