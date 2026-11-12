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

- **MeSH match:** plain code matches the indication's words to one MeSH disease (`catalyst.mesh`).
  - The index comes from NLM's descriptor file, stored exactly as NLM sends it (the .gz file, about 17 MB), with its hash, under a `mesh:` source key. A run asks for next year's file first, because NLM keeps only the current year's.
  - It holds the disease trees (C and F03) less the broadest descriptors ("Disease", "Syndrome", "Rare Diseases", and any descriptor at the top of a tree, such as "Neoplasms" and "Infections"): 5,156 descriptors and 56,486 terms. Terms MeSH tags as abbreviations or acronyms are left out, and so are non-disease descriptors such as Recurrence, Disease Progression, Death, Inflammation, and Fibrosis.
  - Text and terms are read the same way: Greek letters spelled out, accents dropped, lower case, brackets and possessives removed, hyphens read as spaces, and "non" joined to the next word, so "non small cell" never reads as "small cell".
  - A text matches when its whole words hold disease terms of exactly one descriptor. Walking back from each term to the start of the text or another term, every word must only grade, stage, name people, or join ("advanced", "relapsed", "patients", "with"). Any other word narrows the term to a disease MeSH may not have ("noncirrhotic MASH with liver fibrosis" is not liver cirrhosis), and "not", "no", or "without" negates it. A cause after the term ("pruritus associated with PBC") narrows it too. All give no match, as do no term or terms of two or more diseases. There are no synonyms beyond MeSH's own.
  - Measured on 371 distinct extracted indications from the 2026-11-10 export: 153 match one disease (41%), 123 are narrower, 15 name several, and 80 name none. Two random samples of 40 matches were all correct. A miss sends only the indication to review. The extractor's indication quote (the next pull request) asks for the disease as the source names it, which should leave fewer narrowing words.
  - **What is printed:** the indication as its own quote words it, with the MeSH identifier attached for search and filters. The MeSH name (for example "Glycogen Storage Disease Type II" for Pompe disease) appears in no quote, so it is never printed as the indication.
- **Its own sentence, read by one model and checked by a second** (amended again 2026-11-10; the owner chose plain code after two model routes failed, then, after two Opus reviews blocked the plain-code checks, wrote: "feed any ambiguous problems of this type to an LLM. then, have another LLM check its work"):
  - *Tried first:* the matched words are in the claim's date quote for only 81 of 395 claims. Adding a disease and its own quote to extractor prompt 1.1.0 dropped four gated scores below their floors (recall 0.80 against 0.90, precision 0.889 against 0.90, type 0.938 against 0.944, window 0.875 against 0.889; run 42886179457). It was not merged, and the extractor stays at 1.0.0.
  - *Tried second:* a separate model reader per drug named the gold disease word for word for 8 of 16 events (run 42889537423). Given only the drug, it could not tell apart two events of the same drug. It was not merged.
  - *Tried third:* plain code alone (`indication_sentence`), deciding from sentence wording whether a drug is aimed at a disease. It found 17 of 508 held claims (3%), all right by hand check, but two Opus reviews of PR 124 found sentences it got wrong: another drug named by brand or generic name, side effects, excluded patients, and "following earlier results in". Wording rules cannot cover these. It was not merged.
  - *Chosen:* `catalyst.extract.disease`, in four steps.
    1. Plain code offers up to 20 visible sentences of the stored source that name the drug as whole words, with no invisible characters.
    2. The first reader (`indicate_catalysts`, extractor role) reads the drug, the catalyst's own quote, and the numbered sentences. It picks one sentence and copies the disease from it, or answers none.
    3. Plain code checks the answer: the sentence is one offered, and the disease (less a trailing bracketed abbreviation) is written in it, has no brackets, is not given there as another condition's cause, and names one MeSH disease, with the words after it in the copied words checked too.
    4. The second reader (`check_indication`, verifier role, its own prompt) reads only the drug, the event, that one sentence, and the disease. It answers whether the sentence states this drug is aimed at this disease for this event, and if not, which of eight reasons applies.
  - An indication stands only when both readers agree and every plain-code check passes. Every refusal keeps its reason.
  - Neither reader has tools or database access, and both read only stored, visible source text (spec: Agent architecture).
  - *Cost:* both run under existing roles and their daily caps ($2 each). No role, cap, or model is added.
  - *Scores:* the readers together are scored on the 18 gold events that name a drug (`evals/indicate_catalysts/`), and the checker on 39 planted cases (`evals/check_indication/`). Ten cases are real sentences and 29 are written; the false ones are the reviews' failing sentences.
    - First run (runs 42903067210 and 42903081793): both models confirmed "primary biliary cholangitis" from "pruritus due to primary biliary cholangitis", so 1 of 18 events was wrong. The checker confirmed all 3 planted cases of that kind. So plain code also refuses a disease the sentence writes after "due to", "associated with", "secondary to", and similar words, or before "-associated" and "-related". A trailing abbreviation in brackets is dropped.
    - Second run (runs 42904400128 and 42904405989, $0.06 for both): 5 of 15 events with a right answer found, 3 of 3 events with none left empty, 0 of 18 wrong. The checker confirmed 12 of 13 true cases and refused 23 of 24 false ones; the one it confirmed is also refused by the plain-code cause check.
    - The separate Opus review of PR 124 found one more wrong answer hidden by lenient labels. Two SPY072 events share one sentence naming two diseases, and the first reader gave both the same disease. So the labels are now strict, and plain code refuses an indication when the event's own quote names more than one MeSH disease. Model inputs are now quoted fields, so source text cannot imitate the layout.
    - Third run, current (runs 42906242447 and 42906248003, $0.07 for both): 2 of 15 found, 3 of 3 left empty, 0 of 18 wrong. The checker confirmed 13 of 13 true cases and refused 25 of 26 false ones; the one it confirmed is refused by the plain-code cause check.
    - Each of the 13 misses is a refusal, not a wrong disease: the MeSH match refusing an abbreviation or a narrowing word (8), an event quote naming more than one disease (4), or the checker answering "unclear" (1).
    - *Gap partly closed (2026-11-12):* plain code now also refuses a disease the sentence writes straight before another MeSH disease term, up to the next punctuation ("Alzheimer's disease psychosis"). A following word that names no MeSH disease is left to the second reader: "Parkinson's disease agitation" passes plain code, because MeSH files agitation outside the disease trees, and the second reader refuses it (two planted cases). A word that names nothing ("expected", "trial") never narrows. The two indications the third run found still pass.
    - *Floors (set 2026-11-12, at the third run's scores):* `evals/indicate_catalysts/baseline.json` holds found_right at least 0.133, none_right at least 1.0, and wrong at most 0.0; `evals/check_indication/baseline.json` holds accepts_true at least 1.0 and refuses_false at least 0.962. Both files are owner paths in `scripts/merge_gate.py`. `indication_gate_problems` refuses to let the readers run on live data unless each folder's scores name the current prompts, role settings, and label sets, and meet these floors.
  - *Coverage:* abbreviations ("UC", "PsA") never match MeSH, so a release that writes the disease only that way gives no indication. Company pipeline pages (decision 5) remain the route to nearly every entry.
- **The reading job (`catalyst indicate-catalysts`, 2026-11-12):** for each published catalyst with one drug tested alone and no indication, plain code takes the source claim that states the event and its stored copy, and both readers run as above. A found indication is kept as a claim: its quote is the sentence, its value the MeSH identifier, the MeSH name, and the sentence's own words; its field is `indication:` and the catalyst's id, so two catalysts never share a claim row. The second reader's pass is a claim check beside it. Every try, found or refused, is a row of `indication_attempt`, so a re-run never pays to ask again about the same catalyst, claim, prompts, and models. A copy that the strict and browser-like readers read differently is refused without a model call (ADR 0044). The job asks nothing unless `indication_gate_problems` is empty, and a role at its cap stops it. It never writes a catalyst; the writer's pass does, in the next pull request. The writer reads indications only through `indication_attempt`, never by claim field, and uses only each catalyst's latest attempt (by `attempted_at`): a later refusal, an earlier run's disease redrawn included, overrides an earlier find (Opus review of PR 127). A copy refused because the two page readers differ is refused without a model call; it comes back only when a prompt or model version changes.
- **Its own gate:** a catalyst whose indication fails these checks publishes as it does today, without an indication. Only the indication waits in the review queue with its reason. No existing check is loosened. This touches the publish gate: test first, and Opus review.
- **Three pull requests:** the MeSH file and matcher (merged, PR 123); the two readers and their checks (PR 124); then the writer, with Opus review.

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
