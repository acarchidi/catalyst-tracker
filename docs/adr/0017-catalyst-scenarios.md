# 0017 — Catalyst scenarios: conditional price-move ranges with every input shown

- **Status:** accepted (design); default values are set when the item is built
- **Date:** 2026-10-17
- **Roadmap item:** Phase 4, "Catalyst scenarios" (owner request, 2026-10-17)

## Context

The owner wants each upcoming catalyst to carry "what if" outcomes. For example: a Phase 2 drug shows 15% symptom reduction against a marketed drug's 10%, with no added side effects and more patients eligible. What would a Phase 3 that confirms it be worth? The guesses need not be precise, but anyone must be able to see what went into them.

The spec banned price targets, and said "no predictions in v1". Any predictive feature must publish its accuracy against a naive baseline.

## Decision

1. **Owner decisions (2026-10-17):**
   - Each scenario shows estimated peak sales, risk-adjusted value as a share of market value, and a price-move range, never a single target.
   - The "no price targets" hard rule excepts only this feature.
   - There is no securities lawyer review.
   - Every page, alert, API response, and MCP tool description that carries scenarios shows "Estimate. Not financial advice."
   - Personalized advice and buy/sell ratings stay banned.
2. **Defaults the owner did not override:**
   - An AI analyst drafts the inputs, and the owner approves them before they publish.
   - The model is checked against past price reactions.
   - The feature is built in Phase 4, after readout benchmarking and market-size estimates, and after the price-data license.
3. **Model** (spec: Catalyst scenarios):
   - Each program is valued as patients × share × net price over launch, uptake, and exclusivity, times operating margin, less remaining development cost, adjusted for partner terms, probability-weighted, and discounted.
   - The company's value in a scenario is: program value + other programs + cash − debt.
   - The price-move range is (company value in the scenario ÷ market value) − 1. Its ends come from the low and high market-size cases.
   - "Before" is the market's own price, not our model's.
4. **Inputs this ADR's build must pin down, with a named source for each:**
   - discount rate;
   - approval base rates by phase and disease area (for example the BIO/Informa success-rate report);
   - uptake curve;
   - operating margin or sales multiple;
   - loss-of-exclusivity dates (Orange Book, Purple Book, 10-K);
   - net-price analogs when no comparable drug exists.
5. **Accuracy record:**
   - Each actual result is mapped to its scenario.
   - Each actual move is measured as abnormal return against XBI on day 0 and day +1.
   - Ranges are scored with an interval score, which penalizes width, against a naive baseline range of equal coverage.
   - A backtest on past catalysts publishes at launch, and the record is free to view.
6. **Checkpoint CAL3** (quality-gates):
   - 20 scenario drafts plus the backtest;
   - an Opus review of the math;
   - the owner approves at least 18 of 20;
   - the backtest beats the naive baseline.

## Alternatives

- **Value at stake without a price-move range.** It fits the old rule, but the owner chose the range.
- **Past price reactions only.** Lowest risk, but it does not use the drug's own efficacy, safety, or competitive position, which is the point of the request.
- **A single expected move.** A point estimate reads as a price target and hides how uncertain the guess is.
- **Scoring by hit rate.** A range of "+5% to +100%" is almost always right, so a hit rate rewards uselessly wide ranges.

## Consequences

- The analysis layer now makes a forecast. The accuracy record and CAL3 are what keep it honest.
- The feature needs the price license and the written confirmation from Tiingo before anything displays.
- Cross-trial comparisons that readout benchmarking labels "not comparable" are shown as such, never used silently. Scenarios built on such comparisons say so.
- A disclaimer supports, but does not decide, the publisher's exclusion. Scenarios publish on a regular schedule and stay impersonal.
