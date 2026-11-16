# Weekly issue hand-off (ADR 0055)

Run each Monday by a fresh coding session. It builds the free and paid Substack pages from the live data and gives them to the owner privately. Print counts only. Never commit, publish, upload as an artifact, or paste into a pull request, issue, or log: the export, the private key, or either page.

Work in the session's scratchpad directory, written below as `$S`. Every file made here stays there with mode 600.

1. **Key pair.** `uv run --frozen catalyst seal-keygen --private-key $S/keys/week.pem > $S/keys/week.pub`, then `chmod 600 $S/keys/week.pem`. Only the public key leaves the session.
2. **Sealed export.** Start `extract-live.yml` on `main` with inputs `mode=export` and `public_key=<contents of week.pub joined onto one line, line breaks removed>`. Through the REST API, send the dispatch body as JSON with `Content-Type: application/json`. Wait for the run to finish. The export makes no model call.
3. **Unseal.** Download the run's job logs to `$S/dl/week/log.txt`. The log holds one block starting `-----BEGIN CATALYST SEALED FILE-----`. Run `uv run --frozen catalyst unseal --private-key $S/keys/week.pem --in $S/dl/week/log.txt --out $S/dl/week/export.json`, then `chmod 600` it.
4. **Build both pages.** For `a` in `free` and `paid`: `uv run --frozen catalyst digest --issue-date <this Monday> --from-export $S/dl/week/export.json --access $a --out $S/issue/$a.json --page $S/issue/$a.html`. Each command prints the week's counts.
5. **Look before sending.** Open the free page in the pre-installed Chromium with outside requests blocked, and check that it renders: title, colour key, the three sections, and the notice at the end.
6. **Hand over.** Send `free.html` and `paid.html` to the owner privately (the session's file-sending tool), with the counts from step 4 in two plain lines. Remind them the paid page is for paid subscribers only.
7. **Clean up.** Delete `$S/keys/week.*` and `$S/dl/week/`. Keep nothing else.

If any step fails, say which step and the error's first line, and stop. Do not retry more than once (AGENTS.md: two failures, stop and explain).
