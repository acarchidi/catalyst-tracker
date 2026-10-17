# 0018 — Verification: stored checks per claim, built in five parts

- **Status:** accepted
- **Date:** 2026-10-17
- **Roadmap item:** Phase 1, "Verification checks and publish gate" (leads to CP4)

## Context

The spec's verification section lists five kinds of check:
- quote match;
- a second extraction;
- rule checks;
- cross-source match;
- change sanity.

It also states a publish rule. Under that rule, a claim publishes automatically only when the quote match, the second extraction, and the rule checks pass, and either a second source agrees or the source is a regulator or SEC filing. Everything else goes to the review queue with its reason.

The owner asked for two more things on 2026-10-17:
- accuracy of 99% to start;
- proof that automated checking finds and corrects errors: a planted-error test with a one-page result.

Gate 2 now requires under 1% errors at 95% confidence (PR #24).

## Decision

1. **Every check's latest outcome is stored per claim.** The table is `claim_check`: one row per claim and check (`check_name`, since `check` is a reserved word in SQL), with an outcome of pass, fail, or not applicable, and a plain-words reason. The database refuses a failure without a reason. The publish gate, the review queue, and the planted-error test all read this one table.
2. **Built in five parts, each its own pull request:**
   1. **Rule checks in code** (this part, `catalyst.verify.rules`):
      - the extractor's own checks are clean, and the claim carries the list of them (a missing list fails);
      - the date words appear in the quote, checked again here rather than taken from the extractor;
      - the stored window is exactly what the date words give as of the release's New York date, and had not ended by then (checked again here too, so a changed or unchecked value cannot pass);
      - the claim has a window;
      - the window has not passed;
      - every name the resolver saw matched exactly one record, and the document has a company;
      - a phase written in the quote ("Phase 3", "Phase III", "Phase 2b", "Phase 1/2", "Phase 2–3") overlaps the matched trial's ClinicalTrials.gov phase;
      - an FDA decision (PDUFA) date, or the month or quarter a release gives for it, can fall after the release and no more than 15 months after it. That is the longest standard review, 12 months from submission, plus the 3-month extension for a major amendment. Without the submission date, this is a necessary condition, not a sufficient one.

      The job runs after each extraction and after matching, and also as `catalyst verify-claims`. "Today" and the release date are New York dates, as in the extractor.

      Left to later parts (Opus review of PR #29):
      - The quote is matched against the stored copy of the source at extraction time. The publish gate (part 5) matches it again against the stored copy before publishing (spec: Provenance), because that needs the snapshot store.
      - A claim with no drug name passes `names_matched`. The second extraction (part 2) compares the drug field, so a missing drug there is a disagreement.
      - Any other phase written in the quote ("after Phase 2 data, Phase 3 AZURE-1") can fail a good claim. The error sends the claim to review, never to publication; part 3 revisits it with the planted-error results.
      - With several matched trials, a stated phase needs to match only one of them.
   2. **Second extraction** (two pull requests: the prompt and the comparison first, then the job):
      - a different prompt, `verify_catalysts`, reads the same release on the verifier role. It never sees the extractor's answer, so it cannot simply agree with it. It answers in the extractor's format, and its claims are stored under the agent name `verifier`. They are never published; they only vouch for the extractor's claims;
      - plain code (`catalyst.verify.second`) pairs each extracted catalyst with one of the verifier's, the way the gold-set scorer pairs proposals with gold events: the quotes overlap, or both give the same type and window. Each verifier catalyst vouches for one extracted catalyst at most;
      - the check `second_reading_agrees` passes only when the type, the window, and the drug all agree. Drug names agree when they match apart from case, spaces, and punctuation, or when the reference tables call them the same drug. Anything else fails with both values, and an event the verifier did not find fails too;
      - trials are left to part 3, which checks them against the registry. The extractor names no trial field, so the two readings have nothing to compare;
      - the verifier prompt gets its own scores on the same gold set (`catalyst eval-extractor --prompt verify_catalysts`), stored in `evals/verify_catalysts/`. The second reading runs live only once those scores match the current prompt and model. Until then, claims have no `second_reading_agrees` row, and the publish gate counts that as not passing;
      - both readings use the same model family for now. The spec counts that as weak evidence, so a second provider stays the plan.
   3. **Cross-source and change checks:**
      - the same fact from a second document (8-K versus release versus registry);
      - an advisory meeting needs a Federal Register notice;
      - a date that moves more than 6 months, or an outcome that reverses, always goes to review;
      - date ranges ("November 16–19", "late 2027 or early 2028") are read as windows.
   4. **Planted-error test (owner proof):**
      - known-correct claims get realistic errors inserted: wrong date or quarter, wrong drug, wrong event type, wrong company, wrong trial, wrong phase, a quote that does not support the value, a past date;
      - every check runs on them, mixed with clean claims;
      - a one-page result for the owner reports, per error type: caught, corrected to the right value with its quote, clean records wrongly flagged, and missed;
      - CI blocks a drop.
   5. **Publish gate:**
      - claims that pass the publish rule become catalysts, with drug and trial links;
      - each link's claim must be about that catalyst and field (from the PR #23 review);
      - everything else gets a review status with its reasons;
      - a claim with a missing check row (for example, after a run where matching failed) counts as not passing;
      - `window_not_past` applies only before first publication; once a catalyst is live, its date passing is a change for the outcome tracker, not a review item;
      - prompt-injection fixtures prove that text in a source cannot change a stored value.

      CP4 then runs (Opus review).
3. **A failing check never deletes or changes a claim.** It only blocks publication and gives the reason. Corrections come from evidence: a second extraction or another source that passes every check.

## Alternatives

- **Checks computed on the fly at publish time, not stored.** The review queue and the planted-error report would each recompute them, and nobody could see why a claim was held last week.
- **One status column on the claim.** Claims are frozen, by design, so a mutable status would break that. It would also hide which check failed.
- **A model that judges each claim.** It is not independent of the extractor, and it cannot be tested like a rule. Models enter only through the second extraction, which is scored on the gold set.

## Consequences

- The publish gate becomes a short rule over stored outcomes, and its decisions can be explained row by row.
- New checks add rows, not columns. A check that is retired leaves old rows behind until the claim is next checked. The gate reads only the current list of checks.
- The PDUFA check is weak until submission dates are collected (from company releases or FDA filing-acceptance letters). The cross-source part can tighten it.
