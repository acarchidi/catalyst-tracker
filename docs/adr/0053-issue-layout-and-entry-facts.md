# 0053 — Issue layout, and the facts each entry needs

- **Status:** accepted. The owner decided on 2026-11-10 what each issue shows: everything in the Context list below, and skipping issue 1. How decisions 2 to 6 get those facts, and the order, are the coding agent's proposals, marked "(agent's proposal)". Decision 1 is built with this ADR; decisions 2 to 6 are roadmap items
- **Date:** 2026-11-10
- **Roadmap items:** Phase 2, "Issue layout" and "Facts on every entry"

## Context

On 2026-11-10 the owner skipped issue 1 (dated 2026-11-09): "we arent ready to launch just yet, so we have to skip this one." The owner asked for these changes across all publications:

- the financial-advice notice once, at the very end, in smaller type;
- sections named "Rescheduled Catalysts", "New", and "Upcoming Catalysts through [date]";
- each entry to state the indication and the development phase, preclinical included, and the cohort size for clinical trials;
- no "Date label: company-confirmed" line and no quote such as "Moderna expects potential data in 2026"; approximate dates written as "When: Company expects potential data in 2026.", exact dates as the date;
- an unnamed "medical conference" identified, with its dates, where possible;
- a colour code for the type of catalyst, with a key at the top, and separate categories for preclinical, clinical, and conference presentations at least;
- links to the company's pipeline page, and to the program's own page if it exists, in the sources line.

Facts from the export of 2026-11-10 (147 open catalysts on the paid calendar):

- **Indication:** empty on all 147. The extractor proposes one, but nothing checks it against the quote, so the publish gate does not write it (`catalyst/publish/title.py`). The read API shows an indication only with a MeSH identifier.
- **Phase and cohort size:** the trial table holds the registry phase and enrollment, but ADR 0039 keeps them out of published records. The spec requires a verbatim quote behind every published value, checked against the stored copy (Provenance, rules 4 and 8), and a registry field is not a sentence. Only 1 of the 147 catalysts is linked to a trial.
- **Preclinical:** no catalyst type or field says a program is preclinical. Coverage today is clinical and regulatory events (spec: Catalyst types).
- **Conferences:** some quotes name the meeting, for example "the American College of Gastroenterology (ACG) 2026 Annual Scientific Meeting and United European Gastroenterology (UEG) Week 2026". Others say only "a medical conference".
- **Substack:** its help pages do not say whether pasted text keeps colours or a smaller size. Third-party guides disagree (searched 2026-11-10).

## Decision

### 1. Layout (built now)

- **Top:** the title, then "Full issue for paid subscribers." on the paid page, then the key.
- **Key and colours:** coloured squares, not styles, so they survive a paste into Substack and into email clients:
  - 🟦 Clinical data (topline readouts);
  - 🟪 Conference presentation;
  - 🟩 Regulatory (PDUFA dates, advisory committees, submissions, acceptances, CHMP opinions, complete response letters, label expansions);
  - 🟨 Trial progress (enrollment completion);
  - 🟥 Corporate and financing (offerings, shelf filings, mergers, earnings).
  - Each entry's title also names its type in words, for readers who cannot tell the colours apart.
  - 🟧 Preclinical joins the key with decision 3, once a published stage can say "preclinical"; until then no record could carry it.
- **Sections:** "Rescheduled Catalysts (n)", "New (n)", and "Upcoming Catalysts through [date] (n)". The date is the free calendar's last day (the issue date plus 30 days). On the paid page, which has no last day, it is the latest day any listed window reaches.
- **When:** a day is written as the day, for example "When: 15 December 2026.", with "(set by the regulator)", "(registry estimate)", or "(our estimate)" when the company is not the one standing behind it. A wider window uses the source's own words, for example "When: Company expects potential data in 2026." The words before the date are fixed per catalyst type, computed by plain code (ADR 0047, decision 1). They replace the date label without losing who stands behind the date.
- **The quote:** no longer printed in the issue. The source link carries a text fragment (`#:~:text=`), so the source page opens with the quoted sentence highlighted (spec: Provenance, rule 3). The fragment is percent-encoded by plain code, so it cannot add a tag or an attribute. Browsers without text fragments open the page at the top. The JSON issue, the read API, and the export keep every quote. This supersedes "the verbatim quote" and "the date label" in ADR 0047, decision 2, for the HTML only.
- **The notice:** printed once, last, inside `<small>`. Substack may show it at normal size; the owner checks the first paste. This supersedes "above and below" in ADR 0049, decision 6, ADR 0052, decision 1 (the segment issues' notices), and the roadmap's segment builder item. The same words in CP7b (quality-gates.md, owner-only) change in their own pull request, which must merge before CP7b runs.

Decisions 2 to 6 are the coding agent's proposals for how to get the facts the owner asked for.

### 2. Indication on every entry

Amended 2026-11-10, after measuring the export of that day (agent's proposal):

- **MeSH match:** plain code matches the indication's words to one MeSH disease.
  - The index comes from NLM's descriptor file (`desc2026.gz`), stored with its hash under a `mesh:` source key. It holds only the disease trees (C and F03): 5,194 descriptors and 60,341 terms, each inverted term also in reading order.
  - A text matches when its whole words hold disease terms of exactly one descriptor. Longer terms are found first. No term, or terms of two or more diseases, means no match, never a guess. There are no synonyms beyond MeSH's own terms, and no abbreviation of three letters or fewer matches alone.
  - Measured on 371 distinct extracted indications: 272 match one disease (73%), 36 name several, and 63 name none.
- **Its own quote:** the matched words are in the claim's date quote for only 81 of 395 claims, because releases usually name the disease in another sentence. So the indication gets its own verbatim quote, a sentence naming the drug and the disease. That quote is a new extractor output with a new prompt version, and the gold set is scored again. Plain code checks the quote against the stored copy and the disease words inside it, as for every quote.
- **Its own gate:** a catalyst whose indication fails these checks publishes as it does today, without an indication. Only the indication waits in the review queue with its reason. No existing check is loosened. This touches the publish gate: test first, and Opus review.
- **Three pull requests:** the MeSH file and matcher first; then the extractor's indication quote with the gold set; then the writer.

### 3. Development stage, preclinical included

- A new published field, `stage`: preclinical, phase 1, 1/2, 2, 2/3, 3, filed, or approved.
- It comes from the quote's own words ("Phase 2 trial", "IND-enabling", "preclinical"), from a new extractor prompt version, with a whole-word check.
- When a linked trial's registry phase disagrees, the existing rule check sends it to review (spec: Rule checks).
- The gold set gains stage labels and both readers are scored again. CI blocks a drop in any field's accuracy.

### 4. Cohort size, and registry phase, from the stored registry record

- **The claim:** a registry-field claim that holds the value and its exact JSON path in the stored ClinicalTrials.gov record. The publish gate checks that the value is at that path in the stored copy, with its hash.
- **Printed as:** for example "Cohort: 120 participants (registry, estimated)", with the registry record linked in the sources line.
- **Spec change needed:** this changes how the spec's Provenance rules 4 and 8 apply to registry fields. That is the owner-only spec and an Opus-reviewed pull request of its own, superseding ADR 0039, decision 2, in part.
- **More trial links:** NCT numbers and study names found in the stored source link a trial. A match by drug and sponsor alone is only a proposal for review, never a link by itself.

### 5. Pipeline and program pages


- A collector finds each covered company's website from its SEC data, or from links in its own filings.
- It reads robots.txt and fetches a few likely pipeline addresses at the usual rate limit. A page counts as the pipeline page only when it is on the company's own domain and names at least one of the company's published drugs.
- It counts as a program page when its title or main heading names the drug.
- Both are stored with their hash, linked as "Company pipeline" and "Program page", and checked by the weekly link check (ADR 0043).
- They back no published value, so the entry lists them on their own line, "Company pages:", apart from "Sources:".
- A later item may take phase and indication claims from these stored pages, with their quotes. That needs the company website added to the spec's source ranking, an owner-only change.

### 6. Conferences with their dates

- `config/conferences.toml`, reviewable by the owner, lists only which meetings to follow: the names and abbreviations sources use, and the official page's address.
- A collector stores each official page with its hash. The meeting's dates and city are claims from that stored copy, each with the verbatim sentence that states them, checked by plain code and written by the publish gate like any other claim (spec: Provenance). Nothing typed into the config file is ever printed as a date.
- When a quote names a followed meeting with published dates, plain code adds "ACG 2026 Annual Scientific Meeting, [first day] to [last day], [city]" with the official page in the sources line.
- **"A medical conference":** an unnamed meeting cannot be identified from that text. It stays as written until a later release or filing names the meeting, and that document then updates the record.

### Order

Decision 1 ships first. Then 2 and 6, which need no model change. Then 3, then 5, then 4 (it waits for the owner-only spec change). The agent proposes that the first issue waits for these facts, since the owner asked that every entry carry them (agent's proposal).

## Alternatives

- **Keep the quote in each entry.** It is the plainest proof that a date is real, but the owner wants shorter entries. The highlighted link keeps the proof one click away.
- **Colours through styles.** Substack and email clients may strip them, and ADR 0047 allows no styles in the issue's HTML.
- **A model writes each entry's sentence.** Easier to read, but recurring public text is computed, never drafted (ADR 0047, decision 1).
- **Show phase and size from the registry now, as unchecked registry data.** This breaks the spec's Provenance rules (ADR 0039, Opus review of PR #80).
- **Guess an unnamed conference from the month and the indication.** A guess is not a source.

## Consequences

- The issue's HTML may now contain `<small>`, and each link may carry a text fragment. The tests check both, and that source text still cannot add a tag or a link.
- The spec's Offering row "Primary-source link and quote: every record" still holds: the API, the export, and the JSON issue keep the quote, and the issue's link highlights it.
- Indication, stage, cohort, pipeline links, and conference dates each need their own pull request. Two need Opus review: the publish-gate change in decision 2, and the spec change in decision 4.
- Revisit the key when Phase 10's corporate events and Phase 8's devices add types.
