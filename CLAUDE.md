# Instructions for Claude Code

This repository builds the biotech catalyst tracker described in `docs/spec.md`.

@AGENTS.md

When the owner says "start", "start implementing", "continue", or similar:

1. Run the session-start protocol in AGENTS.md (setup check, then reminders for anything missing).
2. Find the first unticked items in `docs/roadmap.md` that can run side by side (ADR 0033) and say which items you are starting and which checkpoint in `docs/quality-gates.md` each leads to.
3. Work through the roadmap, earlier items first. At each checkpoint, post the preview and continue (ADR 0007); stop only for the cases AGENTS.md lists under "How work proceeds".
