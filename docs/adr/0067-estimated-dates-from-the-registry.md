# 0067 — Estimated dates from the registry, as registry-field claims

- **Status:** accepted
- **Date:** 2026-11-24
- **Roadmap item:** Phase 1, "Trials and their sizes on each catalyst", new sub-item "Estimated dates from the registry" (owner decision 2026-11-23)
- **Needs first:** the spec change on `claude/estimated-dates-spec` (Provenance rule 9, Date precision, Catalyst types), an owner path

## Context

The owner decided on 2026-11-23: for covered companies' trials that have no company-stated date, show the registry's estimated primary completion date as a catalyst, "do #4 but just say 'Estimated'".

The spec already names ClinicalTrials.gov as the source of primary completion dates and "registry estimate" as a date label. But its Provenance rules ask for "the sentence that states it" and a quote checked by exact match against the stored copy (rules 1 and 4). A registry field is not a sentence. ADR 0039 kept registry phase and size out of published records for that reason, and ADR 0053 (decision 4) and the roadmap's cohort-size item say registry-field claims need an owner-only spec change first. So this does need a spec change. It is on its own branch, `claude/estimated-dates-spec`, and only adds wording: Provenance rule 9, a "Estimated dates from the registry" paragraph under Date precision, and the new type in Catalyst types.

Facts that shape the design:

- The collector stores each registry record as compact JSON with sorted keys (`ctgov.to_document`), and `document_text` reads a JSON copy as it is. So a field is a plain substring of the stored copy's text: `"primaryCompletionDateStruct":{"date":"2027-03","type":"ESTIMATED"}`.
- Each stored record is linked to a covered company only by its exact lead sponsor (ADR 0005, ADR 0009).
- Primary completion is when the last primary measurement is due, not when results come out (roadmap, Later: "Estimated results-reporting window").
- Only 1 of 147 published catalysts was linked to a trial on 2026-11-10 (ADR 0053), so a company date for a trial is rare today.

## Decision

1. **Registry-field claims** (spec: Provenance, rule 9). A claim's quote is the field exactly as the stored copy writes it, at its place in the copy's text for the current text version. Its value records the field's path, the value, the trial's number, and the text version. Agent `registry_field`, prompt `registry_field/1.0.0` (the version of these rules, not a prompt), provider `plain_code`, model `catalyst.publish.estimates`. Before any claim is stored, plain code checks:
   - the copy's bytes match its recorded SHA-256 and its key;
   - the record is the trial's own (its `nctId`);
   - the field's text stands exactly once in the copy, so it has one place (`field_quote`);
   - the quote is at its stored place (`gate.match_quote`, the publish gate's own quote check);
   - the field at the path holds the claim's value (`registry_field_at_path`).
   Both outcomes are stored as claim checks. No model reads anything.
2. **A new catalyst type, `primary_completion`** ("Primary completion"), written only by this job. It is not `topline_readout`, because the registry does not say when data come out. The extractor's prompts do not offer it. Its only type word is the registry field's name, so every check on releases reads them exactly as before. The planted-error test plants as wrong types only the types a reader can propose (`REGISTRY_ONLY_TYPES` is left out, and a test holds both reader prompts to that), so its 221 wrong-type copies and its scores are unchanged.
3. **Which trials.** The latest stored registry record of the trial (by last-update date) must be linked to exactly one covered company, say the trial is not yet recruiting, recruiting, enrolling by invitation, or active, not recruiting, and list phase 2, 3, or 4 (phase 1/2 counts). This follows the spec's Catalyst types: "topline readouts (phase 2 and later ...)" (agent's proposal; the owner may widen it).
4. **The entry.** Window from the registry's date: "2027-03" is the whole month, "2027-03-15" the day; anything else gives no entry. Date label `registry_estimate`. Raw text the registry's own "2027-03". Title from checked fields: "Primary completion: ARISE trial" (study name, else the number). The trial link and every change row cite the claim. Its source link is the trial's registry page.
5. **"Estimated" in print.** The weekly issue writes a registry estimate as "When: Estimated March 2027." (or "Estimated 15 March 2027."), replacing "The trial registry estimates completion in ...". Its source link carries no text fragment, since the trial page does not print the field. Its colour is 🟨 Trial progress.
6. **A company-stated date always wins.** A company or regulator date is an open published catalyst of the same company, with date label company-confirmed or regulator-scheduled, linked to the trial. A trial with one never gets an estimate. When one appears later, the estimate's status becomes `replaced`, with a change row citing the claim behind the company's date. A replaced entry never comes back.
7. **Later records.** A newer estimate moves the window, each change citing the newer claim, so the old date stays in history. A move of more than 6 months, or to a window that has passed while the published one has not, is held and counted (spec: Change sanity); until the review queue's parts 2 and 3 exist, no person can approve it, so the old estimate stays. A record that gives the actual date, or says the trial was terminated, withdrawn, or suspended, resolves the entry, citing a claim on that field.
8. **Past dates.** A window that has passed is never written. A published estimate whose window passes is left as it is; the read API and the issue already list only windows that have not ended, so it is never shown as upcoming.
9. **Schema.** `catalyst_type` gains `primary_completion` and `status` gains `replaced` (migration `e5d2a7c41b93`); the downgrade refuses while any catalyst holds either value.
10. **Behind a switch.** The job is `catalyst publish-estimates`, run under the catalyst writer's lock. No workflow runs it yet (AGENTS.md: feature flags). Switching it on is a workflow step in its own owner-path pull request, after a staging check.

## Alternatives

- **Show the estimate on a "topline data" entry.** Shorter, but it says something the registry does not: primary completion is not results.
- **Keep registry values in the private export only (ADR 0039).** Safe, but it does not do what the owner decided.
- **A model reads the registry record.** Needless: the field is structured, and plain code can check it exactly.
- **Quote a sentence from the trial's web page.** The page is built by scripts from the same record; the stored record is the copy we hold and hash.
- **Let any same-company catalyst for the trial's drug count as a company date.** A drug often has several trials, so one company date would hide other trials' estimates. Only an exact trial link counts.
- **Delete a replaced estimate.** The catalyst table keeps its history; `replaced` keeps the record and says why it is not shown.

## Consequences

- Registry-field claims are now defined, so the cohort-size and registry-phase item can use the same claim and check.
- **Volume (a guess):** the coverage list counts 19,876 trials for 466 companies. If about 15 to 20% are still running, about 60% of those are phase 2 or later, and most carry a future estimate, about 1,500 to 2,000 trials would gain an "Estimated" entry, against about 150 published catalysts today. Big sponsors dominate (Bristol Myers Squibb alone lists 854 trials). Fewer show until the registry records are all stored (the sponsor-wide run timed out in ADR 0040).
- That many entries would fill the weekly issue: about 70 a month, so the free issue's 25-entry list and the paid issue's 200 could be mostly estimates. The owner should see a staging export before switching on, and may want them counted rather than listed, or listed in their own section.
- The weekly sample checker (ADR 0041) reads sources for sentences, which a JSON field is not, so it would report estimates as disagreements. Before switching on, either the sample leaves registry-field records out (their checks are exact) or it learns to check a field. That is a change to an audit, so it needs the owner's word.
- An estimate the registry moves more than 6 months stays stale until a person can approve the move.
- **The staging check is a dry run (added 2026-11-25).** `catalyst publish-estimates --dry-run` takes every step and check of the real job (the copy's hash and key, the field written once at its path, the value at the path, the phase and status filter, company date wins, the passed-date rule, the 6-month hold) inside a transaction the database itself keeps read-only (`SET TRANSACTION READ ONLY`, the read API's own `read_session`), so Postgres refuses any write; a test checks every row and sequence is unchanged afterwards and that the real job, run next, does exactly what the dry run counted. It prints counts only: would create, move, hold (over 6 months, or passed), close, be replaced by a company date; skips by reason; new or moved estimates by month for the next 12 months; and the top 20 companies. `--sample-out` writes up to 50 new or moved entries, spread evenly across the dates (company, ticker, trial number and title, phase, "Estimated March 2027", registry link). The `estimates-sample` mode of `extract-live.yml` runs it on the live database inside the `live-database` concurrency group, without migrating; the counts go to the job summary, and the sample leaves the job only sealed to the run's one-off public key, the same `catalyst seal` step as the export (ADR 0034, decision 9). Without a key it stops before reading anything. The coding session opens the sealed text with `catalyst unseal` and shows the owner the rows privately. Switching the job on is still its own owner-path change.
- Revisit when the review queue exists, when the results-reporting window (roadmap, Later) is built, or if the owner wants phase 1 trials too.
