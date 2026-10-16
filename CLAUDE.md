# Instructions for Claude Code

This repository builds the biotech catalyst tracker described in `docs/spec.md`.

@AGENTS.md

When the owner says "start", "start implementing", "continue", or similar:

1. Run the session-start protocol in AGENTS.md (setup check, then reminders for anything missing).
2. Find the first unticked item in `docs/roadmap.md` and say which item you are starting and which checkpoint in `docs/quality-gates.md` it leads to.
3. Work until that checkpoint, then stop and ask "Approve checkpoint X?"
