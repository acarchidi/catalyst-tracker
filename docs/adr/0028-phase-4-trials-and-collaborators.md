# 0028 — Phase 4 trials, independent trials, and collaborators with their exposure

- **Status:** accepted (planned; nothing is built now)
- **Date:** 2026-10-18
- **Roadmap items:** Phase 2, "Phase 4 and independent trials" and "Collaborators on every trial and catalyst"; Phase 4, "Collaborator exposure estimates"

## Context

The owner asked for three things:
1. Coverage that explicitly includes ongoing Phase 4 trials (post-marketing studies), whether the company runs them or someone else does.
2. Trials run with private collaborators accounted for, and the collaborators listed.
3. A rough estimate of each collaborator's price exposure to the catalyst.

**What the sources give:**
- ClinicalTrials.gov records each study's lead sponsor and its collaborators, and marks Phase 4 studies.
- Studies of a company's approved drug that are run by universities, hospitals, or cooperative groups name the drug as an intervention, but not the company as sponsor.
- Companies disclose partners, licensees, and co-development deals in press releases and SEC filings (8-K, 10-K, 10-Q).
- The FDA publishes the post-marketing requirements and commitments it sets for approved products. Its database's access is checked at build time.

## Decision

1. **Phase 4 trials are covered explicitly.**
   - The registry collector keeps Phase 4 studies of each covered company's drugs as well as its own.
   - Independent studies are found by drug name: generic, brand, and code names from the reference tables, matched exactly (ADR 0015). Each one is marked "independent" with its real sponsor.
   - Their readouts and enrollment milestones are catalysts like any others.
   - Where the FDA's post-marketing requirement and commitment data allows, a study linked to a requirement is labelled so, with its due date.
2. **Collaborators are listed on every trial and catalyst:**
   - every collaborator from the registry, from deals disclosed in filings, and from releases;
   - each one's role, for example co-sponsor, licensee, co-developer, contract research organization, or academic site, as the source states it;
   - whether it is public or private;
   - for public ones, its ticker and SEC number when the entity resolver matches it;
   - the source quote for each.

   Unclear roles stay as written. The resolver gains a collaborator entity type, and it follows its existing exact-match rules.
3. **Exposure estimates come later, with the catalyst scenarios** (Phase 4 of the roadmap; ADR 0017). Each is labelled an estimate, shows every input, and carries the not-financial-advice notice.
   - **Public collaborator:**
     - its economic share of the program, from disclosed terms: profit split, royalty rate, milestones;
     - times the scenario's change in program value;
     - as a share of the collaborator's own market value.

     This gives the same kind of upside and downside range as a scenario.
   - **Private collaborator:** the estimated dollar value at stake, from disclosed terms. Where a valuation is publicly reported (for example a funding round), the same value is also shown as a share of it, with its source. There is no share price, so there is no price-move range.
   - **Undisclosed terms:** the estimate is marked "terms not disclosed". It is never guessed.

## Alternatives

- **Company-sponsored Phase 4 trials only.** This is simpler, but independent studies of approved drugs can move a drug's prospects too. The owner asked for both.
- **A price-exposure guess for private collaborators.** There is no share price to anchor it, so dollar exposure and its share of a disclosed valuation are as far as the evidence goes.

## Consequences

- More trials per company. Independent Phase 4 studies can be numerous for widely used drugs, so they are filtered to those with dated milestones.
- The collaborator list depends on name matching. Ambiguous names go to review, as today.
- Exposure estimates need the scenario model, market-size estimates, and the price-data licence, in that order.
