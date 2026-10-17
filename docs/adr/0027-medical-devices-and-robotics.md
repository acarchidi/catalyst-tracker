# 0027 — Regulated medical devices and medical robotics across the whole platform

- **Status:** accepted (planned; nothing is built now)
- **Date:** 2026-10-17
- **Roadmap item:** Phase 8, "Medical devices and medical robotics"

## Context

On 2026-10-17 the owner asked for the whole platform to cover the regulated parts of the medical device and medical robotics industries.

Today's design is built around drugs:
- the catalyst types (PDUFA dates, CHMP opinions);
- the `drug` tables;
- the extractor's prompt and gold set;
- the rule checks.

**Devices follow different FDA pathways.** These are:
- 510(k) clearance, which shows substantial equivalence to a legally marketed device;
- premarket approval (PMA) for Class III devices;
- De Novo;
- advisory panels;
- Breakthrough Device designation;
- investigational device exemptions (IDE) for trials;
- payment decisions by CMS (Medicare coverage), which often matter as much as clearance.

**Free public data exists.** openFDA has device endpoints ([device APIs](https://open.fda.gov/apis/device/)) for:
- 510(k) and PMA;
- classification;
- recalls and enforcement;
- adverse events (MAUDE);
- the unique device identifier (UDI) database;
- registration and listing.

Device trials are already on ClinicalTrials.gov. The SEC classes device makers under industry codes 3841 (surgical and medical instruments), 3842 (orthopedic and prosthetic), 3845 (electromedical), and 3851 (ophthalmic goods) ([SEC SIC list](https://www.sec.gov/search-filings/standard-industrial-classification-sic-code-list)). Surgical-robot makers fall mostly under 3841 and 3845.

## Decision

1. **Products, not only drugs.** A catalyst links to products. A product is a drug (as today) or a device. Devices get their own tables:
   - **identifiers:** names (brand, model, and code name), FDA product codes, and 510(k), PMA, or De Novo numbers;
   - **device class:** I, II, or III;
   - **robotic flag:** whether it is a robotic system, so medical robotics can be filtered.

   This is a schema change, with reversible migrations. The data dictionary is regenerated, and an Opus review covers it.
2. **New catalyst types**, each with its own rule checks:
   - expected 510(k) clearance;
   - PMA decision;
   - De Novo decision;
   - FDA device advisory panel;
   - Breakthrough Device designation, from company releases;
   - IDE approval;
   - CMS national coverage decision, plus other payment pathways checked at build time;
   - CE mark under the EU's device regulation (MDR), with Phase 5's rules for sources outside the US.

   Device trial readouts use the existing readout type.

   The rule checks for device review times are written from FDA's published performance goals (MDUFA) at build time, the way the PDUFA check was written. Nothing is guessed.
3. **Sources:**
   - openFDA device endpoints, for clearance and approval decisions, which double as outcomes, plus recalls and adverse-event context;
   - Federal Register notices of device panels (through GovInfo, as now);
   - CMS's Medicare Coverage Database, with its terms checked first;
   - the FDA's De Novo database, with access checked first;
   - ClinicalTrials.gov device trials;
   - SEC filings and company releases (as now).

   Every source is rate-limited and follows robots.txt. openFDA works without a key at a lower daily limit. A free openFDA key raises the limit; that is the owner's decision when needed.
4. **Coverage:**
   - The coverage list adds companies under 3841, 3842, 3845, and 3851.
   - A list the owner reviews adds medical-robotics companies classed elsewhere.
   - Device makers whose main business is unregulated consumer goods are left out.
5. **Readers and scores:**
   - The extractor and the second reader get new prompt versions that know the device types.
   - The gold set gains device releases, and both readers are scored again before any device catalyst publishes. That is checkpoint CP11, and the floor rules are unchanged.
6. **Every feature follows:**
   - search and filters, with device class, pathway, and robotic flags;
   - timelines and pipeline views;
   - outcomes;
   - benchmarking, against predicate devices and competing systems;
   - market size, which uses procedure volumes;
   - scenarios;
   - insider purchases;
   - foreign listings.

   Each later feature adds the device case when it is built, rather than all at once.

## Alternatives

- **Devices as a separate product.** This would split the data and the subscription. The owner asked for the whole platform to cover devices.
- **Treat devices as drugs in the existing tables.** This is fast, but wrong: devices have no INN or generic names, they use other identifiers, and they follow other pathways. The rule checks would misfire.
- **Robotics only.** It is narrower, but robotic systems are regulated through the same device pathways. Covering devices covers robotics.

## Consequences

- It is a large change touching the schema, both prompts, the gold set, the rule checks, and the collectors. Phase 8 sits at the end of the roadmap, and the owner can move it earlier.
- No new account is required to start, only optionally a free openFDA key.
- Device approvals are public in openFDA, so device outcomes are cheaper to track than drug outcomes.
