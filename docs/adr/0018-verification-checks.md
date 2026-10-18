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
      - an advisory meeting needs a Federal Register notice; plain code reads each stored notice (`catalyst.verify.fedreg_notice`): committee, meeting days, and each product with its application number and sponsor, every value with its quoted sentence, empty where the notice does not say; an unreadable or undated notice, or one that a later amendment for the same committee and topic replaces (the `superseded` list), is listed apart, never counted as a match; a meeting sentence that names any other date, or a change of date, gives no meeting days. The check `advisory_meeting_noticed` (part 3c, `catalyst.verify.advisory`) passes a claimed advisory committee only when a notice that no amendment replaced has its meeting inside the claim's window and names the same drug: the same name apart from case, spaces, and punctuation; a longer name that adds only anion-salt, dosage-form, and strength words (not metal salts: sodium and calcium oxybate are two products) ("atropine" and "Atropine Sulfate Ophthalmic Solution, 0.01%"); or one drug in the reference tables. A longer name that adds anything else is another product ("insulin" and "Insulin Efsitora Alfa", "adalimumab" and "adalimumab-aaty"). It fails when a notice published since the match (every stored notice is read, whatever its title or date, including a fresh "Notice of Meeting" that re-announces the meeting for another day) names the drug, or is for the same committee and names no product, and does not put the meeting in the window; the collector keeps cancellations and postponements for this (Claude review of PR #49); when any stored notice could not be read; when the notice's sponsor matches another company in the reference tables; and, without a match, for an undated notice, a replaced notice, or no notice, each with its own reason, so the claim goes to review. Stored notices are read once per run (Opus review of PR #49). Other catalyst types are not applicable. A meeting the company announces before FDA's notice appears waits in review until the notice is stored;
      - a date that moves more than 6 months, or an outcome that reverses, always goes to review;
      - date ranges ("November 16–19", "late 2027 or early 2028") are read as windows (part 3a, `catalyst.extract.dates`): the window runs from the first part's start to the last part's end; its precision is the coarsest of the parts, and a range of days counts as month precision, so "day" always means one day; a year written only at the end, in digits or in words ("first quarter to second quarter of 2028", "November 16 to December 2, 2027", "first half or second half next year"), applies to the first part too, never the document's year, and a year written only at the start ("Q3 2026 to year-end") applies to the last part; "Q4 2026 to first quarter" then runs backwards and gives no window. Unspaced hyphens between whole parts ("Q4 2026-Q1 2027") and "and" without "between" also give no window, so such claims fail `has_window` and go to review; a backwards range or a part with no window gives none. Ranges the extractor already proposed (for example "year-end 2026/first quarter 2027") now get windows instead of failing `has_window`.
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
      - the error types (twelve, after the Opus review of PR #51): the window moved 1 or 2 periods or a year later with its date words left as they were (a day 1, 7, or 30 days); another date phrase of the same quote, stored with the window it gives, as the extractor would; the same period a year or more earlier, so it had passed; an FDA decision date moved beyond 15 months; another drug of the same release; another company's drug; another event type; another company of the gold set; another of the company's registered trials; a trial whose registry phase contradicts the phase in the quote (only when the quote states one); the quote swapped for an unrelated sentence of the release; the quote swapped for a sentence of another gold event of the release that holds the same date words and does not name the claim's drug. Where an error has several forms (every other trial, company, drug, type, or date phrase), each is its own copy, so no single random pick decides a row; the seed picks only the unrelated sentence, and the seed used is written to the counts;
      - the second reading is simulated as a reader that gets every event right. A copy counts as **corrected** when the second reading disagrees and the reading it is paired with gives the right value, with its own quote. Because the stand-in never misreads, the page also counts what the rule checks catch without it;
      - an error is planted only in a claim that passes every check when correct; a claim flagged when correct (SKYLINE Part B, whose drug is empty) counts as wrongly flagged, and so does a correct claim that a planted neighbour makes fail;
      - checks are one list (`CHECKS`); a new check is one more line there. It holds the rule checks, the second reading, and the independent-source check, which sees each claim beside every other release's clean claims, with each gold release's real tier, address, and file hash; a planted copy shares its original's release, so it never vouches for itself or for it. Every gold release is an SEC filing, which the publish rule lets stand alone, so this check passes every claim here. The advisory-meeting check runs with no Federal Register notice stored for the gold releases, so any claim typed as an advisory meeting fails it;
      - `evals/planted_errors/` holds the page (`report.md`), the counts (`scores.json`), and the floor (`baseline.json`): each error type's catch rate, with and without the second reading, its corrected rate, the fewest copies it may plant (so a mutation that stops producing copies fails), and the most clean claims that may be wrongly flagged. A test recomputes the counts and fails on any drop, and fails when the committed page or counts are out of date. The floor joins `OWNER_PATHS` in a pull request the owner merges;
      - the claim-check table is not used here: the test calls the same check functions that fill it, on claims that never enter a database.
   5. **Publish gate** (three pull requests: the decision first, then the catalyst writer, then the adversarial tests):
      - **the decision** (part 5a, `catalyst.publish.gate`) is plain code over stored rows. A claim may publish only when both hold:
        - every required check has a stored row that passed. The required list is a constant: the rule checks, read from the rules themselves so their names live in one place (two rules naming one check stop the gate from loading), plus `second_reading_agrees`, `independent_source`, and `advisory_meeting_noticed`;
        - only four checks may answer "not applicable" instead, because each is about something a claim may lack: `window_not_past` (no window), `phase_matches_registry` (no stated phase or matched trial), `pdufa_date_plausible` (not an FDA decision date), and `advisory_meeting_noticed` (not an advisory meeting). "Not applicable" from any other check holds the claim (Opus review of PR #52). A new check that may not apply joins this list by name;
        - the quote is found again, character for character, at its stored place in the stored copy. The copy's text is computed the way the extractor computed it (`document_text`). The quote found somewhere else in the copy is not enough, because the place is part of the claim;
      - anything else waits for review with every reason, in order: the quote first, then each check in the list. A failing check gives its stored reason ("has_window: the date words name no window"). A check with no row gives "check X has not run". Rows of checks no longer on the list are ignored;
      - these send a claim to review, never to publication:
        - a missing copy;
        - a copy that no longer matches its hash;
        - a copy whose key is not the document's recorded hash;
        - a copy that cannot be read;
        - a claim that does not name the text version its offsets use, or names another version than the current one. Such a quote must be found again before it can publish;
        - a claim that is not the extractor's catalyst claim;
        - a claim from any prompt or prompt version other than the current extractor prompt. Its reading was replaced, so it must not publish beside the new one;
      - the decision is kept in `claim_decision`: one row per extractor claim, with publish or review, the list of reasons, and when it was decided. A re-run updates the row in place. The database refuses a review row with no reason, and a publish row with one. Row-level security is on with no policy, so only the server's own code reads or writes it. The rows can always be rebuilt from the stored checks and copies, so the migration's downgrade may drop them;
      - the job `catalyst decide-claims` (free, no model) decides every extractor claim, reads each copy once per run, and reports how many claims will publish, how many wait for review, how many copies could not be read, and the ten most common reasons. It also runs last in `catalyst extract-catalysts`, after every check. If matching, the rule checks, or the advisory check failed in that run, it decides nothing and says so, so no decision rests on outcomes the run did not renew. If it fails itself, the earlier steps stand and the command fails;
      - the decision reads outcomes as they were stored. A decision made long after its checks ran can rely on an old `window_not_past` or `independent_source` outcome. The catalyst writer (5b) therefore re-runs the checks and decides in one pass before it writes anything;
      - **the catalyst writer** (part 5b, `catalyst.publish.write`, also `catalyst publish-claims`) is the only job that writes live catalysts. It is plain code and makes no model calls. It runs last in `catalyst extract-catalysts`, after the decisions, under its own key (`published`); when the decisions did not finish it writes nothing and says so, and when it fails itself the earlier steps stand and the command fails;
      - **it checks again before it writes.** For each claim the decision job marked to publish, it recomputes, as of today in New York:
        - every rule check (`catalyst.verify.rules.RULES`);
        - `independent_source`, since it depends on other documents' claims;
        - `advisory_meeting_noticed`, since a notice may since have been amended or cancelled;
        - the quote match against the stored copy.

        Every other check on the gate's list is read as stored. Today that is only `second_reading_agrees`, which needs a model call; its own job removes outcomes no current reading supports. The list itself is read from `catalyst.publish.gate` (`REQUIRED_CHECKS`, `MAY_NOT_APPLY`), never copied, so a check added there is read as stored until the writer recomputes it. The entity matches and registry phases the rules read are stored inputs, renewed by their own jobs. Then `decide` runs again, and only a fresh publish writes;
      - **`window_not_past` applies only before first publication.** A claim is about a live catalyst when a change row already cites it, or when it states the same event as an open published catalyst (below). For such a claim, both the writer and the decision job treat `window_not_past` as not applicable: the date passing is a change for the outcome tracker, not a review item. Nothing is ever unpublished or deleted;
      - **which catalyst a claim is about.** Two statements are one event when they share the company, the catalyst type, the matched drugs, and the matched trials. Resolved catalysts are left out, so a new event of the same kind (a new FDA date after a refusal) becomes a new catalyst;
      - **what it writes, the way the read API reads it (ADR 0025):**
        - a new event becomes a catalyst with status upcoming, and a first-value change row, citing the claim, for every value it sets. `window_raw_text` is the claim's date words. The indication stays empty until indications are matched;
        - each matched drug gets a `catalyst_drug` link naming the claim, and one `drugs` change row lists the links. The extractor names one drug and no role yet, so the link is `in_combination` when the quote says "combination", and `alone` otherwise;
        - each matched trial gets a `catalyst_trial` link naming the claim. A migration adds the required `claim_id` column; it refuses to run while a trial link exists that would lack or lose its claim. The read API does not show trials yet (ADR 0025, rule 3);
        - so every link's claim is about that catalyst and field: it is the claim whose change rows set the catalyst, and whose match names that drug or trial (PR #23 review);
      - **which claim wins** (spec: Which claim wins). Several new claims about one new event: the best-ranked creates it. A claim about a live event moves its date only when it outranks the claim behind the current window: a higher source tier, or the same tier and published later. A move writes a change row, citing the new claim, for each date field that changes (`window_*`, `date_confidence`), always timed after the catalyst's earlier rows. The same window in other words writes nothing. The title, type, company, and links stay as first written. A claim that does not outrank it writes nothing, and keeps its publish decision for the conflicts display;
      - **these wait for a person instead:**
        - a claim whose window does not overlap the live one's: it may be a slip or another event, and plain code cannot tell;
        - a move that takes either end of the window more than 6 months, unless the new window lies inside the old one (spec: Change sanity);
        - a claim that matches more than one open published catalyst;
        - a claim whose values make no valid catalyst (for example a title over 300 characters).

        The writer stores each held claim's reasons, and those of a claim the fresh checks hold, as its review decision, so the review queue shows the newest reason. The decision job's next run decides it again from stored rows, and the writer holds it again;
      - **safe to re-run.** A claim a change row already cites is skipped, so a re-run with nothing new writes nothing. The job takes a database lock (`pg_advisory_xact_lock`), so two runs never both create one catalyst, and writes everything in one transaction, so a failure leaves nothing half written;
      - **memory.** Both the decision job and the writer read claims grouped by document and hold only the current document's text (Claude review of PR #52);
      - the decision job's publish count is therefore not final; its help text says so (Claude review of PR #52);
      - prompt-injection fixtures prove that text in a source cannot change a stored value (part 5c).

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
