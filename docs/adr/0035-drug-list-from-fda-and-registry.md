# 0035 — A drug list from Drugs@FDA and ClinicalTrials.gov; another company's drug waits for review

- **Status:** accepted
- **Date:** 2026-10-19
- **Roadmap item:** none; fixes the top hold reason of the live run of 2026-10-18 (Phase 1, "Entity resolver and reference tables", is ticked)

## Context

The live run of 2026-10-18 (466 companies, 878 press releases) held 374 of 412 claims for review. The most common reason, on 313 claims, was `names_matched: not matched to exactly one record: drug unknown`. Drug records came only from a company's own quoted code name (ADR 0015, part 4), so generic and brand names ("mavorixafor", "LIVMARLI", "ELEVIDYS") never matched.

ADR 0015 (part 3) left registry intervention lists out of the drug tables. Insmed's records list a comparator (azithromycin), placebos ("ELC (matching placebo for ALIS)"), and short forms ("ETH"), any of which would match the wrong drug. That reason stands; this ADR answers it with rules, not by trusting the lists.

The CP4 review (finding O1) also noted that a generic or brand name matched for any company. A release that names a partner's or a competitor's drug could then link that drug to the release's company. More drug names on file make that likelier.

## Decision

1. **Two public sources, no key, no account.**
   - **Drugs@FDA through openFDA** (`api.fda.gov/drug/drugsfda.json`): every NDA and BLA, about 6,400 records in seven pages of 1,000.
     - An application counts only if one of its submissions is approved. Generic copies (ANDA) and tentative approvals name no drug of their sponsor.
     - Each product gives its brand name, and, when it has one active ingredient, that ingredient's generic name.
     - The FDA spelling is cleaned. The biologic suffix goes ("DOSTARLIMAB-GXLY" is dostarlimab), and so do trailing salt and water words ("RUXOLITINIB PHOSPHATE"). Esters stay ("FLUTICASONE PROPIONATE", and the fumarate esters "DIMETHYL FUMARATE" and "DIROXIMEL FUMARATE"), and so does a metal written first ("SODIUM OXYBATE").
     - A combination product ("ALYFTREK", "VYTORIN") gives only its brand; its ingredients are kept only for the comparator rule below.
   - **ClinicalTrials.gov** (API v2): for every registered sponsor name on the company list, one search on the lead sponsor, keeping only trials whose lead sponsor is that name exactly.
     - The search is the name's first six words as a phrase, since the API refuses longer phrases and reads brackets and "&" as syntax.
     - That is about 550 requests at 45 a minute (ADR 0005), so about 13 minutes.
     - Interventions of type drug, biological, or genetic are read.
2. **Sponsor restriction.** A record names a drug of a covered company only when its sponsor is exactly one covered company:
   - registry: the company list's registered lead-sponsor names, exactly, as the collector already links trials (ADR 0009);
   - FDA: the sponsor matches a company's current or former name, or a hand-checked FDA name on the list. The comparison is the resolver's company rule (ADR 0015: case, punctuation, and legal-form words aside), with FDA's abbreviations spelled out (PHARMS, PHARM, THERAPS, LABS);
   - FDA also cuts names short ("KURA", "MIRUM"). Such a name stands for a company only when every label on the application names that one company in full ("Kura Oncology, Inc.") and the sponsor's words begin that name. A partial name alone never matches;
   - one hand-checked spelling fix: FDA files LYNOZYFIC (BLA761400) under "RENGENERON PHARMACEUTICALS, INC.", labelled by Regeneron; "RENGENERON" is read as "REGENERON";
   - a sponsor that would name two companies names none.
3. **Registry names** (the answer to ADR 0015, part 3).
   - **An intervention needs a main name that names a drug.** Without one ("Non-Interventional Study", "Noninterventional"), it gives nothing, not even its other names. Its listed other names, and text in brackets, count as names too (owner decision 2026-10-19), held to more rules, and never shown (rule 5).
   - **An intervention that is another sponsor's approved drug gives nothing.** That is when Drugs@FDA lists its main name, or a run of its words, for another sponsor ("Nivolumab", with Candel's other names ONO-4538 and BMS-936558), or lists one of its names as another sponsor's brand ("Gazyvaro" with "Gazyva", Genentech's). Before the second review, its codes stayed with the trial's sponsor and made 246 drugs of other companies' approved drugs. A molecule another sponsor sells, listed beside the sponsor's own code ("AP01" with "Pirfenidone"), takes only itself out: the code may be a new form of it.
   - A name is dropped when:
     - the intervention is given only in comparator arms (placebo, active, or sham comparator, no intervention);
     - it names a stand-in: placebo, vehicle, sham, saline, standard of care, chemotherapy, investigator's or physician's choice, background or rescue medication;
     - it joins drugs ("+", "/", ",", "and", "plus", "with", "in combination"), inside brackets too ("simvastatin (+) ezetimibe", "ezetimibe (plus simvastatin)");
     - it is a description, not a name: more than six words (four for an other name), a colon, or a description word ("cells", "antibody", "therapy", "cohort", "extension", "pharmacokinetics", "noninterventional", "lymphoma", "radioligand"); the lists are in `catalyst.drug_list_words`;
     - it holds an ordinary word as a whole word ("in", "of", "if", "until", "as", "brand", "name", "commercial", "concentration", "administration"), or it is only salt or metal words ("fumarate");
     - it names a trial ("KEYNOTE-D79", "CheckMate-067");
     - it is a short form: no digit and fewer than five letters ("ETH", "ATEV");
     - it is a code that is not a drug: fewer than four letters and digits ("14C"); a disease, congress, or year prefix (as in ADR 0015, part 4); a target, receptor, antibody class, or virus ("CTLA-4", "PI3K", "IgG1", "B7-H4", "LPA1", "ILT4", "5-HT3 RA", "CD19", "IL-17A", "HER2", "HPV16", "BCMAxCD3"); an isotope ("131I", "64Cu", "Lu-177"); an insulin strength ("U100"); a variant ("B.1.1.7"); a mutation ("G12C", "V600E"); an excipient or adjuvant ("PS80", "QS-21", "AS01B", "MF59");
     - as an other name, it is a code not shaped like a drug code. The shape is allowed, not denied: a letter prefix (up to six characters, with one dash-joined part allowed: "Cu-AKY", "GR-MD"), then two to seven digits (four or more after a one-letter prefix: "R115777", not "U100" or "C225"), then up to two short parts ("ENTR-601-45"). "REGN5458", "GS-5423", "SCH 58235", and "X4P-001" pass;
     - **Drugs@FDA lists it, or any run of four or more letters of its words, for another sponsor.** Every approved comparator or partner is in Drugs@FDA, so azithromycin (Pfizer's ZITHROMAX), venetoclax, and "Dexamethasone Ophthalmic" never become a trial sponsor's drug;
     - another intervention of the same trial also lists it (NCT07683000 lists teropavimab's code GS-5423 under "Lenacapavir Tablet" too);
     - **records of the company with different main names list it** (second review, B1'): Aktis's two imaging agents both list "[64Cu]", and Regeneron's linvoseltamab and vonsetamig both list "BCMAxCD3". Such a name says what the drugs share, not which drug it is. An other name that is itself another record's main name ("INS1007" under brensocatib, and a trial of "INS1007") still joins the two, when only one main name lists it.
   - Dose and form words at the ends go ("Brensocatib 25 mg"), but a bare number after a code's letters stays ("SCH 58235", "BMN 333"). A name with a digit is a code name, which matches only its own company's claims (ADR 0015). Any other name is a generic name, or a brand when the registry marks it ® or ™ or writes it in capitals ("ELEVIDYS").
4. **Ambiguity.**
   - After these rules, a name that two owners' records give is recorded for neither. Owners are covered companies, and also any FDA sponsor we do not cover. Such a name is stored in `withheld_drug_name`, with its companies and records, for review. Examples: cemdisiran (Alnylam's and Regeneron's trials), danicamtiv (Bristol Myers Squibb and Kardigan), bevacizumab (several sponsors).
   - This holds for code names too. Code names match only their own company's claims (ADR 0015), but a code two companies' records give is usually one partnered drug, and recording it for both would make two records of it.
   - Against drugs already on file, any name another company's drug already has is withheld, a code name too.
   - An intervention whose main name is shared gives nothing: its other names alone would make a drug that only they name.
5. **One drug per company's joined names.**
   - Names that one record gives together join into one drug: an intervention's names, or a product's brand and generic name.
   - A registry record that joins two Drugs@FDA products is left out. The FDA products are told apart by their one ingredient, or, for a combination, by its brand, so VYTORIN never joins ZETIA.
   - A planned drug joins the company's one drug on file that shares a name, such as a code-name drug the matching job created. When it shares names with two or more of the company's drugs, it waits for a person.
   - **The name shown** (`preferred_name`, which the read API and titles show) is Drugs@FDA's first, its generic name (in small letters) before its brand. Else it is a registry main name, a generic name before a brand or a code, and among those the one most records give as their main name, so one trial's spelling ("Iza-bren") does not win by the alphabet. It is never a registry other name. When a drug on file gains an FDA generic name, that name is shown from then on, as the spec asks ("generic name if any").
   - A metal salt keeps its whole generic name ("FERRIC CITRATE", "POTASSIUM CHLORIDE"), as a fumarate ester does: the metal alone names no drug.
6. **Provenance.** `drug_name` gains `source` (`claim`, `openfda`, or `ctgov`: where the name was first found) and `source_refs` (every FDA application or registry number that gives it). The reversible migration marks the names already stored as `claim`; its downgrade is tested. After a downgrade and a new upgrade, every name then on file, loaded ones too, is marked `claim` with no records, until the next drug-list run fills in its records again. Nothing is deleted when a source stops listing a name.
7. **One run, all or nothing.**
   - `catalyst load-drugs [--companies config/coverage_companies.toml] [--sources openfda,ctgov]` and the `drugs` source of `catalyst collect` both load the list. The `drugs` source runs only when named, so `Collect (live)` with sources `drugs` loads the list with no workflow change.
   - They fetch everything first, then write in one transaction. A failed request stores nothing, because a list built from part of the records could hand a shared name to one company.
   - Registry names need Drugs@FDA (for the comparator rule), so `ctgov` alone is refused.
   - A run of both sources replaces the withheld names. A run of `openfda` alone keeps the withheld names it did not read again and adds its own.
   - Re-running adds nothing. Only companies already collected get drugs.
8. **Another company's drug waits for review** (CP4 review, O1).
   - In the matching job (`known_drug`), a generic or brand name whose one drug another company develops is ambiguous, with that drug as the candidate, so `names_matched` holds the claim.
   - A drug with no known company still matches. A name of two drugs, one of them the company's own, stays ambiguous, as before.
   - So a co-owned or licensed drug matches only when its name is not shared and belongs to the claim's company.
9. **"A (B)".** A claim's drug written as two names ("TRYNGOLZA (olezarsen)") resolves only when both names match the same one drug. Names that match two drugs are ambiguous; a part that matches nothing leaves the whole unknown.

## Measurement

This is an offline measurement, not committed. It used recorded copies of Drugs@FDA (2026-10-16 data) and of every covered sponsor's trials (2026-10-19, 524 sponsor names), the resolver's own code, and no model, database, or live data. It ran against the drug names of the 2026-10-18 export, after the second review's fixes.

- Of the 315 held claims whose reasons include `drug unknown`:
  - 156 now match exactly one drug (97 distinct company and drug pairs);
  - 5 are newly held because the drug is another company's: Ionis's eplontersen and bepirovirsen (AstraZeneca's and GSK's), LeonaBio's lasofoxifene (Ligand's), and Summit's sonesitatug vedotin (AstraZeneca's);
  - 154 stay unknown. These include descriptions such as "CardiAMP Cell Therapy", lists such as "SPY001, SPY002, SPY003", names no source gives such as "miv-cel", and shared names.
- **Wrong matches: 0, by the matched drug and by the name shown.** Every pair that changed since the first review was checked by hand against the claim's drug, its quote, and the name shown. That is 9 pairs: 8 newly matched (cema-cel, BMN 333, rese-cel, seralutinib, Symvess, rinvatercept, ATR 1072, azer-cel) and 1 shown differently (aglatimagene besadenovec is now shown as "AdV-tk", its main name in more of Candel's trials). The other 89 pairs were checked before.
  - Some shown names are codes or descriptions rather than generic names: rese-cel as "CABA-201", seralutinib as "GB002", Symvess as "Acellular Tissue Engineered Vessel". Each is the company's own main name for the drug.
  - One is imprecise: ARIKAYCE is shown as "amikacin", Drugs@FDA's generic name for its one ingredient.
- The 48 held claims and the 36 published drug links that matched before all still match the same drug. The ownership rule holds none of them.
- The drug check on the quote alone (`drug_named_near_quote` without the text before the quote, which the export lacks) gives the same outcome for all 363 held claims with a drug.
- The second review's probes now fail as they must:
  - a YERVOY claim on a quote about another anti-CTLA-4 antibody;
  - paxalisib on a PI3K sentence;
  - ANX009 on a sentence about an IgG1 antibody;
  - Cu-AKY-2519 on a Cu-AKY-1189 sentence;
  - linvoseltamab on a vonsetamig sentence.
- None of ONO-4538, OSI-774, PLX3397, ASP-2215, GA101, RAD1901, CPT-11, Gazyvaro, or KEYNOTE-D79 is now its trial sponsor's drug.
- Of the 312 drugs the second review's plan built only from other names (the review counted 246), none is built so now. Every drug has a main name. Five codes are still on file for the same company, each now beside its main name and each the company's own drug: SYR-322 (Takeda's alogliptin), AVE5026 and SSR126517 (Sanofi), CNTO148 (Janssen's golimumab), and MDX-1106-04 (Bristol Myers Squibb).
- The code shape keeps 2,114 of the 2,506 distinct other-name codes the main-name rule would keep, and drops 392 (16%). Most of those are not drugs ("Cohort1", "FOLFOX 7", "13vPnC", "PSMAxCD28", "Humalog Mix50", "1-7F9"). Some real codes go too, such as Merck's one-letter vaccine codes ("V260", "V441") and "ABBV-8E12". That is the safe direction.
- Every one of the 361 drugs with an FDA generic name is shown by that name. Lenacapavir and teropavimab are two drugs, and so are ZETIA (ezetimibe) and VYTORIN. BRILINTA is shown as ticagrelor. No drug is shown by a registry other name.
- The list holds about 6,980 drugs and 9,090 names: 5,620 registry code names, 2,290 registry generic or brand names (460 of them other names), 820 FDA brands, and 360 FDA generic names. 600 names are withheld as shared.

## Alternatives

- **Registry intervention lists as they are.** These are ADR 0015's comparators, placebos, and short forms. The measurement found more than 18,000 registry names that Drugs@FDA lists for another sponsor.
- **Registry other names as code names only.** The first review's fix kept only their code names, because junk among them ("concentration", "if necessary", "Commercial") made `drug_named_near_quote` pass a quote that does not name the drug. That gave up 15 claims (cema-cel, seralutinib, and others). The owner decided on 2026-10-19 to keep other names, filtered further: more words, the code shape, the bridge rule, and never shown.
- **A deny list of target and isotope codes only.** It misses every new target spelling. The second review asked for an allowed shape first, and the deny list stays as a second net for main names, whose shape is the sponsor's own.
- **Drugs@FDA only.** It is safe, but it misses every investigational drug, which most held claims name.
- **A model to classify names.** It costs a call per name and cannot be pinned by a test. ADR 0015 keeps models out of matching.
- **The openFDA bulk file** (one 9 MB download) instead of the API. It holds the same records, but the API's seven pages are lighter to parse and stay within the documented, key-less limits.
- **Fuzzy matching of FDA sponsor names** ("KURA" to any company starting "Kura"). It is a partial match (ADR 0015). The label check confirms a cut name with FDA's own full name instead.
- **A dictionary of English words, or WHO INN stems, to drop common words.** No such list ships with the project. A short list of ordinary and description words, the five-letter floor, the comparator rule, the main-name rule, and the ownership rule (8) cover the cases found.

## Consequences

- Half of the `drug unknown` holds clear, with no wrong match found. Those claims still face every other check.
- `drug_named_near_quote` (ADR 0018, part 3e) now sees many more of a company's other drugs. That is the safe direction: a correct claim whose quote relies on the text before it can be held if another of the company's drugs is named in between. The check also reads every name of the claim's own drug, other names included, so a junk other name could still make a quote seem to name a drug. The filters above, and the probes in the tests, are what stop that; a junk name that slips through can only help a claim of the drug it is filed under. The planted-error test still runs on its production-like tables (gold code names only).
- Some registry main names are imperfect labels (a brand written in small letters is stored as a generic name). Both kinds match the same way; only `matched_by` differs.
- Description-like names can remain among about 2,290 registry generic or brand names of large companies ("Prandial Insulin", "Human Acellular Vessel", "Lambda"). Each matches only as a whole name. Another company's claim that names it waits for review (rule 8).
- Known gaps:
  - FDA sponsors named in other forms ("GLAXOSMITHKLINE" for GSK plc, "TAKEDA PHARMS AM") match no company, so their products belong to no one here. That is safe, because the comparator rule still keeps them from others. Hand-checked FDA names in `config/coverage_overrides.toml` would fix it.
  - CBER products (cell and gene therapies, vaccines) are not in Drugs@FDA; they come from the registry only.
  - The resolver's `name_key` turns "™" into "TM" (Unicode NFKC), so a claim name with ™ does not match the same name without it. This list strips the marks first.
- A shared name stays with the company that had it first, if it was on file before the second company's records appeared. The withheld list shows the newcomer for review.
