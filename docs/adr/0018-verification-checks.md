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
      - plain code (`catalyst.verify.second`) pairs each extracted catalyst with one of the verifier's. Two readings can be the same event when their quotes overlap, or, in different sentences, when they give the same type, the same complete window, and the same named drug. The closest pairs are taken first, so the order of either list never changes an outcome. Each verifier catalyst vouches for one extracted catalyst at most;
      - the check `second_reading_agrees` passes only when the type, the window, and the drug all agree:
        - drug names agree when they match apart from case, spaces, and punctuation (every letter counts, in any script), or when the reference tables call them the same drug;
        - a missing window never agrees;
        - a missing drug never agrees, except that a deal (`merger_acquisition`) need not name one;
        - anything else fails with both values, and an event the verifier did not find fails too;
      - trials are left to part 3, which checks them against the registry. The extractor names no trial field, so the two readings have nothing to compare;
      - the verifier prompt gets its own scores on the same gold set (`catalyst eval-extractor --prompt verify_catalysts`), stored in `evals/verify_catalysts/`. The second reading runs live only once those scores match the current prompt and model. The extractor's first-version exemption (`unscored.json`) does not apply to it (review of PR #31). Until then, claims have no `second_reading_agrees` row, and the publish gate counts that as not passing. Before any scores or floor are committed there, `evals/verify_catalysts/baseline.json` and `unscored.json` join `OWNER_PATHS`, in a pull request the owner merges;
      - both readings use the same model family for now. The spec counts that as weak evidence, so a second provider stays the plan;
      - **the job** (`catalyst.verify.second_run`, part 2b) runs inside `catalyst extract-catalysts`, last, after matching and the rule checks, so a problem in it never holds them up (review of PR #34):
        - it reads each release that has extracted claims once per verifier prompt version and model;
        - the table `document_reading` keeps each reading, even one that found nothing, so a re-run spends nothing;
        - a verifier claim counts only if its prompt version and model match the current reading; ModelClient refuses an answer from any other model, so an older model's reading never vouches, and outcomes no current reading supports are removed on every run, also while the gate is shut;
        - only verifier proposals whose own checks found no problem can vouch;
        - a failed model call leaves the release unread for the next run, with no check row;
        - a spending cap stops the job and says so;
      - two trials of one drug, with the same type and window in different sentences, can still pair, because trials are not compared here. Part 3's registry checks cover that case (Claude review of PR #31).
   3. **Cross-source and change checks:**
      - the same fact from a second document (8-K versus release versus registry): the check `independent_source` (part 3b, `catalyst.verify.independent`) passes a claim whose own source is a regulator or SEC filing, as the publish rule allows. Any other claim passes only when another document agrees. That document must:
        - be another source: another address (ignoring case, "www.", the scheme, a query, a fragment, and a trailing slash), another file, and other words. A copy of one release posted twice shares its mistakes, so it is one source (Opus review of PR #46);
        - be linked to the same company;
        - hold a claim from that document's latest reading that the extractor flagged nothing on and that passed every rule check itself;
        - give the same type, a window inside the claim's window, and the same drug (by name, or by the reference tables), with a deal allowed to name no drug. A vaguer window ("2027" for a claimed "Q1 2027") confirms less than the claim says, so it does not vouch.

        A missing window or drug never agrees. Wire releases are not linked to a company yet, so a wire-only claim fails this check and goes to review until the wire collector links releases to companies;
      - an advisory meeting needs a Federal Register notice; plain code reads each stored notice (`catalyst.verify.fedreg_notice`): committee, meeting days, and each product with its application number and sponsor, every value with its quoted sentence, empty where the notice does not say; an unreadable or undated notice, or one that a later amendment for the same committee and topic replaces (the `superseded` list), is listed apart, never counted as a match; a meeting sentence that names any other date, or a change of date, gives no meeting days;
      - a date that moves more than 6 months, or an outcome that reverses, always goes to review;
      - date ranges ("November 16–19", "late 2027 or early 2028") are read as windows (part 3a, `catalyst.extract.dates`): the window runs from the first part's start to the last part's end; its precision is the coarsest of the parts, and a range of days counts as month precision, so "day" always means one day; a year written only at the end, in digits or in words ("first quarter to second quarter of 2028", "November 16 to December 2, 2027", "first half or second half next year"), applies to the first part too, never the document's year; a backwards range or a part with no window gives none. Ranges the extractor already proposed (for example "year-end 2026/first quarter 2027") now get windows instead of failing `has_window`.
   4. **Planted-error test (owner proof):**
      - known-correct claims get realistic errors inserted: wrong date or quarter, wrong drug, wrong event type, wrong company, wrong trial, wrong phase, a quote that does not support the value, a past date;
      - every check runs on them, mixed with clean claims;
      - a one-page result for the owner reports, per error type: caught, corrected to the right value with its quote, clean records wrongly flagged, and missed;
      - CI blocks a drop.

      How it is built (`catalyst.verify.planted` and `planted_report`, `catalyst planted-errors`):
      - plain code, no model, no network, seed 2026, so every run gives the same page;
      - the clean claims are the gold set's events, stored as the extractor stores them (its own code finds the quote and the window). A gold event whose claim the extractor already flags (Mirum's "Q3", which shares its year with "Q4 2026") is left out and named on the page;
      - names are matched by the matching job's own rules against reference tables built from the gold set and a recorded copy of ClinicalTrials.gov (`evals/planted_errors/registry.json`: the five sponsors' trials, lead sponsor only, recorded 2026-10-18). A code-shaped drug name (SPY072, ABP-450) is a code name; any other is a generic name;
      - each error is planted in the stored claim, after the extractor's checks, so the checks after it must catch it on their own. One error per copy; each copy is checked among the clean claims of its own release, as of the day the release came out, so the result does not change as the dates pass;
      - the error types: a later quarter, half, month, or year than the date words say (a day moved 1 to 30 days); the same period a year or more earlier, so it had passed; an FDA decision date moved beyond 15 months; another drug from the gold set; another event type; another company of the gold set; another of the company's registered trials; a trial whose registry phase contradicts the phase in the quote (only when the quote states one); the quote swapped for another sentence of the release;
      - the second reading is simulated as a reader that gets every event right. A copy counts as **corrected** when the second reading disagrees and the reading it is paired with gives the right value, with its own quote. Because the stand-in never misreads, the page also counts what the rule checks catch without it;
      - an error is planted only in a claim that passes every check when correct; a claim flagged when correct (SKYLINE Part B, whose drug is empty) counts as wrongly flagged, and so does a correct claim that a planted neighbour makes fail;
      - checks are one list (`CHECKS`); a new check is one more line there. The independent-source check (PR #46) joins it when it merges;
      - `evals/planted_errors/` holds the page (`report.md`), the counts (`scores.json`), and the floor (`baseline.json`): each error type's catch rate, with and without the second reading, and the most clean claims that may be wrongly flagged. A test recomputes the counts and fails on any drop, and fails when the committed page or counts are out of date. The floor joins `OWNER_PATHS` in a pull request the owner merges;
      - the claim-check table is not used here: the test calls the same check functions that fill it, on claims that never enter a database.
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
