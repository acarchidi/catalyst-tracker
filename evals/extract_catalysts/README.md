# Evaluation set: extract_catalysts

`gold.jsonl` will hold hand-checked catalysts for recorded press releases, one document per line. The extractor's answers are scored against it field by field, and CI blocks a prompt or model change that lowers any field's score (AGENTS.md: Prompt evaluations as tests).

It arrives in the third part of the roadmap item "Extractor agents and prompt evaluation sets". Agents prepare the entries; the owner confirms them (spec: Gold set).
