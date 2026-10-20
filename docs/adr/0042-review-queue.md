# 0042 — The review queue: GitHub pages for the owner, approvals through the publish gate

- **Status:** accepted
- **Date:** 2026-10-20
- **Roadmap item:** Phase 1, "Review-queue screen" (no checkpoint of its own; it changes the publish path, so it gets an Opus review, and it feeds Gate 2)

## Context

The spec's review queue is one screen. It shows the highlighted source sentence beside the extracted fields. The reviewer can approve, correct, or reject in one action, with keyboard shortcuts, and each decision is added to the gold set. In the spec's diagram, an approval goes back to the publish gate ("R -- approved --> G"). Only the plain-code gate writes live data (spec: Agent architecture).

The spec leaves open:
- where the screen runs;
- how a decision is stored;
- which holds an approval may lift.

Facts that matter:
- The owner is the only reviewer and does not program.
- The live database is reachable only from GitHub Actions (ADR 0011). The owner has no local copy of the code or the database.
- The repository is private. The web app (`web/`) starts in Phase 2.
- `.github/workflows/` is an owner path (ADR 0036).
- The gate stores each held claim's reasons in plain words (`claim_decision`).

## Decision

1. **Form factor: GitHub, no server.**
   - The queue is a page in a hand-started GitHub Actions job's summary. GitHub shows that page only to people with access to this private repository.
   - For each held claim the page shows:
     - the quote, highlighted in its paragraph of the stored copy;
     - the link to the source;
     - the gate's reasons, in plain words;
     - the proposed values;
     - a short code for those reasons.
   - The owner records a decision by starting a second job by hand, with three inputs: the claim, the action, and that code.
   - The job records the GitHub account that started it as the reviewer. It refuses any account but the repository owner's.
   - This needs no new account or key, and makes nothing public.
   - Keyboard shortcuts wait for the Phase 2 web app.
2. **The record (`review_decision`).**
   - One row per decision: the claim, approve or reject, who, when, and the gate's reasons the person saw, word for word.
   - Rows are only ever added. A claim's latest row counts, so a rejection can withdraw an approval before the writer runs, and nothing is erased.
   - The decision is refused, with the reason, in three cases:
     - the claim is not held;
     - the reasons the person saw are not the claim's stored reasons now (the claim was decided again since the page was made);
     - it does not say who made it.
   - Row-level security is on, with no policy (ADR 0011).
   - The gate's own rows can be rebuilt, but a person's decisions cannot. So the migration's downgrade refuses to run while the table holds any row.
3. **An approval goes through the gate.**
   - Recording it writes no live data.
   - The decision job (`catalyst decide-claims`) and the writer (`catalyst publish-claims`) both apply it with `gate.approve`. That excuses only holds whose reasons the person saw, word for word. A new reason since then still holds the claim.
   - The writer then checks the claim again and writes it as it writes any claim. Every change row cites the claim, and so its stored source and verbatim quote. The writer's run report counts these claims as `published_by_approval`.
   - No approval excuses:
     - the quote match against the stored copy (spec: Provenance);
     - a claim that is not the extractor's catalyst claim;
     - a missing window;
     - a passed window on a new catalyst (`PERSON_MAY_NOT_EXCUSE`).
   - Of the writer's own holds, an approval lifts only two (`PERSON_MAY_APPROVE`): a date move of more than 6 months, and a window that does not overlap the published one. Each needs only a yes or no about the claim's own dates.
   - The writer's other holds need a choice: which of several catalysts, whether the drug is tested alone or in combination, or which of two equal sources. An approval leaves them held, because the writer must never guess. Corrections (decision 5) will handle them.
   - An approval does not override the source ranking (spec: "Which claim wins"). An approved claim that a better source outranks writes nothing.
4. **A rejection never publishes.** The gate excuses nothing for a rejected claim. It stays held with its reasons, and the queue page leaves it out. Nothing is deleted.
5. **Built in parts:**
   - **Part 1 (this):** the record and the approval path, tested first.
   - **Part 2:** the queue page and the decision command (`catalyst review-queue`, `catalyst review-decide`), plus corrections. A correction stores the person's values as a new claim by the reviewer, on the same verbatim quote in the same stored copy, so every published value still has a claim and a quote. Part 2 widens the gate's rule on which claims may publish for that, under its own review.
   - **Part 3:** the two GitHub Actions jobs. This is an owner-path pull request of its own (ADR 0036).
6. **The gold set.** Recorded decisions are kept so that the "Gold set of 200 verified catalysts" item can add them. This item does not change the gold set.

## Alternatives

- **A local web page (FastAPI on 127.0.0.1).** This is closest to "one screen with keyboard shortcuts". But the owner would have to install the code and hold the live database password locally (ADR 0011). That is the setup the owner asked to avoid.
- **A hosted page with a sign-in.** It needs a host, an account, and an authentication design. It would also be the first thing reachable from outside the project. That belongs with the Phase 2 web app.
- **Decisions by issue comment.** Comment text can come from anyone who can comment, and comments can be edited. A hand-started job records a verified account, and plain code reads its inputs.
- **Let an approval excuse every hold.** That would publish a quote not found in the stored copy, or make the writer guess a catalyst or a drug role.
- **Overwrite one row per claim.** That loses the history of who decided what and when.

## Consequences

- The owner reviews on GitHub, in the browser, with two clicks per decision and no setup. Until part 3 merges, decisions can only be recorded by code, as the tests do.
- Each approval waits for the next scheduled decision and writer run before it shows. The writer's report says how many approved claims it wrote.
- Holds that need a choice wait for corrections in part 2.
- Revisit when the Phase 2 web app exists: the screen can move there, with keyboard shortcuts, using the same record and the same gate path.
