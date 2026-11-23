# Weekly issue hand-off (ADR 0055)

Run each Monday by a coding session. It builds the free and paid Substack pages from the live data and gives them to the owner privately. Print counts only. Never commit, publish, paste into a pull request, issue, or log, or upload as a public artifact: the export, the private key, or either page. The one exception is step 6's private copy page (owner request 2026-11-23, ADR 0055 as amended).

Work in the session's scratchpad directory, written below as `$S`. Every file made here stays there; run `umask 077` first so each one is created readable by this session only.

1. **Key pair.** `uv run --frozen catalyst seal-keygen --private-key $S/keys/week.pem > $S/keys/week.pub`, then `chmod 600 $S/keys/week.pem`. Only the public key leaves the session.
2. **Sealed export.** Start `extract-live.yml` on `main` with inputs `mode=export` and `public_key=<contents of week.pub joined onto one line, line breaks removed>`. Through the REST API, send the dispatch body as JSON with `Content-Type: application/json`. Wait for the run to finish. The export makes no model call.
3. **Unseal.** Download the run's job logs to `$S/dl/week/log.txt`. The log holds one block starting `-----BEGIN CATALYST SEALED FILE-----`. Run `uv run --frozen catalyst unseal --private-key $S/keys/week.pem --in $S/dl/week/log.txt --out $S/dl/week/export.json`, then `chmod 600` it.
4. **Build both pages.** For `a` in `free` and `paid`: `uv run --frozen catalyst digest --issue-date <this Monday> --from-export $S/dl/week/export.json --access $a --out $S/issue/$a.json --page $S/issue/$a.html`. Each command prints the week's counts.
5. **Look before sending.** Open the free page in the pre-installed Chromium with outside requests blocked, and check that it renders: title, colour key, the three sections, and the notice at the end.
6. **Hand over.** Update the owner's private "Catalyst Tracker issue" artifact page, the same page and link every week (owner request 2026-11-23: "i don't want to have to repeatedly download stuff"). It is private to the owner and never shared. It holds: the issue date and counts in one line; the line "Paid issue: set the audience to Paid subscribers only before publishing."; four buttons, "Copy free title", "Copy free issue", "Copy paid title", "Copy paid issue", the issue buttons copying formatted HTML (text/html and text/plain) so Substack's editor keeps headings and links; then a preview of both issues, the paid one under its own heading. No runtime capabilities and no outside scripts. Then send the owner a push notification with the page link: "This week's issue is ready: open, copy, paste, publish."
7. **Clean up.** Delete `$S/keys/week.*` and `$S/dl/week/`. Keep nothing else.

If any step fails, say which step and the error's first line, and stop. Do not retry more than once (AGENTS.md: two failures, stop and explain).
