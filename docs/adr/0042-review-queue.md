# 0042 — The review queue: a private page for the owner, approvals through the publish gate

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
- The repository is public (owner, 2026-10-20). Anyone can read its job logs, job summaries, uploaded files, issues, and the inputs of a hand-started job. Held claims are unverified, so their quotes, reasons, and proposed values must never appear in any of those (AGENTS.md: no unverified data made public).
- Only people with write access can start a job by hand, and GitHub records who started it.
- The web app (`web/`) starts in Phase 2.
- `.github/workflows/` is an owner path (ADR 0036).
- The gate stores each held claim's reasons in plain words (`claim_decision`).

## Decision

1. **Form factor: a private page, decisions by identifier only.**
   - **Where the held claims travel.** A live job reads the held claims and writes them only into the encrypted export, which is designed separately. What the job prints, in its log or summary, is counts only. No held claim's text ever goes into a log, summary, uploaded file in plain text, or issue.
   - **Where the owner sees them.** The coding session decrypts that export and builds the queue page as the owner's private Claude page (an artifact, private unless the owner shares it). For each held claim the page shows:
     - the quote, highlighted in its paragraph of the stored copy;
     - the link to the source;
     - the gate's reasons, in plain words;
     - the proposed values;
     - the claim's identifier and a short code for its reasons.
   - **How a decision is recorded.** The owner starts the "Review decision" job by hand with three inputs: the claim's identifier, approve or reject, and the reasons code. The page gives all three ready to copy. These inputs are public, so they carry only identifiers:
     - the claim's identifier is a random UUID;
     - the code is the first 8 hexadecimal characters of a SHA-256 hash of the reasons list. It shows that the reasons have not changed since the page was made, and says nothing readable about them.

     Someone who guesses a claim's exact reasons could check the guess against the code. That would tell them only why an unpublished claim is held, never its quote or values.
   - The job records the GitHub account that started it as the reviewer, and refuses any account but the repository owner's. Its log and summary say only the claim's identifier, the action, and whether it was recorded or refused, with a refusal reason that quotes no claim text.
   - This needs no new account. Reading the encrypted export needs the key that its own design names.
   - Keyboard shortcuts wait for the Phase 2 web app.
2. **The record (`review_decision`).**
   - One row per decision: the claim, approve or reject, who, when, and the gate's reasons the person saw, word for word.
   - Rows are only ever added. A claim's latest row counts, so a rejection can withdraw an approval before the writer runs, and nothing is erased.
   - The decision is refused, with the reason, in three cases:
     - the claim is not held;
     - the reasons the person saw are not the claim's stored reasons now (the claim was decided again since the page was made);
     - it does not say who made it.
   - The claim's stored decision is locked while the decision is checked and recorded, so the decision job cannot change the reasons in between.
   - Row-level security is on, with no policy (ADR 0011).
   - The gate's own rows can be rebuilt, but a person's decisions cannot. So the migration's downgrade refuses to run while the table holds any row.
3. **An approval goes through the gate.**
   - Recording it writes no live data.
   - The decision job (`catalyst decide-claims`) and the writer (`catalyst publish-claims`) both apply it with `gate.approve`. That excuses only holds whose reasons the person saw, word for word. A new reason since then still holds the claim.
   - The writer then checks the claim again and writes it as it writes any claim. Every change row cites the claim, and so its stored source and verbatim quote. The writer's run report counts these claims as `published_by_approval`.
   - No approval excuses (`PERSON_MAY_NOT_EXCUSE`):
     - the quote match against the stored copy (spec: Provenance);
     - a claim that is not the extractor's catalyst claim, or comes from an older extractor prompt;
     - a missing window;
     - a passed window on a new catalyst;
     - a check that has not run, since nobody can vouch for it.
   - Of the writer's own holds, an approval lifts only two (`PERSON_MAY_APPROVE`): a date move of more than 6 months, and a window that does not overlap the published one. Each needs only a yes or no about the claim's own dates. Two safeguards come from the Opus review of PR #84:
     - **Every date reason is listed.** Against a published catalyst, the writer's check gives every reason that fails, in order, joined by "; ". So lifting one never skips another. An approval lifts the hold only when every part can be approved (`person_may_approve`). A passed window never can. Example: a slip from "first half of 2027" to "fourth quarter of 2026" is approved, but the writer runs on 2027-01-05. The reason now also says the new window has passed, so the claim stays held and comes back to the queue.
     - **Date reasons name both windows**, as in "(from 2027-01-01..2027-06-30 to 2026-10-01..2026-12-31)". If the published window changes after the approval, the text no longer matches what the person saw, and the approval lifts nothing.
   - The writer's other holds need a choice: which of several catalysts, whether the drug is tested alone or in combination, or which of two equal sources. An approval leaves them held, because the writer must never guess. Corrections (decision 5) will handle them.
   - An approval does not override the source ranking (spec: "Which claim wins"). An approved claim that a better source outranks writes nothing.
4. **A rejection never publishes.** The gate excuses nothing for a rejected claim. It stays held with its reasons, and the queue page leaves it out. Nothing is deleted.
5. **Built in parts:**
   - **Part 1 (this):** the record and the approval path, tested first.
   - **Part 2:** the queue page, built from the decrypted export with no database access, and the decision command (`catalyst review-decide`), plus corrections. The export's held items gain what the page needs: the quote's paragraph and the claim's latest review decision, so rejected claims leave the page. A correction stores the person's values as a new claim by the reviewer, on the same verbatim quote in the same stored copy, so every published value still has a claim and a quote. Part 2 widens the gate's rule on which claims may publish for that, under its own review.
   - **Part 3:** the "Review decision" GitHub Actions job. This is an owner-path pull request of its own (ADR 0036). The held claims reach the page through the encrypted export's own job, so no second job is needed. The job:
     - takes the reviewer from GitHub's record of who started the run (`github.actor`), never from a job input;
     - refuses anyone but `acarchidi`, both in the job's settings and in the code it runs;
     - works out the reasons the person saw from the short code: the code must match the claim's stored reasons now, and those reasons are what is recorded.
6. **The gold set.** Recorded decisions are kept so that the "Gold set of 200 verified catalysts" item can add them. This item does not change the gold set.

## Alternatives

- **A local web page (FastAPI on 127.0.0.1).** This is closest to "one screen with keyboard shortcuts". But the owner would have to install the code and hold the live database password locally (ADR 0011). That is the setup the owner asked to avoid.
- **The queue in a job summary.** This was the first choice, while the repository was private. Now that it is public, a summary would publish unverified claims.
- **A hosted page with a sign-in.** It needs a host, an account, and an authentication design. It would also be the first thing reachable from outside the project. That belongs with the Phase 2 web app.
- **Decisions by issue comment.** Comment text can come from anyone who can comment, and comments can be edited. A hand-started job records a verified account, and plain code reads its inputs.
- **Let an approval excuse every hold.** That would publish a quote not found in the stored copy, or make the writer guess a catalyst or a drug role.
- **Overwrite one row per claim.** That loses the history of who decided what and when.

## Consequences

- The owner reviews on a private page and decides on GitHub, with one copy and one job start per decision, and no new account. Until part 3 merges, decisions can only be recorded by code, as the tests do.
- The page is only as fresh as the latest export. A decision made from an old page is refused, safely, when the claim's reasons have changed since.
- The page depends on the encrypted export. If that design changes, only part 2's page-building changes; the record and the gate path stay.
- Each approval waits for the next scheduled decision and writer run before it shows. The writer's report says how many approved claims it wrote.
- Holds that need a choice wait for corrections in part 2.
- Revisit when the Phase 2 web app exists: the screen can move there, with keyboard shortcuts, using the same record and the same gate path.
