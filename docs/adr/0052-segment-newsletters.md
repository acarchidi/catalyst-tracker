# 0052 — Segment newsletters: longevity, gene editing and synthetic biology, augmentation, and neurotechnology

- **Status:** accepted as a plan (owner request 2026-11-10). The owner asked for the four segments, a monthly cadence, sale on their own or as an add-on, 1 to 3 papers of the month, and the month's collaborations and mergers. Everything else here is the coding agent's proposal, marked "(agent's proposal)" where it first appears; prices, names, and launch order stay with the owner
- **Date:** 2026-11-10
- **Roadmap item:** new Phase 3b, "Segment newsletters"

## Context

On 2026-11-10 the owner asked for specialized newsletters, sold as an add-on to the main subscription or on their own. Each covers one segment the owner cares about and goes out less often than the weekly digest. The four to start:

1. **Longevity and healthspan:** programs aimed at ageing itself or at extending healthy life, in clinical trials.
2. **Gene editing and synthetic biology.**
3. **Augmentation:** interventions meant only to improve the abilities of people who are already healthy.
4. **Brain–computer interfaces and neurotechnology.**

The owner set three content requirements. An issue may repeat what the main digest carries. It adds the 1 to 3 most important academic papers published in the month. It adds the month's most important collaborations and mergers and acquisitions in the segment.

Facts that shape the design:

- **The product's rule still applies.** Every published value has a source document and a verbatim quote (spec: Provenance). Recurring public text is computed by plain code, not drafted by a model (ADR 0047, decision 1). The owner approves each issue (ADR 0049).
- **Coverage today is drug developers only.** The list is US-listed companies in SEC industry codes 2834, 2836, and 8731 (ADR 0009). Most neurotechnology companies are device makers (Phase 8, ADR 0027). Many longevity, synthetic-biology, and neurotechnology companies are private, so they file nothing with the SEC.
- **There are no segment labels yet.** The data model has a controlled modality list, but nothing marks a catalyst as "longevity" or "brain–computer interface".
- **Paper ranking by citations does not work for new papers.** The planned "key publications" item (Phase 4) ranks by NIH iCite's Relative Citation Ratio. A paper published this month has almost no citations yet.
- **Corporate events are planned for Phase 10** (ADR 0030): collaborations and deals from filings, with quotes. Only PR Newswire among the wire services is reachable (ADR 0005).
- **Substack.** Sources say one account can hold several publications, each with its own subscribers and paid plan; whether Substack offers a bundle across publications was not confirmed (third-party sources only, checked 2026-11-10).
- **PubMed.** NCBI's E-utilities allow 3 requests a second without a key and 10 with one. Their terms for commercial use were not confirmed.

## Decision

### 1. What a segment issue holds

Each issue goes out once a month, on a fixed day: the first Monday, after the main digest (agent's proposal). It is built by the same plain code as the digest and approved by the owner. **Every entry is a published record:** a claim from a stored, hashed source copy, with its verbatim quote, checked and written by the publish gate like any catalyst (spec: Provenance; ADR 0047, decision 2). The builder reads only published records. Four parts:

1. **The segment calendar.** Catalysts in the segment in the next 90 days, dates that moved in the month, and outcomes posted in the month. Same rules and source links as the digest.
2. **Trials.** Trials in the segment that were newly registered, started, completed, or posted results on ClinicalTrials.gov during the month. Each entry gives the registry number, sponsor, phase, and status, as claims quoted from the stored registry record. This is a new collection scope: trials of any sponsor that match a segment's rules, not only covered companies' trials.
3. **Papers of the month (1 to 3).** See decision 3.
4. **Deals of the month.** See decision 4.

Each issue carries two notices, above and below its records: "Nothing here is financial advice" and "Nothing here is medical advice; no intervention is recommended for anyone". All four segments carry both, because all four report interventions.

**Paid only.** A segment issue is paid content, built from the paid shape. A segment publication's free plan is the waitlist and its About page; free subscribers get no issues. A reader who wants a free view gets the main free digest. A test proves the builder cannot produce a segment issue in the free shape. Checkpoint CP7b applies to each new publication: its first issue and its first paid post, including the owner's check that free subscribers and signed-out visitors see none of it.

### 2. Which records belong to a segment

- A segment is defined in an owner-reviewed file, `config/segments.toml`. Each segment lists the rules that place a record in it, from fields we already hold or will hold: MeSH indication terms, modality, target or mechanism, ClinicalTrials.gov condition and intervention terms, the registry's "accepts healthy volunteers" field, and named companies.
- Plain code applies the rules. Each record placed in a segment keeps the rule that placed it, so the owner can see why.
- A model may propose a segment for a record the rules miss. Like an uncertain name match (ADR 0015), the proposal waits for the owner and never places a record by itself.
- Starting rules, for the owner to correct:
  - **Longevity and healthspan:** MeSH "Aging" and "Longevity"; frailty and sarcopenia; mechanisms such as senolytics, mTOR inhibition, NAD+ precursors, and partial cellular reprogramming; trials of healthy older adults with an ageing endpoint.
  - **Gene editing and synthetic biology:** new modalities added to the controlled list (CRISPR nuclease, base editing, prime editing, epigenetic editing, RNA editing, engineered cell circuits, engineered microbes), plus the existing gene-therapy modality where the source says the product edits or rewrites a genome.
  - **Augmentation:** registry studies that accept healthy volunteers and whose stated purpose is to improve a normal ability (memory, attention, strength, endurance, sleep), excluding studies that only test safety in healthy volunteers before a disease trial.
  - **Brain–computer interfaces and neurotechnology:** implanted and non-invasive brain–computer interfaces, neurostimulation, and neuroprosthetics. Most of these are devices, so this segment depends on Phase 8.

### 3. Papers of the month

- **Source:** PubMed, through E-utilities. Each paper is a source document, stored with its hash like any other, and its printed facts are claims quoted from that record. Peer-reviewed journal articles only, as in the Phase 4 publications item (agent's proposal; the owner asked for "academic papers"). Preprints (bioRxiv, medRxiv) are left out by default. If the owner wants them, they appear in their own list, labelled "preprint, not peer reviewed".
- **Candidates:** articles published in the month that match the segment's MeSH terms or its title and abstract terms (new records often have no MeSH terms yet), or that report one of the segment's registered trials (the trial number is in the PubMed record).
- **Shortlist:** plain code ranks candidates by checkable facts: a clinical trial or a meta-analysis before other designs (PubMed publication types), a randomized design, the linked trial's registry enrollment where the paper names a trial, and whether the paper reports a trial on our calendar. PubMed has no participant-count field, so size counts only through a linked trial. Citation counts are not used, because new papers have none.
- **Choice:** the owner picks 1 to 3 from a shortlist of about 10 when approving the issue (agent's proposal). The analyst role may add a private note on each candidate. That call gets only the candidate's PubMed record, with no tools and no database access, as for every agent that reads outside text. The shortlist and notes reach the owner privately, never in a public log or file (ADR 0049, decision 3). The note is never published.
- **What is printed:** the title, journal, date, first author, publication type, the trial number if any, one short verbatim sentence from the abstract, and a link to PubMed and to the journal. Plain code picks the sentence: the first sentence of the abstract's conclusions section, else its last sentence. No abstract is reprinted in full and no summary is written by a model.

### 4. Deals of the month

- **What counts:** collaborations, licences, option deals, asset purchases, and mergers or acquisitions in which at least one party works in the segment.
- **Sources:** the party's own press release or SEC filing (for example an 8-K under Item 1.01 or 2.01), or a wire copy of the release. News is a lead, never a source (spec: Provenance).
- **How:** deals are corporate events. The extractor learns the deal type from a new prompt version, the gold set gains deal entries, both readers are scored again, and checkpoint CP13 applies before any deal publishes (quality-gates.md), as for the rest of Phase 10. Each deal is a claim checked and written by the publish gate.
- **What is printed:** the parties, the kind of deal, the terms as disclosed (upfront, milestones, royalties) or "terms not disclosed", the date, the quote, and the link.
- **Private companies:** included when one of the parties publishes the deal on a page or wire we can collect. Only PR Newswire among the wires is reachable (ADR 0005) and collectors fetch only listed hosts, so private-company deals will be few at first. A deal known only from news is left out.
- **"Most important":** plain code orders deals by disclosed value, then by whether a party is a covered public company. The owner may drop or reorder entries when approving the issue.
- This pulls part of Phase 10's corporate events forward, limited to deals in the four segments.

### 5. Packaging and price

- Each segment newsletter is its own Substack publication under the owner's account, with its own free and paid plans. A reader may take any of them without the main subscription.
- **Proposed by the coding agent, for the owner to set:** $10 a month or $100 a year for each segment newsletter on its own, and a lower add-on price for main subscribers once a bundle mechanism is confirmed. Monthly issues hold fewer records than the weekly digest, so the price rests on the papers and deals sections and the focus, not on volume.
- Until Substack bundles are confirmed, an "add-on" means a discount code given to main paid subscribers.

### 6. Launch order and a demand test first

- Each segment opens first as a free waitlist (a free Substack publication with an About page and no issues). A segment launches paid once it has at least 100 free sign-ups (agent's proposal; the owner may change the number). The About page makes no health claims.
- **Suggested order (agent's proposal):** gene editing and synthetic biology first (most public companies, trials, and deals today), then longevity, then neurotechnology (needs Phase 8 devices), then augmentation (fewest regulated trials; it may prove too thin for a paid monthly, and the waitlist will show it).
- **Owner time:** the owner asked on 2026-11-09 to spend no more than 2 hours a week on the business. Each issue needs about 20 to 30 minutes of review at first, mostly choosing papers, so four monthly issues add roughly 20 to 30 minutes a week. With the main digest and the review queue that would exceed 2 hours, so segments launch one at a time, and only once the main digest's weekly review takes the owner under 30 minutes (agent's proposal).
- **Model cost:** analyst notes on about 40 candidates a month across four segments, deal extraction, and segment proposals all run inside the existing daily role caps and the $50-a-month workspace limit (ADR 0051). The analyst's cap is $0.50 a day, so notes are spread over the month or limited to the shortlist.
- **Payment:** each new paid publication falls under ADR 0049, decision 5: the owner takes payment as an individual until the entity exists, and each widens the owner's personal liability and sales-tax exposure.

### 7. Rules carried over

- No advice of any kind: no buy or sell language, no price targets (scenarios excepted, ADR 0017), and nothing tailored to a reader.
- No medical advice: no dosing, no protocols, no suggestion that a reader try an intervention, and no self-experimentation or do-it-yourself case reports, even when published. Where an intervention is banned in sport (World Anti-Doping Agency list), the entry says so.
- No dual-use detail: synthetic-biology papers whose main content is a method of concern are left out of the shortlist.
- Respect each source's terms: PubMed's usage policy and rate limit, publishers' copyright (title, citation, link, and one short quote only), and robots.txt for company pages.

## Alternatives

- **One newsletter with sections for each segment.** Cheaper to run, but it cannot be sold separately, which is what the owner asked for.
- **A model writes a summary of each paper.** More readable, but it would be published text no quote supports, against ADR 0047. The owner's choice and a quoted sentence carry the same value.
- **Rank papers by Altmetric or social attention.** Those services need paid licences or accounts, and attention is not importance.
- **Launch all four paid at once.** It would exceed the owner's time and risks paid products with too little content, especially augmentation.

## Consequences

- New roadmap phase 3b with its items. It waits for the Phase 2 Substack items and CP7b, not for Phase 3 or Gate 4: nothing in it needs accounts or alerts. The first items (segment rules and waitlists) need no new account; papers need PubMed (free; an NCBI key is optional, and its commercial terms need checking first); deals pull part of Phase 10 forward, with CP13.
- New modalities in the controlled list need a reversible migration, a line in the spec's data model, and a new extractor prompt version with the gold set scored again.
- `config/segments.toml` joins the owner-reviewed files, so a change to a segment's rules is the owner's call.
- The spec's Offering gains a line for segment newsletters.
- Revisit when a segment's waitlist reaches its threshold, when Substack bundles are confirmed, and when Phase 8 devices land (neurotechnology).
