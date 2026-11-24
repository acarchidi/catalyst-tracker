# 0069 — A newer source of the same rank updates the entry

- **Status:** accepted
- **Date:** 2026-11-24
- **Roadmap item:** none; a fix to the publish gate's writer check (owner request 2026-11-24)
- **Amends:** ADR 0018, part 5b (`safe_to_write`)

## Context

About 127 claims were held by `safe_to_write` because a newer release of the same rank gave other dates, or named the indication differently from the published entry. The writer held any claim whose window did not overlap the published one ("a slip, or another event?"), whoever published it and when. The spec already says how to settle this: "among sources of equal rank, the most recently published wins" (Which claim wins).

Example: NextCure's merger entry said "close in November 2026", from an 8-K of 25 September 2026. A newer 8-K set the stockholder vote for 9 December 2026, so the close is later. The newer claim's December window did not overlap November, so it was held, and the issue still showed November under "Due".

The owner asked that such claims update the entry and record the change, not wait.

## Decision

1. **A newer source of the same rank moves a window that does not overlap.** When a claim's source has the same tier as the source behind the published window and was published on a later New York day, "the window does not overlap the published one" is no longer a hold. Every other check still applies, so the claim moves the window only when it passes all of them. The move is written as before: one change row per date field, citing the new claim, with the old value; the older rows, citing the older claim, stay. So the history shows both windows, each with its own source and quote.
2. **An older source of the same rank loses and writes nothing.** Its non-overlapping window is no longer a hold either: the newer one already won. It is counted as a lower-rank claim that writes nothing, as any losing claim is.
3. **A newer source of the same rank may fill in or leave out the indication.** A claim that fits a published catalyst in everything but the indication, where one of the two states no indication, updates it when it comes from a newer source of the same rank. From any other source it waits, as before ("the indication differs from the published one").
4. **What still waits for a person** (real conflicts):
   - two sources of the same tier published on the same New York day, including the same release (`TIED`, or "does not overlap");
   - a newer source of a lower tier, or an older one of a higher tier;
   - a move of either end by more than 6 months (spec: Change sanity), unless the new window lies inside the old one;
   - a window that has already passed while the published one has not (B2): a forecast made before the window ended is not news that the event happened;
   - two indications that are both stated and differ: plain code cannot tell a renamed indication from a second event.
5. **"Due" and "Open".** The move changes the window the issue reads, so the old window leaves "Due" in the same run. An entry whose window has fully ended is in no section of the issue (tested).

## What this does not do

A filing that says an event already happened (for example, PTC's 8-K of 21 September 2026 saying the ST-920 acquisition closed) makes no claim today: the extractor is told not to list past events, and the rule check `window_matches_words` fails a window that ended before its own release. Such an entry leaves the calendar only when its window ends. Taking it off earlier needs an outcome record ("resolved", with the filing's quote), which is the roadmap's "Change history and outcome agent". Loosening either check to fit this fix would weaken a verification check, which AGENTS.md forbids.

## Alternatives

- **Keep holding and let a person approve each one.** That is the current queue of about 127; the spec already decides them.
- **Any newer source wins, of any tier.** The spec ranks tier first; a newer wire item must not move an SEC filing's date.
- **Compare publication times, not days.** Two 8-Ks of one day often restate one plan in two wordings; the same day stays a person's call.

## Consequences

- Most of the held claims of this kind should now publish as date moves on the next run; the exact number is known only on the live data.
- A newer same-rank release about a different event that shares the company, type, drugs, and trials, with dates within 6 months, now moves the entry instead of waiting. The 6-month rule and the trial and indication matching still catch the clearest cases.
- Three writer tests and one review-queue test that showed the old hold now use a same-day source, which still waits; their assertions are unchanged.
