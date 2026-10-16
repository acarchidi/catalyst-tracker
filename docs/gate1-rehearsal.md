# Gate 1 dress rehearsal, 2026-10-16

The full Gate 1 run, made against a local database and local copies because the live Supabase database does not exist yet (setup Part 3). The live run repeats these steps with `--snapshot-store supabase` (docs/gate1.md).

| Step | Result |
| --- | --- |
| Companies | 50: the 10 pilot companies plus 40 drawn with seed 2026 (`config/gate1_companies.toml`) |
| Collection | 148 seconds; 2,543 documents stored; all 50 companies linked to their documents; no source failed |
| Documents by source | ClinicalTrials.gov 2,304; SEC 174; FDA 38; PR Newswire 20; Federal Register 7 |
| Stored copies | 2,543 of 2,543 match their fingerprints (`catalyst snapshots-verify`) |
| Links | 2,536 of 2,543 worked on the first check (53 minutes). The 7 others were Federal Register notices, whose pages send scripted visitors to a human check; all 7 were then confirmed at their stored addresses through the Federal Register API (ADR 0010). 2,543 of 2,543 work. |

## Ten records to click (seed 2026, two from each source)

| Company | Source | Document | Published (UTC) | Link | Fingerprint |
| --- | --- | --- | --- | --- | --- |
| INCYTE CORP | ClinicalTrials.gov | Registry record | 2024-10-24 04:00 | [open](https://clinicaltrials.gov/study/NCT05593432) | 972190cb6ffc |
| (not linked to a company) | Federal Register | Federal Register notice | 2026-09-16 04:00 | [open](https://www.federalregister.gov/documents/2026/09/16/2026-18920/microbiology-devices-panel-of-the-medical-devices-advisory-committee-notice-of-meeting-establishment) | e5795b5a72f8 |
| INSMED Inc | FDA (Drugs@FDA via openFDA) | Drugs@FDA application NDA207356 | 2026-06-25 04:00 | [open](https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo=207356) | 444a0632719c |
| Protagonist Therapeutics, Inc | SEC EDGAR | 10-Q | 2026-08-05 20:17 | [open](https://www.sec.gov/Archives/edgar/data/1377121/000110465926091123/ptgx-20260630x10q.htm) | 0412f1696885 |
| (not linked to a company) | PR Newswire | Wire press release (feed item) | 2026-10-16 14:00 | [open](https://www.prnewswire.com/news-releases/zifo-recognized-by-nasscom-for-advancing-ai-driven-lab-transformation-302911239.html) | 9692f5a5a317 |
| ALNYLAM PHARMACEUTICALS, INC. | ClinicalTrials.gov | Registry record | 2025-05-07 04:00 | [open](https://clinicaltrials.gov/study/NCT05761301) | 9e37343507d0 |
| (not linked to a company) | Federal Register | Federal Register notice | 2026-09-25 04:00 | [open](https://www.federalregister.gov/documents/2026/09/25/2026-19700/dermatologic-and-ophthalmic-drugs-advisory-committee-notice-of-meeting-establishment-of-a-public) | ffea26bfd5f6 |
| ALNYLAM PHARMACEUTICALS, INC. | FDA (Drugs@FDA via openFDA) | Drugs@FDA application NDA214103 | 2025-04-04 04:00 | [open](https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo=214103) | d3116a65fa7e |
| INCYTE CORP | SEC EDGAR | 8-K EX-99.1 | 2026-07-07 00:06 | [open](https://www.sec.gov/Archives/edgar/data/879169/000087916926000048/ax2026skylineclosingxex99.htm) | d3ba66a2b5d2 |
| (not linked to a company) | PR Newswire | Wire press release (feed item) | 2026-10-16 14:01 | [open](https://www.prnewswire.com/news-releases/devonian-health-group-provides-update-on-its-planned-nyse-american-listing-302911326.html) | 35f87f20b5a6 |

Each link should open the exact document: a trial's registry page, an FDA application page, an SEC filing or exhibit, a Federal Register notice, or a press release. "(not linked to a company)" marks Federal Register and wire items, which the entity resolver (Phase 1) links to companies.
