# 0037 — A generic or brand name of another company's drug waits for review

- **Status:** accepted
- **Date:** 2026-10-19
- **Roadmap item:** Phase 1, "Verification checks and publish gate" (CP4 review, finding O1)
- **Amends:** ADR 0015, decision 4

## Context

ADR 0015 (decision 4) lets a generic (INN) or brand name match a drug for any company, "because licensed drugs change hands". The matching job (`catalyst.resolve_claims.known_drug`) then gave a claim the drug that name matched, whichever company develops it. Only code names are tied to their company.

The CP4 review (optional finding O1) noted the consequence. A release often names a partner's or a competitor's drug: a combination partner, a comparator, or a licensed-in product. Its claim would be linked to that other company's drug and published under the release's company. `names_matched` would pass, because the name matched exactly one record.

Today the tables hold only code names (ADR 0015, part 4), so the rule changes nothing yet. The drug list from Drugs@FDA and ClinicalTrials.gov (ADR 0035, PR #73) will add brand and generic names for many companies, which makes the case common.

## Decision

1. In the matching job, a generic or brand name whose one drug another company develops is **ambiguous**, with that drug as its candidate. `names_matched` therefore holds the claim for review, with the reason "drug ambiguous".
2. A drug with no known company still matches, as before.
3. A name of two drugs stays ambiguous, as before, even when one of them is the claim's company's own.
4. A code name is unchanged: another company's code name was already ambiguous (ADR 0015, part 4).
5. Nothing else changes. The resolver's lookup (`ReferenceData.resolve_drug`), used by the second reading and the advisory check to tell whether two names are one drug, still matches across companies. The rule sits in `known_drug`, which only the matching job and the planted-error report use. Neither the gate's list of checks nor `MAY_NOT_APPLY` changes.

## Alternatives

- **Keep ADR 0015 decision 4 as it was.** That publishes another company's drug under the release's company whenever a release names it, which is wrong in the common case.
- **Match only the company's own drugs, and call the rest unknown.** "Unknown" tells the reviewer less than "ambiguous, with the other company's drug as the candidate".
- **Accept another company's drug when a licence is on file.** No reference table records licences yet. When one does, a licensed drug can match for its licensee.

## Consequences

- A correct claim about a licensed-in drug that the reference tables file under its originator (for example Ionis's eplontersen, which AstraZeneca markets) waits for a person. That is the safe direction. ADR 0035's offline measurement shows 5 such holds among 315 claims, and no claim that matched before is held by this rule.
- On the claims of the 18 October live run, the rule changes no outcome today, because no generic or brand name is on file yet.
