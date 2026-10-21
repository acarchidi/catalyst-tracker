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
7. **Trials are not exported yet.** The read API does not show them (ADR 0025, decision 3), and the export shows what the read API shows. *Amended 2026-10-20 (ADR 0039):* the read API now shows each catalyst's trials, and the export matches it except that each trial also carries a `registry` object with its phase and size, labelled as registry data and not a quoted claim, which the read API never returns; held claims list their exactly matched trials the same way.
8. **`catalyst collect --sources sec,ctgov,openfda,fedreg,wire`** runs only the named collectors. Without the flag every collector runs, as before. An unknown name stops the command, naming the valid ones. Any per-company source still reads the company's SEC record, which names the company. When that read fails, the failure is listed under `sec:<ticker>` and the run goes on to the next company. Before, one company's failure stopped the whole run.

9. **Sealed, never plain, outside the job (amended 2026-10-20).** The owner made the repository public on 2026-10-20, so its job logs, job summaries, and artifacts are public. The job printed the export to its log only while `github.event.repository.private` was true, so it stopped printing (it fails closed), and the owner's private page lost its feed. The export now leaves the job only sealed:
   - The coding session runs `catalyst seal-keygen --private-key FILE` in its scratchpad. It writes a new RSA-4096 private key (readable by its owner only, never committed, never in a log) and prints the public key as one line (base64 DER).
   - The workflow takes that line as an input. `catalyst seal --to LINE --in FILE` prints the file sealed: a fresh 256-bit AES-GCM key per file, wrapped with RSA-OAEP (SHA-256); the format line and wrapped key are bound to the ciphertext, so any change is detected. Keys under 3072 bits, or not RSA, are refused. Without a valid key the job prints nothing of the file.
   - The session downloads the log and runs `catalyst unseal --private-key FILE --in LOG --out FILE`; it reads the sealed block straight from a log with time stamps. It deletes the private key when done, and makes a new pair for the next export.
   - The extract mode's full results (unchecked proposals with quotes) use the same input; without it they are not printed.
10. **Held items for the review queue (amended 2026-10-20).** The owner's private review queue reads only the decrypted export, with no database access. Each held item gains `paragraph`: the line of the stored copy's text (one line per block element, ADR 0012) that holds the quote, cut to 600 characters on each side of a long quote with "…"; it is read only when the quote is still at its stored place in a copy whose hash matches, and otherwise is null with `paragraph_missing` giving the reason. Each held item also has `review_decision`, null until the review queue (PR #84) adds its `review_decision` table and fills the hook `catalyst.export.review_decisions` with the latest decision (action, who, when, reasons code). `export-catalysts` takes `--snapshot-store` and `--snapshot-dir` for this, like the other commands that read copies.

## Alternatives

- **age instead of the code here (decision 9).** age is the standard tool for exactly this, but it is not on the runner or in the lockfile, so CI could not test the path the job takes. The `cryptography` package is already locked, and the scheme is a textbook one.
- **An encrypted artifact instead of the log.** Artifacts of a public repository are downloadable by anyone signed in, so they need the same sealing; the log keeps one path for both modes.
- **A long-lived key in the repository's secrets.** Decryption happens in the coding session, not in Actions, so a secret there would not help, and a long-lived key widens what one leak exposes. A one-off pair per export needs no new account or secret.
- **A separate query for the export.** It would let the export and the API disagree about what is published. Reusing the read layer keeps one rule.
- **Held claims mixed into `catalysts` with a flag field.** A reader that ignored the flag would show unverified facts as published.
- **Claim ids only in the export, not the API.** That needs a second serializer. Adding the id to the shared shape is smaller, and an API caller can cite a claim too.

## Consequences

- The export file holds held, unverified claims when `--include-held` is used. It is for the private prototype only and must not be made public. Since 2026-10-20 it leaves a job only sealed (decision 9).
- Two earlier runs, made while the repository was private, printed the export or the extraction results in plain text, and their logs are now public: Extract catalysts runs 39158134190 and 39329101142. The owner decides whether to delete those logs; nothing here deletes them.
- Sealed copies sit in public logs for as long as GitHub keeps them (90 days by default). They are safe while the private key stays secret, which is why each pair is used once and deleted.
- When the read API starts showing trials, the export shows them with no change here.
- A `claim_id` is now part of every API and MCP source.
