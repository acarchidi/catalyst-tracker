# 0015 — Entity resolver: exact matching in plain code, ambiguity goes to review

- **Status:** accepted
- **Date:** 2026-10-17
- **Roadmap item:** Phase 1, "Entity resolver and reference tables"

## Context

The spec's entity resolver "maps names to canonical company, drug, and trial IDs". It reads reference tables, never writes live data, and sends ambiguous matches to review. A Sonnet-class model handles "ambiguous cases only". AGENTS.md requires tests written first for entity matching.

Sources name the same thing in many ways:
- SEC writes "VERTEX PHARMACEUTICALS INC / MA"; a press release writes "Vertex".
- Code names appear as "SPY001", "SPY-001", and "SPY 001".
- Study names appear as "AZURE-1" and "AZURE 1".
- Tickers change hands: AGLE belonged to Aeglea (now Spyre) until 2023.

On 2026-10-17 the owner ruled that a trial of several drugs names every drug and combination, marks each as tested alone or in combination, and is found by a search for any of them (`evals/extract_catalysts/README.md`, rule 3).

## Decision

1. **Plain code, exact rules, no model in this item** (`catalyst.resolve`). A name matches once spelling is set aside: case, punctuation, spacing, trademark signs, and, for companies, legal-form words and EDGAR's state suffix (the coverage list's `normalize_name`). A partial name never matches. A model may later suggest candidates for unknown names, but its suggestion goes to review and never resolves a match by itself.
2. **Each lookup returns a status.**
   - Resolved: exactly one candidate.
   - Ambiguous: several candidates. All of them are kept for review.
   - Unknown: none.
   - Each result also names the rule that found its candidates.
3. **Companies.**
   - An SEC number (CIK) decides alone. An unknown CIK is an unknown company, never a cue to try the name.
   - Otherwise, a ticker matches the company that held it on the document's date (today's tickers when no date is given).
   - Otherwise, the current or any former name matches.
4. **Drugs.**
   - A generic (INN) or brand name matches for any company, because licensed drugs change hands.
   - A code name matches only the given company's drugs, because two companies can use the same code. With no company given, a code name two companies share is ambiguous.
5. **Trials.**
   - A ClinicalTrials.gov number in the text matches exactly. Two numbers in one text are ambiguous.
   - Otherwise, a study name matches within the given company's trials.
6. **Planned parts of this item**, each its own pull request:
   - **Several drugs per catalyst:**
     - a `catalyst_drug` table links each catalyst to each drug it concerns, marked alone or in combination;
     - a `drug_component` table names a combination's parts, only where a source states them;
     - each link is backed by a claim with a quote. Combination codes such as SPY120 are shown as the company wrote them, and their parts are never inferred from the code.
   - **Fill the reference tables during collection** (`catalyst.references`), from records the run already fetches:
     - company names, former names with their dates, and tickers, from each SEC company record. SEC lists only current tickers, so a ticker's start date is the first day we saw it. A ticker SEC stops listing is closed the day before, and one that moves to another company is closed for its old holder first.
     - trials, study names (the registry's acronym), and phases, from each ClinicalTrials.gov record. A trial's sponsors are the companies its registry records are linked to.
     - Registry intervention lists are **not** imported as drugs. Insmed's records, for example, list comparators (azithromycin), placebos ("ELC (matching placebo for ALIS)"), and short forms ("ETH"), any of which would match the wrong drug. Drugs come from the company's own documents in the next part, and registry names may later become aliases of a drug already known.
   - **Resolve extracted claims** (`catalyst.resolve_claims`, run right after each extraction and as `catalyst resolve-claims`). Each name in an extractor claim gets one `entity_match` row: resolved (with its record), ambiguous (with every candidate), or unknown. Later runs update the row as the reference tables grow.
     - The company is the one the claim's source document is linked to, recorded as `cik` when the link is the SEC filer's own and `document_link` when it came from a registry or FDA sponsor name.
     - The drug name is matched by decision 4. A code name that belongs to another company is ambiguous, with that company's drug as the candidate, never a second drug. When the name is unknown everywhere, the company is known, the name has the shape of a code name (letters, an optional dash or space, then digits, as in SPY001 or VX-548; not a disease, target, or year such as COVID-19, IL-23, or FY2026), and the quote contains it as a whole word in capitals, the job creates that company's drug and records the match as `new_code_name`; the marker stays while that drug is the match. Other unknown names (generic or brand names, lists of drugs) wait for review: code cannot tell their kind reliably.
     - Trials are every registry number in the quote, each reported on its own, and, only when the company is known, every study name of the company's trials found as a whole word with the same capital letters as the registry gives it, so "ARISE" the trial matches and "questions may arise" does not. An unknown registry number is kept as unknown. (`resolve_trial`, which answers one mention, still treats two numbers in one text as ambiguous.)
     - Names no longer found on a later run lose their old match. A claim that fails is logged and skipped; the rest still match.
     - Ambiguous and unknown matches are what the review-queue item shows.

## Alternatives

- **Fuzzy string matching** (edit distance, token overlap). It turns "SPY001" into "SPY002" and "AZURE-1" into "AZURE-14". A wrong link publishes a wrong fact under a real company, which is worse than a review item.
- **A model resolves every name.** It costs a call per name and cannot be checked by a test. It also adds a model's errors to a step that exact rules handle for nearly every name.
- **Global code names.** Two companies can use the same code name, so matching a code name without its company would link one company's catalyst to another company's drug.

## Consequences

- Every resolved link can be explained by one rule, and tests pin each rule.
- New spellings that the rules do not cover (an abbreviation such as "BMS") go to review until a reference table records them, for example as a former name or an alias. The review queue will show how often this happens. If it happens often, a model that suggests candidates for review becomes worth building.
- The catalyst's single `drug_id` is replaced in the multi-drug part, by a reversible migration.
