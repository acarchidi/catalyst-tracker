# 0042 — The weekly link check reads only published sources; only a missing or moved link is dead

- **Status:** accepted
- **Date:** 2026-10-20
- **Roadmap item:** Phase 1, "Auditor agent, weekly link check, sample checker on a stronger model, planted-error test, alerts only when something needs the owner plus a weekly summary" (part 2: the weekly link check)

## Context

The spec (Provenance, item 6) asks for a weekly job that checks every source URL, with a dead-link rate below 1%, measured weekly. `catalyst check-links` (ADR 0010) already fetches links politely: only the five source sites, each at its own rate, robots.txt obeyed, redirects never followed, one verdict per link. But it checks every stored document, and most stored documents back no published record: on Gate 1's data, 91% were registry records. It also fails on any verdict but "works", including a site that is briefly down. The spec leaves open which links count, what counts as dead, and when the owner hears about it.

## Decision

1. **Which links.** With `--published`, the check reads only the links that published records cite, exactly as the read API shows them (`catalyst.api.read.published`): every source of every value, and the source of every change in each record's history. A document no published record cites is not checked or counted.
2. **What is dead.** A link is dead when it is missing (HTTP 404 or 410) or has moved (a redirect). A refusal (401, 403, 429), a site error or no answer, and a link robots.txt keeps us from checking are counted as "not answered". They are not dead, because they say nothing about the document itself.
3. **The rate.** The dead-link rate is dead links divided by links checked, shown to one decimal place beside the spec's target of under 1%.
4. **What the job reports.** `--summary FILE` writes the counts as JSON: checked, working, dead, the rate, not answered, and each dead link with its verdict. In published mode the command fails only when a link is dead.
5. **Alerts (ADR 0023).** Any dead link raises the job's alert (`job-failed:check-links`). The alert gives the count and the rate, then lists up to 20 dead links: only `https://` addresses we stored, each with its code-chosen verdict. A clean check closes the alert. Links that were not answered never raise an alert on their own.
6. **Gate 1 is unchanged.** Without `--published` the command checks every stored link and fails on anything but "works", as ADR 0010 says.

## Alternatives

- **Check every stored link each week.** It would spend most of its time on registry records no published value cites (about 35 minutes for ClinicalTrials.gov alone at its rate), and their dead links would not touch a published record.
- **Count refusals and site errors as dead.** A site that is down for an hour would send the owner an alert about documents that are fine.
- **Search for the moved document now.** The spec asks for a search when a link dies, and an Internet Archive copy otherwise. That is the auditor's next part. Until it exists, the alert tells the owner which records need it.

## Consequences

- The weekly job checks a few hundred links, not thousands, inside each site's rate limit.
- A moved SEC or press-release link reaches the owner within a week, with its address.
- Remaining parts of the roadmap item: the scheduled workflow for this check (an owner-path pull request), the search for moved documents with an archive fallback, the auditor's re-check that quotes are still on the live page and that past dates have an outcome, the weekly summary, and scoring the sample checker (ADR 0041) on the planted errors.
