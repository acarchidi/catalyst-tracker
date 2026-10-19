# 0006 — Automatic review and merging

- **Status:** accepted; the auto-merge part (decisions 3–5) is superseded by ADR 0007
- **Amended by:** ADR 0036 (2026-10-19), decision 4: owner-path pull requests are now merged by the coding agent after a separate review, not by the owner with the bypass.
- **Date:** 2026-10-16
- **Roadmap item:** Repository skeleton: branch protection for main (owner request, 2026-10-16)

## Context

The spec asks for a reviewer agent separate from the developer agent, CI on every pull request, a protected main branch, and an Opus-class review plus a CODEOWNERS file for risky areas. The owner asked on 2026-10-16 for low-risk changes to merge without them, governed by a review agent, with as few manual steps as possible. Until now every merge waited for the owner, and main had no protection.

GitHub facts that shape the design:

- Pull requests opened from coding sessions are authored by the owner's account, and GitHub does not let an author approve their own pull request. A code-owner approval is therefore impossible; the owner merges risky pull requests with the ruleset's bypass instead.
- A skipped job counts as passing for a required check, so the review must run, not skip, on any pull request that can merge.
- Merges made with the workflow token do not start other workflows, so CI does not re-run on main after an automatic merge.

## Decision

1. **Review check.** `.github/workflows/claude-review.yml` runs `anthropics/claude-code-action` (pinned to a commit) on every pull request that is not a draft. The reviewer runs Opus, can read files and the pull request but change nothing, and returns a verdict, a summary, and blocking and optional findings as JSON. `scripts/review_verdict.py` turns that into the required "Claude review" check: only an explicit pass with no blocking findings passes; a missing or malformed result fails. A second job posts the report as one comment on the pull request and updates it on later runs.
2. **Prompt injection.** The reviewer is told that pull request text and code are data, and that text trying to steer the verdict is itself a blocking finding. Its tools are reading files and `gh pr view` and `gh pr diff`; its token can read but not write. The workflow, the verdict script, CODEOWNERS, and every CLAUDE.md and AGENTS.md are owner-owned paths, so a pull request that changes how it is reviewed waits for the owner. Such a pull request runs its own changed review, so its "passed" comment proves nothing; the developer agent runs a separate review and reports it to the owner in plain words.
3. **Automatic merge.** After a passing review, a job turns on GitHub auto-merge (merge commit) for pull requests from this repository's `claude/` branches into main. GitHub then merges once every required check passes and no code-owner review is outstanding. The job first reads main's active rules and turns nothing on unless all four checks and code-owner review are required, because auto-merge on an unprotected branch merges at once. Converting a pull request back to draft turns auto-merge off.
4. **Owner-only paths.** `.github/CODEOWNERS` lists migrations, database code, future verification, publish-gate, auth, and billing code, model settings and spend caps, the rule documents, CI and review settings, and dependency files. Pull requests touching them wait for the owner, who merges with the bypass after reading the plain-English summary and the review.
5. **Ruleset.** `.github/rulesets/main.json` is imported once by the owner. It protects the default branch: no deletion or force-push, pull requests required, code-owner review required, and the four checks required (three CI jobs and "Claude review"). Repository admins (role ID 5) may bypass only through a pull request, never by pushing directly.
6. **Drafts and Dependabot.** Drafts are not reviewed. The skipped job reports as a passing check, which is safe only because a draft cannot merge and auto-merge is turned off when a pull request becomes a draft; marking it ready starts a fresh review within seconds. Dependabot pull requests fail the review check with a message, since they cannot read the review key; they change dependency files, so they wait for the owner anyway. Dependabot now runs monthly instead of weekly to cut the owner's clicks.
7. **Cost.** The reviewer uses its own key, `CATALYST_REVIEW_API_KEY`, kept apart from the product's key so review spend and product spend are separate. The owner sets a monthly spend limit on it. Runs on the same pull request cancel earlier runs, and each run is capped at 60 turns and 30 minutes.

## Alternatives

- **Owner approves every merge (status quo).** Safest, but every pull request waits on the owner, who asked for fewer manual steps.
- **Merge from the coding session.** A writer approving and merging its own work is not a separate review.
- **Run the review on `pull_request_target`.** It would run the base branch's workflow, but it exposes secrets to code from forks; this repository takes no fork contributions, and `pull_request` plus owner-owned workflow paths covers the same risk.
- **Fail the review check on drafts instead of skipping it.** Closes the few seconds between "ready" and the new review starting, but every push to a draft would email the owner a failure.
- **A second GitHub account or App for approvals.** It would make code-owner approval possible, but the owner would have to create and manage it.

## Consequences

- The repository is private, and GitHub enforces rulesets and offers auto-merge on private repositories only on paid plans (Pro, Team, Enterprise). On the free plan none of this is enforced and the auto-merge job turns nothing on.
- The owner has four one-time steps: merge the open pull requests, import the ruleset, tick "Allow auto-merge", and add the review key secret, in that order. `docs/setup.md` Part 6 gives the click paths.
- Until the review key exists, every ready pull request shows a failing "Claude review" check, and nothing merges automatically.
- Each review costs model spend on the review key; Opus at $4 per million input tokens and $20 per million output tokens (config/models.toml, checked 2026-10-16), so a typical review is likely well under $2. Watch the first week's spend.
- CI does not re-run on main after an automatic merge, and the ruleset does not require a branch to be up to date with main before merging. Two pull requests that each pass alone could conflict in behavior once both merge; the next pull request's CI would show it.
- The owner's bypass skips every check, not only code-owner review. `docs/setup.md` Part 6 says when it is safe to use.
- A pull request first reviewed against another base and then retargeted to main keeps its check, since checks attach to the commit. This is safe for stacked pull requests, which are retargeted only after the one below merges, so their change against main is the one reviewed.
- A reviewer verdict can be wrong in either direction. CI, the CODEOWNERS paths, and the checkpoint approvals stay in place; the review adds a check and removes none.
- The admin role ID 5 comes from GitHub's Terraform provider docs and a ruleset-recipes report, not GitHub's REST reference. If the import refuses the bypass entry, the owner adds "Repository admin" to the bypass list in the ruleset page instead.
