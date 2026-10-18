# 0013 — The catalyst extractor: the model proposes, code checks, claims wait for the gate

- **Status:** accepted
- **Date:** 2026-10-17
- **Roadmap item:** Phase 1, "Extractor agents and prompt evaluation sets" (second part)

## Context

The spec gives the extractor no tools and no database access, requires every value to match a verbatim quote in the stored source, and lets only the publish gate write live data. It leaves open how a proposed catalyst is shaped, which event types an extractor of press releases looks for, how date confidence is labeled, and how re-runs avoid paying twice. The publish gate and the second-model verifier are later roadmap items.

## Decision

1. **Prompt `extract_catalysts` 1.0.0** (`prompts/extract_catalysts/1.0.0.toml`). The model reads the publication date and the document text (ADR 0012). It returns a list of future events, each with a type, a short title, the drug and indication as written, the date words as written, and a quote of one or two sentences that states the event and its time. The document is declared to be data, not instructions.
2. **Event types.** Ten forward-looking types from `CatalystType`: PDUFA date, advisory committee, topline readout, conference presentation, regulatory submission, regulatory acceptance, CHMP opinion, label expansion, enrollment completion, and deal closing. Complete response letters, offerings, shelf filings and earnings are left to code that reads SEC and FDA records, because a press release reports them after the fact.
3. **Code checks every proposal** (`catalyst.extract.agent`): the quote must be found in the text (ADR 0012), the date words must be inside the quote as whole words, code turns them into a window, and a window that ended before publication is flagged. Each failure is kept as a named problem; nothing is corrected or guessed.
4. **Date confidence** is set by code from the type: PDUFA dates and advisory committees are "regulator-scheduled"; every other date in a company's release is "company-confirmed".
5. **Claims, not catalysts.** Each proposal whose quote was found becomes one `claim` row (`subject_type` catalyst, `field` "catalyst:" plus a 16-character fingerprint of the proposed event, so two events stated in one sentence get two claims; review of PR #16), with the proposal, its window, and its problems as the value, plus the prompt, provider, and model that made it. A proposal whose quote is not in the source makes no claim. No `catalyst` row is written: that is the publish gate's job.
6. **The job** (`catalyst extract-catalysts`) reads the newest stored 8-K Exhibit 99.1 press releases, up to `--limit` (default 50), and skips any document that already has claims from this prompt version. A claim is stored once (the table's unique key). A role at its daily cap stops the job and says so; a failed document is reported and the job goes on. The product key `CATALYST_ANTHROPIC_API_KEY` is the only key it reads.

## Alternatives

- **One claim per field** (type, date, drug separately). Closer to "one fact per claim", but a catalyst's fields are only meaningful together and share one quote. One claim per catalyst can be split later without re-extraction, because the value keeps every field.
- **Let the model label date confidence.** It would add a judgment code can make from the type alone.
- **Write unverified proposals as catalysts marked "unverified".** Faster to show, but it would bypass the publish gate the spec requires.

## Consequences

- The demo can show proposed catalysts with their quotes, labeled as not yet verified, straight from the claims.
- A document where the model found nothing has no claim, so a later run reads it again. Each re-read costs a few cents at the prices in `config/models.toml` (a median release is about 4,000 input tokens); the daily cap ($2 since ADR 0022) bounds the total. A record of empty extractions can come with the publish gate.
- The prompt has no scores yet. The gold set and its scorer are the third part of this roadmap item; until they exist, no prompt change can be judged, so the prompt stays at 1.0.0.
