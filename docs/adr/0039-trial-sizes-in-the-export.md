# 0039 — Trial sizes from the registry; trials on each catalyst, and their sizes in the private export only

- **Status:** accepted
- **Date:** 2026-10-20
- **Roadmap item:** Phase 1, "Trials and their sizes on each catalyst" (owner request, 2026-10-20)

## Context

The owner's private catalyst page should list, for each company's entries, the drugs, the indication sought, and the size of each trial where known. Drugs and indications are already in the export (ADR 0034). Trial size was not stored: the `trial` table held the registry number, study name, and phase only. The read API did not show trials either (ADR 0025, decision 3), and the export showed exactly what the read API shows (ADR 0034, decision 7).

The spec lists enrollment among the ClinicalTrials.gov data we use. Its provenance rules say every published value has a claim with a verbatim quote, checked against the stored copy (rule 4), and that the API and MCP tools return a source with every value (rule 8). A trial's phase and size come from structured registry fields. No quote states them and the publish gate does not check them.

## Decision

1. **Size is stored on the trial, as the registry gives it.** `trial.enrollment` is the registry's `enrollmentInfo.count`. `trial.enrollment_type` says whether that count is `actual` (final) or `estimated` (planned). Both may be empty. A type the registry does not name as actual or estimated is left empty, and so is the type when there is no count. The model and the database refuse a negative count.
2. **Phase and size are registry reference data, not published values.** They are kept for matching and for the owner's private page, the same way the phase always has been. Because no quote states them, they are not published through the read API or the MCP tools, and the spec's provenance rules apply unchanged. Each registry record the collector reads updates the trial's study name, phase, and size (`catalyst.references.record_trial`), but only after that record is stored. So the newest stored registry record, `ctgov:<NCT number>:<last update>`, with its snapshot and hash, is always the one the values were last read from. A record that fails to store changes nothing. A re-run writes no second row, a changed count replaces the old one, and a change to a stored trial's phase or size is logged as `trial_registry_changed`.
3. **The read API and MCP tools show each catalyst's trials, free and paid: the link only.** `trials` sits beside `drugs` in the summary shape. Each trial gives its id, registry number, and study name, and nothing else. A trial is shown only when one of the catalyst's change rows cites its link's claim, so the quote that names the trial is a source. That source lists `trials` in its `fields`, as drug links list `drugs`. Any other link is left out and logged as `trial_link_withheld`, and the rest of the catalyst is still shown. Trials are ordered by registry number. A trial name says what the event is, like the drug and the indication, so it is in the free summary (spec: Offering).
4. **The private export adds the registry values, labelled.** In `catalyst export-catalysts`, each trial, in published catalysts and held items alike, gains a `registry` object:
   - `note`: "Registry data from ClinicalTrials.gov, not a quoted claim: shown as the registry reports it, not checked by the publish gate.";
   - `phase`, `enrollment`, and `enrollment_type`;
   - `record`: the link, posting date, and retrieval date of the newest stored registry version, or empty when none is stored.
   Everything else in each catalyst is exactly what the read API shows. The export is private (ADR 0034) and must not be made public.
5. **Held claims list only exact matches.** In the export's `held` list, each item gains `trials`: the trials its names resolved to exactly one of (`entity_match`, status resolved). Ambiguous and unknown names add nothing. The list is empty when nothing resolved.
6. **Filling the sizes.** After this merges, running "Collect (live)" with `sources` set to `ctgov` reads every covered company's registry records again and fills the sizes. No model call is made.

## Alternatives

- **Phase and size in the read API, with the registry record as their source.** The registry record has no quote, so this would publish values the spec's provenance rules do not allow (Opus review of PR #80, B1).
- **A claim and quote for each registry field.** A field value is not a sentence, and the publish gate's checks are built for quoted text. This is worth doing if phase or size ever need to be public, for example for the phase filter.
- **Trials in the export only.** This would leave the API without the quoted trial link, which is a published value like the drugs.
- **A source-document column on the trial row.** Updating the row only after its record is stored gives the same guarantee without one.

## Consequences

- Each export trial differs from the read API by its `registry` object, and only by that (ADR 0034, decision 7, amended).
- Trial rows already stored show no size until the next registry run.
- The phase filter (ADR 0025, deferred) still needs phase to be published with a source first.
