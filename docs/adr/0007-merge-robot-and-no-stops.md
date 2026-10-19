# 0007 — Merge robot on the free plan; no stops at checkpoints

- **Status:** accepted
- **Amended by:** ADR 0036 (2026-10-19), decision 2 and the alternative "the developer agent merges its own pull requests": the coding agent now merges owner-path pull requests itself after a separate review; ordinary `claude/` pull requests still go through the robot.
- **Date:** 2026-10-16
- **Roadmap item:** Repository skeleton: branch protection for main (owner request, 2026-10-16)

## Context

ADR 0006 relied on a repository ruleset and GitHub's auto-merge. The repository is private, and GitHub offers rulesets on private repositories only on paid plans (Pro, Team, Enterprise Cloud); the owner chose to stay on the free plan. The owner then asked for work to proceed without manual steps: pull requests should merge without them, and work should not stop at checkpoints. Sessions still start only when the owner asks ("only when I say"), so the spec's "no always-on coding agent in v1" stands.

## Decision

1. **Merge robot.** `.github/workflows/merge-robot.yml` runs whenever the CI or Claude review workflow finishes on a pull request. `scripts/merge_gate.py` decides; the workflow merges (merge commit) only when the pull request is open, not a draft, targets main, comes from a `claude/` branch of this repository, has no `hold` label, and the latest "Lint, types, tests, migrations", "Dependency audit", "Secret scan", and "Claude review" check runs from GitHub Actions on its latest commit all concluded `success`. The merge is pinned to that commit, so a later push cannot slip in.
2. **The robot cannot be talked into merging itself.** `workflow_run` always runs the workflow file and the gate script from main, never from the pull request. The review itself runs from the pull request and reads its rules (AGENTS.md, CLAUDE.md, the spec, the quality gates) from it, so pull requests that change any of those, the workflows, the rulesets, the gate or verdict scripts, `.claude/`, `.mcp.json`, settings that make a required check weaker (`.gitleaks.toml`, `.pre-commit-config.yaml`), or the spending caps (`config/models.toml`) are left for the owner with one comment. The developer agent reports a separate review of them in plain words. `pyproject.toml` changes too often to list; the reviewer treats a lowered coverage, strictness, lint, or test-selection setting there as blocking.
3. **No stops at checkpoints.** Checkpoint reviews and previews still happen; work continues after them. Pass criteria that need the owner's judgment (CAL1, CAL2) or action (Gate 3) wait on the owner without blocking other work.
4. **Remaining reasons to ask the owner.** Anything public (data, pages, the API), anything sent to people outside the project, spending beyond the daily caps, and accounts or keys. These are outside the code and cannot be undone by reverting a commit.
5. **Brakes.** A `hold` label stops the robot for one pull request; disabling the workflow stops it entirely.
6. ADR 0006's auto-merge job and the draft auto-merge switch are removed. Its review check, CODEOWNERS file, and ruleset file stay; the ruleset is ready for a paid plan.

## Alternatives

- **GitHub Pro (about $4 a month) with ADR 0006 as written.** GitHub would enforce the rules for every merge, not only the robot's. The owner chose the free plan for now.
- **Owner merges every pull request.** Safest, and what the owner asked to stop doing.
- **Keep stopping at checkpoints.** The quality gates were written this way; the owner chose not to.
- **The developer agent merges its own pull requests.** A writer merging its own work is not a separate check, and the session's rules forbid it.

## Consequences

- Nothing in GitHub stops a direct push to main; the rule against it is in AGENTS.md only. The robot's checks guard only the robot's merges.
- The Claude review is now the only check between a passing CI run and main. A wrong "pass" reaches main unseen; the owner hears about it at the next checkpoint preview at the latest.
- Until the reviewer key exists (setup Part 6), "Claude review" fails and nothing merges by itself.
- CI does not run on main after a robot merge (merges made with the workflow token start no workflows). Every merge passed CI on its own commit; pull requests are no longer stacked, so each is tested on top of the main it merges into, except when two merge close together.
- Checkpoint previews become information rather than permission. Errors found by the owner later cost a fix pull request instead of a delay.
