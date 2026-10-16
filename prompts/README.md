# Prompts

Each prompt is a versioned file: `prompts/<id>/<version>.toml` (for example `prompts/extract_catalysts/1.0.0.toml`).
A new wording is a new version; old versions stay so any stored claim can be traced to the exact prompt that produced it.

Required fields, checked by `catalyst.llm.prompts.load_prompt`:

| Field | Meaning |
| --- | --- |
| `id`, `version` | Must match the file path. Version is `MAJOR.MINOR.PATCH`. |
| `purpose` | One sentence: what the prompt is for. |
| `eval_set` | Path of the evaluation set the prompt is scored on. |
| `tested_models` | Models it has been evaluated with. |
| `system` | The instructions. Never include a secret. |
| `output_schema` | JSON Schema of the answer. Every object sets `"additionalProperties": false`. |

The first real prompts arrive with the extractor agents (Phase 1).
