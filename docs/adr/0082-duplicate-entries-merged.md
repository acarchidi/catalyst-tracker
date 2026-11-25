# 0082 — Two open entries that are one event become one

- **Status:** accepted
- **Date:** 2026-11-25
- **Roadmap item:** none; a root-cause fix after the first weekly issue (owner request, 2026-11-25)
- **Amends:** ADR 0018 part 5b (the writer may now mark an entry merged, from plain code only, and holds a claim that fits only a merged entry) and ADR 0070 decision 4 (no read shows a merged entry either)

## Context

The weekly issue sometimes listed one real event twice for one company: the same drug's conference presentation, or the same readout from two releases. The writer (ADR 0018 part 5b, `catalyst.publish.live.place`) puts a claim on an open entry only when the company, the type and the matched drug records are the same, and then the trials and the indication. So one drug matched to two drug records ("pegozafermin" and "pegozafermin (BIO89-100)"), or one talk read from two releases that matched the drug differently, made two entries. The second reading already reads drug names this way (ADR 0062), but the writer compares records, not names.

Live data is never edited by hand (AGENTS.md), and loosening `place` would let a claim move another event's dates. The fix has to be a separate, strict rule that only ever hides an entry another one already shows.

## Decision

1. **Where it runs.** Last in the writer (`publish_claims`), inside its lock and transaction, after the night's claims are placed: `catalyst.publish.duplicates.merge_duplicates`. Plain code, no model calls, no spending. It also runs on a night with nothing new to place.
2. **What it compares.** Every open entry (upcoming or slipped) the gate wrote, among those of the same company and type. For each: the drug as its creating claim names it, the roles of its drug links, its linked trials, its stated indication (normalised, as `place` reads it) and its matched indication, its window, the meeting names its extractor claims' quotes give, and the rank of the source behind its window.
3. **Two entries are one event only when every test holds:**
   - the same company and the same type;
   - the same drug, read by the second reading's own rule (`catalyst.verify.second.drug_names_agree`, ADR 0062): "NAME (OTHER)" is one drug, form words and case do not count, "X + Y" is "X in combination with Y", and "X with Y" is never X. Both must name a drug: two entries without one (most deals, offerings, earnings) are never merged, since plain code cannot tell two such events apart. When both have drug links, the links' roles (alone, in combination) are the same;
   - the same phase, stage and regulator or region, as plain code reads them from each entry's creating claim: its own title (the extractor's, never published) and its quote (`title_marks`). "Phase 2" is not "Phase 3", "interim" is not "final" or "topline", and a BLA to the FDA is not an MAA to the EMA. One side naming a mark the other lacks is unsure, so nothing happens (Opus review of 66ecb0f, finding 1);
   - the trials are the same; or one side names none and the two titles are the same, word for word after case and punctuation (finding 1): a readout with no trial beside a trial's readout is otherwise often another study;
   - the indications are the same, or one side names none, both as stated and as matched;
   - the windows overlap;
   - for a conference presentation, also the same meeting name, or the very same dates when either names none. A meeting name is a run of capitalised words after "at" or "during" holding a meeting word ("Meeting", "Congress", "Symposium" …) and a word of its own; years, ordinals, "the", "annual" and a short form in brackets do not count ("the American Society of Clinical Oncology (ASCO) 2027 Annual Meeting" reads "american society of clinical oncology meeting"). A session ("Late-Breaking Session", "Oral Session") is part of a meeting, never its name, so two meetings' late-breaking sessions are not one meeting (review nit). Two different named meetings are two events, even on the same dates, and an entry whose quotes name two meetings names neither for sure.
4. **Groups.** Entries that are one event form a group. A group is merged only when every entry in it is one event with every other (an entry without a trial between two entries of two trials is not), and one source outranks all the others: a higher tier, then a later publication (spec: Which claim wins). That entry is kept. A tie decides nothing. When unsure, nothing happens.
5. **What it writes,** for each other entry of the group, in one transaction:
   - a claim of its own: agent `deduplicator`, prompt id `merge_duplicates`, version `1.1.0`, provider and model `plain code`; its quote, document and place are those of the claim behind the kept entry's window, word for word, already checked against the stored copy; its value names the kept and the merged entry; its field is `merged_into:<merged entry id>`;
   - a `status` change on the merged entry from its open status to the new status `merged`, citing that claim, marked as a correction, later than the entry's last change;
   - one `catalyst_merged_source` row per claim the merged entry's history cites (plain-code claims left out), linking it to the kept entry.
   It never creates an entry, never changes the kept entry's window, title, type, drugs or trials, and never moves a date. It makes no check, decision, second reading or review row.
6. **What the reads show.** No read (API, MCP, export, and so the digest) shows a merged entry, as for a withdrawn one. The kept entry lists each linked claim among its `sources`, with the field `same_event`, while the entry it came from is still merged. The indication step never writes to a merged entry.
7. **The duplicate does not come back, and nothing moves the kept entry from it** (Opus review of 66ecb0f, findings 2 and 3). The writer's index keeps merged entries aside with the entry each was merged into.
   - A claim that would be a new event but fits a merged entry (as `place` matches) is held for a person: "the claim fits only an entry merged into another as the same event; a person must decide". It is never sent to the kept entry. That is the simpler of the two safe options: a merged entry may have named no trial, so such a claim may be another trial's event, and placing it on the kept entry could move a published window from another study. No approval lifts the hold, as for the writer's other "which entry" holds; a person rejects it, or lists it for withdrawal. A claim that fits the kept entry itself is placed on it as before.
   - When the kept entry is withdrawn (ADR 0070) or closed by plain code (ADR 0080), every entry merged into it is set aside with it: in the writer's index it counts as withdrawn or closed, with the kept entry's cutoff (when it was withdrawn, or when the completion document was published) and a closed deal's names. A claim fitting only the merged entry is then held by those ADRs' own rules, and only a document published after the cutoff makes a new entry. The merged entry's stored status stays `merged`; no read shows it either way.
8. **Safe to re-run.** A merged entry is open no more, so a re-run compares nothing for it; the claim and link inserts also ignore duplicates.
9. **Reversible.** The merged entry keeps every row it had; the merge is one status change and some links. One status change back undoes it, and its links then stop showing by themselves (decision 6). Migration `f2b9d4c6a8e1` adds the status and the table, with row-level security on; its downgrade refuses while any entry is merged or any link exists.

## Alternatives

- **Loosen `place` to match drug names, not records.** A claim would then move the dates of whichever entry it matched, and a near-miss would change a live date instead of leaving two entries. Rejected: a merge must never write to the kept entry.
- **Merge the drug records.** The resolver's tables would change for every claim of both records, far beyond the duplicate, and the drug list has its own review path (ADR 0035, ADR 0060).
- **Withdraw the duplicates by hand (ADR 0070).** Each needs a person and a pull request, and a later release brings the duplicate back.
- **Reuse `withdrawn`.** It means "found wrong after review"; a duplicate is right, and its sources belong to the kept entry. A distinct status keeps the history honest.
- **Change history rows on the kept entry for the extra sources.** The read layer takes each field's newest change as its source, so a row citing the merged entry's claim would change which source stands behind a value. A link table adds sources without touching any value.
- **Merge on looser terms (any shared drug, nearby windows).** A drug alone and in combination, or two trials of one drug, are two programmes with their own dates (ADR 0062 decision 3). Rejected for "when unsure, do nothing".

## Consequences

- An entry shown twice in an issue disappears from the next issue once both entries pass every test; one that misses a test (another indication, another meeting, another trial, windows that do not meet, a combination) stays listed twice: a missed merge, never a wrong one.
- Two deal entries of one company that name the same counterparty still cancel each other in ADR 0080 and stay open until their windows end; this rule leaves deals alone because they name no drug.
- One reversible migration; the downgrade is refused once a merge has run.
- The writer reads the reference tables once a night when a company has two open entries of one type; that is plain code and small.
