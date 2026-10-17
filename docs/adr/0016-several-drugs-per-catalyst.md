# 0016 — Several drugs per catalyst, each tested alone or in combination

- **Status:** accepted
- **Date:** 2026-10-17
- **Roadmap item:** Phase 1, "Entity resolver and reference tables" (part 2 of 4; ADR 0015)

## Context

The schema gave each catalyst one optional `drug_id`. Platform and combination trials break that:
- Spyre's SKYLINE Part B tests SPY001, SPY002 and SPY003 alone and in pairwise combinations, "six investigational agents in total".
- Its table names the combinations SPY120, SPY130 and SPY230 without saying which pair each is.

On 2026-10-17 the owner ruled (`evals/extract_catalysts/README.md`, rule 3) that such a record:
- names every drug and combination;
- marks each as tested alone or in combination;
- is found by a search for any of them;
- never leaves the drug empty.

The spec's provenance rule also applies: no published value without a claim and a verbatim quote. No catalysts are stored yet; extraction writes claims only (ADR 0013).

## Decision

1. **`catalyst_drug`** links a catalyst to each drug it concerns.
   - Each link has a role, `alone` or `in_combination`.
   - Unique on catalyst, drug, and role, so one drug can be linked both alone and in combination.
   - A combination product itself, such as SPY120, is linked as `in_combination`.
2. **`drug_component`** records one part of a combination drug. It is used only where a source states that part. Parts are never inferred from a code; for example, "SPY120" is not split into SPY001 and SPY002. The database refuses a combination listed as its own part.
3. **Every link names a stored claim** (`claim_id`, required, a foreign key). A drug link therefore cannot exist without a stored quote. The database cannot check that the claim is the right one. The publish gate (verification item) checks that a `catalyst_drug` link's claim is about that catalyst with field `drugs`, and that a `drug_component` row's claim is about that drug.
4. **`catalyst.drug_id` is dropped.** Change history records the drug list under the field `drugs`.
5. **The migration refuses to run when it would lose data:**
   - upgrade, while any catalyst still names a drug or change history has `drug_id` rows;
   - downgrade, while any drug link, combination part, or `drugs` history row exists.
   Row-level security is on for both new tables (ADR 0011).

## Alternatives

- **Keep `drug_id` and add a list beside it.** Two places would hold the same fact, and they would disagree.
- **A JSON list of drug names on the catalyst.** It is not searchable by drug and cannot point each name at its own quote.
- **One combination row per trial arm** ("SPY001 + SPY002"). It invents a structure the sources often do not state, and the owner asked for the company's own wording.

## Consequences

- A search for one drug can find every catalyst that tests it, alone or in combination, through one table.
- Showing a catalyst now takes a join to `catalyst_drug`. The site's search and filters item builds the display the owner described: the company's wording first, and an expanding list that never takes over the page.
- Searching by drug needs indexes on `catalyst_drug(drug_id)` and `drug_component(component_id)`. The search item adds them, with plain-index support in the table generator.
- `catalyst_trial` has no `claim_id` yet. The verification item should give it one in the same way.
