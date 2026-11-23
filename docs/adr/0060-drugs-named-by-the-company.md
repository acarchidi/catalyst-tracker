# 0060 — A drug named by the company: its own document's name is enough until an official one exists

- **Status:** accepted
- **Date:** 2026-11-23
- **Roadmap item:** Phase 1, "Drug reference list" (owner decision, 2026-11-23)
- **Amends:** ADR 0015, part 4 (which unknown drug names the matching job may add) and ADR 0035, rule 9 ("A (B)")
- **Leaves unchanged:** ADR 0037 (another company's drug waits for review) and ADR 0035, rule 10 (a drug named only by registry words waits for review)

## Context

On 2026-11-23 the owner asked about the 646 catalysts held back from publishing: "why are all those catalysts held back, fix it and figure it out", and "maximize automation at all steps".

The largest single reason is `names_matched: not matched to exactly one record: drug unknown`. It is on 343 of the 646 held claims (198 distinct drug names). The drug list (ADR 0035) is built from Drugs@FDA and from each covered company's own ClinicalTrials.gov trials, and the matching job adds only a code name shaped like "SPY001" that the quote itself writes in capitals (ADR 0015, part 4). The names that still match nothing are mostly real:

- generic names of drugs that are not approved, so Drugs@FDA does not have them, and whose ending is not on the list of generic-name endings;
- code names in shapes the old rule does not take, or written only in the lines before the quote;
- brand names in mixed case, and names of cell therapies, gene therapies, and devices;
- one drug written by two names, "BRAND (generic name)" or "CODE-01 (generic name)";
- a strength or a form after the name, as in "X foam 0.3%";
- several drugs in one field: "X plus Y", "X with Y", "X and Y".

The owner decided on 2026-11-23: "we can use the code names until there are other and more official names, spec it out and implement it".

AGENTS.md requires tests first for entity matching, and that no check is weakened to release claims.

## Decision

1. **A drug named by the company.** When a claim's drug name matches no record, the matching job (`catalyst.resolve_claims`) adds a drug for the claim's company, in plain code and with no model call, when all of these hold:
   - the claim's document is linked to exactly one company;
   - the name is a drug's name by the rules in part 2;
   - nothing else on file goes by it (part 2);
   - the stored copy of that document names the drug at the claim's quote. This is the evidence of the existing check `drug_named_near_quote` (ADR 0018, part 3e), run by the same code and unchanged: the name is in the quote, or in the 1,000 characters before it with no other drug of the company or of the release named after it, and the quote is at its stored place. The copy must also write the name somewhere as a whole word that is not joined to more by a hyphen ("QRX-12" is not found in "QRX-12-B").

   The new drug belongs to that company. Each new name is stored with source `claim` ("found in a claim's document"), and with the claim as its record (`source_refs` holds `claim:<claim id>`), so the claim and through it the document are its provenance. The claim's match is marked `company_named`, and keeps that mark while that drug is the match. A drug whose names all have source `claim` is a provisional record: only the company has named it so far. No table or column changes, so there is no migration.

   The rule needs the stored copies, so it runs where they are open: in the extraction job (`catalyst extract-catalysts`), which matches every claim again on every run. `catalyst resolve-claims` alone matches without it, as before.

2. **What never becomes a drug.** Each case has a test that shows the claim stays held.
   - **Words that name no drug** (`catalyst.company_drugs`). The claim's drug words are read by the drug list's own rules for a registry name (ADR 0035, rule 3: stand-ins such as "placebo", descriptions such as "cell therapy" or "inhibitor", ordinary words, trial names, targets, isotopes, short forms of fewer than five letters without a digit, more than six words), and by four more:
     - a word of a phrase (`CLAIM_STOP_WORDS`: "our", "lead", "program", "candidate", "combination", "pipeline", "platform", "novel", "molecule", and others), so "the drug", "our lead program", and "the combination" name nothing;
     - a date: a year, a half or a quarter, or a month ("QRX-2027", "2H26", "January");
     - a congress, agency, filing, or advice word before a number ("ASCO 2027", "FDA 2027", "BUY 100"), the list ADR 0015 already uses;
     - more than 60 characters in one name, or more than eight drugs in one field.
   - **A name on file elsewhere.** A name of another company's drug, or of two records, is ambiguous as before (ADR 0015, ADR 0037) and never reaches this rule. A name the drug list withheld because two companies' records give it (ADR 0035, rule 4) stays held. So does a name that is a study name, a company name, or a ticker on file.
   - **A name the document does not write at the quote**, or a stored copy that cannot be read.
   - **A document linked to no company or to several.**
3. **Name forms.**
   - A strength, route, or form at either end is not part of the name: "X foam 0.3%", "X 25 mg tablets", and "subcutaneous X" are X. A few form words are read for claims only (`CLAIM_FORM_WORDS`: "foam", "film", "sublingual", "nebulized", and others). The same reading finds a drug already on file, so "X foam 0.3%" matches the company's X.
   - "BRAND (generic)" or "CODE (generic)" is one drug with both names. This amends ADR 0035, rule 9, which left the whole unknown when one part matched nothing. The two names are joined only when the stored copy writes them together, one in a bracket right after the other, since only that says they are one drug. The drug is shown by its generic name if it has one, else its brand, else its code (spec, Data model).
   - A list or a combination names each drug: "X plus Y", "X + Y", "X with Y", "X in combination with Y", "X and Y", "X, Y and Z". Each drug gets its own match, and the claim's catalyst is linked to each through the existing `catalyst_drug` table (ADR 0016). A slash ("X/Y"), "or", a bracket inside a combination, or any part that names no drug leaves the whole unknown.
   - Two checks are tightened for such claims, so the new matches cannot publish more than the quote says:
     - `drug_named_near_quote`: a claim matched to several drugs passes only when each of them is named. Before, one matched drug's name was enough, and no claim had more than one.
     - The drug's role (`catalyst.publish.sanity.drug_role`): words that combine drugs ("plus", "+", "with") give `in_combination` only when the quote joins two of them the same way and does not say "alone"; they never give `alone`. A list ("and", a comma) gives `alone` for each only when the quote speaks of no combination. Anything else waits for a person, as an unclear role always has.
4. **Until there are more official names.**
   - **A later drug-list run.** When Drugs@FDA or the registry later gives a drug of the same company one of the provisional record's names, the existing rule joins them: the official names are added to the record on file, no second drug is made, and Drugs@FDA's generic name is shown from then on (ADR 0035, rule 5). A test shows this for a company-named drug, and that a second run adds nothing.
   - **A later document of the company.** When a claim's words give two names, one on file for the company's drug and one new ("generic (CODE)"), and the copy writes them together, the new name is added to that drug. A drug shown by a name a claim gave is then shown by the better name (generic, else brand).
   - **Two records of one drug.** When the two names belong to two of the company's records and the copy writes them together, the records are joined into one if one of them is spare: named only by claims, with no catalyst, combination, or target linked to it. Its names and its claims' matches move to the record that stays, so no published row changes. Otherwise the claim stays ambiguous for a person.
   - Re-running adds nothing.
5. **Held claims are released without a new reading.** `resolve_claims` already matches every extractor claim on every run, and the checks and the publish decisions that follow it in the extraction job are computed again each run. So the next run applies this rule to the claims held today. A test shows a claim held before the rule passing `names_matched` after the next run, with no new claim.
6. **"Drug ambiguous" is left as it is.** 43 held claims carry it. They are of two kinds: a generic or brand name whose drug another company develops (ADR 0037), and a drug named only by registry words (ADR 0035, rule 10). Neither is the same drug filed twice under two spellings. The second kind could be released by the evidence this rule uses (the company's own document names the drug at the quote), but since the gate already requires that evidence, doing so would simply retire rule 10, which a review asked for. That is a decision for the owner, not a matching rule.

## Measurement

Offline, on the private export of 2026-11-23, with this pull request's name rules and the export's quote and paragraph (the export holds no full copies, so the look-back is only as long as the paragraph). Counts only; nothing from the export is in the repository.

- Of the 343 held claims with `drug unknown`:
  - 142 would match: their drug is a name by the rules and is written in the quote or its paragraph (120 plain names, 10 with a strength or form removed, 6 written by two names, 6 lists or combinations);
  - 99 more would probably match: a plain name not in the paragraph, on a claim whose `drug_named_near_quote` already passes on the full copy;
  - 41 depend on the full copy: 13 two-name claims with one name at the quote, whose pair must be written together somewhere in the copy, and 28 whose names are not in the paragraph;
  - 44 are not names by the rules and stay held: descriptions and programmes, names with "cell therapy" or "therapy", names joined by a slash, codes with a decimal point or a year, short forms, a combination with a bracket;
  - 17 plain names are not written at the quote and stay held.
- Of the 241 that would or probably would match, 122 would have no other reason left.
- Names withheld as shared, and study or company names, could not be counted offline; they lower these numbers a little.

## Alternatives

- **Only code names, as the owner's words say literally.** About 60 claims. The same evidence (the company's own document writes the name at the quote) holds for a generic or brand name, and most held names are those.
- **Require a generic-name ending or capitals for a name without a digit**, as the drug list does for registry words (ADR 0035, fifth review). The registry's main-name field is free text that often holds a class or a disease. A claim's drug field is the extractor's reading of one drug, the second reading must agree with it, and the quote must name it. The ending list would hold real names such as those ADR 0035 lists as lost.
- **A model decides whether a name is a drug.** It costs a call per name and cannot be pinned by a test (ADR 0015).
- **A new `named_by_company` column, or a new name source.** The existing source `claim` already means "found in a claim's document", and the claim reference is the provenance. A column would need a migration and say nothing more.
- **Drop "Cell Therapy" and similar words from the end of a name.** It would release about 8 claims, but then an ordinary adjective before those words ("engineered cell therapy") would become a drug.
- **Join two records whenever a claim's words give both names.** Without the copy writing them together it could join two different drugs, and joining a record that a published catalyst links to would change published rows from the matching job, which only the publish gate may write.
- **Release "drug ambiguous" claims on the same evidence.** See decision 6.

## Consequences

- A claim through this path still faces every check of the gate. `names_matched` and `drug_named_near_quote` are both required, so a drug the document does not name at the quote cannot publish.
- A release can name a partner's or another company's drug that no list has. This rule then files that drug under the release's company. The catalyst's words and dates are still the release's own, but the drug's owner on file is then wrong until an official list gives the name. A name that Drugs@FDA, the registry, or the CBER list gives another company never gets this far (ADR 0035, ADR 0037).
- A non-drug name that the lists do not catch (a platform or a programme named like a product) can become a drug record if the extractor reads it as the claim's drug and the document writes it at the quote. The weekly sample and the review of new stop words are how we would notice.
- A provisional name of one company makes another company's claim of the same generic or brand name wait for review (ADR 0037), and makes a later drug-list record of that name for another company wait as shared (ADR 0035, rule 4).
- Two provisional records of one drug stay apart until a claim's words give both names and the copy writes them together. A drug-list run that would join two of a company's records still leaves them for a person (ADR 0035, rule 5).
- In one run, claims are matched in turn, so a claim read before the claim that names its drug can wait one more run.
- The matching step of the extraction job now reads stored copies: one read for each document that has a claim with an unmatched, nameable drug. After the first run that is a small number.
- Names with "cell therapy", a slash, a decimal point, or a year in the code stay held, which is the safe direction.
