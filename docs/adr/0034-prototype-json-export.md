# 0034 — One JSON export of the live data for the private prototype

- **Status:** accepted
- **Date:** 2026-10-18
- **Roadmap item:** none; supports the private prototype (owner request, 2026-10-18)

## Context

The owner's private prototype needs the live data as one file. A GitHub Actions job, written separately, will run a command against the live database and upload the file. The spec does not say what such a file holds, how it relates to the read API (ADR 0025), or whether claims the publish gate holds may appear in it. The same job collects only SEC filings for every company in `config/coverage_companies.toml`, which `catalyst collect` could not do: it always ran every collector.

## Decision

1. **`catalyst export-catalysts --out PATH [--include-held]`** writes one UTF-8 JSON file, indented by one space, in a transaction the database keeps read-only and at one snapshot (repeatable read).
2. **Published catalysts come from the read layer.** `catalyst.api.read.published` returns every catalyst of any status, each exactly as `GET /v1/catalysts/{id}` shows a paid caller. The rule of ADR 0025 holds: a record with a value no source states is left out and logged. No new SQL reads published data.
3. **Each source names its claim.** The read API's `SourceView` gains `claim_id`, the claim of the change row that cites the source. The read layer already reached claims only through change rows, so this names no claim it did not already show.
4. **Held claims are opt-in and apart.** With `--include-held`, a top-level `held` list holds every claim whose stored publish decision is `review`. Each item carries `"status": "held for review"`, its company when its names matched exactly one, its extracted values, quote, source, and the gate's stored reasons. Held items never enter `catalysts`.
5. **Shape.** Top level: `notice` (the read API's not-financial-advice text), `generated_at` (UTC), `counts` (`published`, `held`, and `companies`, which counts companies with a published catalyst; `held` is empty without the flag), `prompt_versions` (the extractor and verifier prompts the gate requires now), `catalysts`, and, with the flag, `held`. Both lists are sorted by window start, then company name, then id. No price or return series.
6. **Standard output is the counts only:** one line of JSON with no claim, quote, or held item, since the job shows it in its summary.
7. **Trials are not exported yet.** The read API does not show them (ADR 0025, decision 3), and the export shows what the read API shows. *Update 2026-10-20:* the read API now shows trials, so the export does too, and held claims list their exactly matched trials (ADR 0038).
8. **`catalyst collect --sources sec,ctgov,openfda,fedreg,wire`** runs only the named collectors. Without the flag every collector runs, as before. An unknown name stops the command, naming the valid ones. Any per-company source still reads the company's SEC record, which names the company. When that read fails, the failure is listed under `sec:<ticker>` and the run goes on to the next company. Before, one company's failure stopped the whole run.

## Alternatives

- **A separate query for the export.** It would let the export and the API disagree about what is published. Reusing the read layer keeps one rule.
- **Held claims mixed into `catalysts` with a flag field.** A reader that ignored the flag would show unverified facts as published.
- **Claim ids only in the export, not the API.** That needs a second serializer. Adding the id to the shared shape is smaller, and an API caller can cite a claim too.

## Consequences

- The export file holds held, unverified claims when `--include-held` is used. It is for the private prototype only and must not be made public.
- When the read API starts showing trials, the export shows them with no change here.
- A `claim_id` is now part of every API and MCP source.
