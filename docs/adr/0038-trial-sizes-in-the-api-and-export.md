# 0038 — Trial sizes from the registry, and trials on each catalyst in the API and export

- **Status:** accepted
- **Date:** 2026-10-20
- **Roadmap item:** Phase 1, "Trials and their sizes on each catalyst" (owner request, 2026-10-20)

## Context

The owner's private catalyst page should list, for each company's entries, the drugs, the indication sought, and the size of each trial where known. Drugs and indications are already in the export (ADR 0034). Trial size was not stored: the `trial` table held the registry number, study name, and phase only. The read API did not show trials either (ADR 0025, decision 3), and the export shows what the read API shows (ADR 0034, decision 7).

The spec lists enrollment among the ClinicalTrials.gov data we use, but leaves open how it is stored, where it appears, who sees it, and how its source is shown.

## Decision

1. **Size is stored on the trial, as the registry gives it.** `trial.enrollment` is the registry's `enrollmentInfo.count`, and `trial.enrollment_type` says whether that count is `actual` (final) or `estimated` (planned). Both may be empty. A type the registry does not name as actual or estimated is left empty, and so is the type when there is no count. A negative count is refused by the model and the database.
2. **Filled like the phase.** Each registry record the collector reads updates the trial's study name, phase, and size (`catalyst.references.record_trial`). A re-run writes no second row, and a changed count replaces the old one. The trial row keeps no source column of its own, as before: its registry values come from the trial's stored registry record, `ctgov:<NCT number>:<last update>`, which keeps its snapshot and hash like every document.
3. **Trials appear on every catalyst, free and paid.** `trials` sits beside `drugs` in the summary shape, so free callers see it too. A trial says what the event is, the same kind of fact as the drug and indication, so it belongs with the summary rather than with paid history or analysis (spec: Offering). Each trial gives its id, registry number, study name, phase, size, size type, and `registry_record`: the link, posting date, and retrieval date of the newest stored registry version, which is the one the phase and size were last read from. `registry_record` is empty when no record is stored.
4. **A trial is shown only with the quote that names it.** Each trial link names its claim (ADR 0018, part 5b). A trial is shown only when one of the catalyst's change rows cites that claim; that claim's source then lists `trials` in its `fields`, as drug links list `drugs`. Any other link is left out and logged as `trial_link_withheld`; the rest of the catalyst is still shown. Trials are ordered by registry number.
5. **Held claims list only exact matches.** In the export's `held` list, each item gains `trials`: the trials its names resolved to exactly one of (`entity_match`, status resolved). Ambiguous and unknown names add nothing. The list is empty when nothing resolved.
6. **Filling the sizes.** After this merges, running "Collect (live)" with `sources` set to `ctgov` reads every covered company's registry records again and fills the sizes. No model call is made.

## Alternatives

- **Trials in the export only.** This would make the export and the read API disagree about what a catalyst shows, which ADR 0034 rules out.
- **Trials as a paid field.** A trial name and size describe the event; holding them back would make the free calendar less checkable, which is what the free tier is for.
- **A source-document column on the trial row.** It would pin each size to one stored version, but the phase has never had one, and the newest stored registry version already names where the values come from. This can be added if registry values ever need their own change history.
- **Withholding the whole catalyst when one trial link is unbacked.** Drug links do this because `drugs` is a value with its own change rows. Trials have no such value, so leaving out the one link is enough to keep an unpublished claim from showing.

## Consequences

- The read API, the MCP tools, and the export all show trials with no further change. ADR 0025's decision 3 and ADR 0034's decision 7 no longer hold.
- Existing trial rows show no size until the next registry run.
- The phase filter (ADR 0025, deferred) can now be built on shown trials.
