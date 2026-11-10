# Indication evaluation: both readers together

A catalyst's indication is found by two models and plain code (ADR 0053, decision 2, as amended on 2026-11-10):

1. Plain code offers the visible sentences of the stored release that name the drug.
2. The first reader (`prompts/indicate_catalysts/`, extractor role) picks one and copies the disease.
3. Plain code checks the disease is written in that sentence, has no brackets, and names one MeSH disease.
4. The second reader (`prompts/check_indication/`, verifier role) says whether that sentence states this drug is aimed at that disease for this event.

`indications.jsonl` labels each event of the extractor's gold set (`evals/extract_catalysts/gold.jsonl`), in order. Each line has:

- `sha256` and `event`: the gold document and the event's place in it.
- `disease`: the right disease in plain words, or null when the release states none.
- `mesh`: the MeSH names an answer may carry and count as right. It is empty when no single MeSH disease is right, so nothing should publish.
- `note`: why the label is what it is.

Scores (`catalyst eval-extractor --prompt indicate_catalysts --out scores.json`, at most 36 model calls, two per event):

- `found_right`: of events with an accepted MeSH name, the share found with one.
- `none_right`: of events with none, the share left without an indication.
- `wrong`: of all events, the share given a MeSH disease not accepted. A wrong indication would be published, so this one matters most.

Known limits, written into the labels: abbreviations such as "UC", "PsA" and "Progressive MS" are never matched, so events whose only stating sentence uses one cannot publish. Two SPY072 events share one sentence naming two diseases; they are scored strictly, and plain code refuses an indication when the event's own quote names more than one disease.

Current scores (`scores.json`, run 42906242447): found_right 0.133 (2 of 15), none_right 1.0 (3 of 3), wrong 0.0 (0 of 18).

The scores are recorded here, not floored yet: an indication is not published until a later pull request adds the writer, and that pull request sets the floor.
