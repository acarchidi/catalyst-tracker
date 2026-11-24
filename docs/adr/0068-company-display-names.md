# 0068 — Company names as the company spells them

- **Status:** accepted
- **Date:** 2026-11-24
- **Roadmap item:** none; a fix to the weekly issue (owner request 2026-11-24)

## Context

The weekly issue shows each company's SEC conformed name. About 25 covered companies are in capitals there ("MACROGENICS INC", "INCYTE CORP", "PALISADE BIO, INC."), and some carry EDGAR's state suffix ("Viridian Therapeutics, Inc.\DE", "VERTEX PHARMACEUTICALS INC / MA"). Title case is no fix: it gives "Macrogenics" and "Biomarin", which are wrong. The spec leaves the shown name open.

## Decision

1. **The display name is the company's own spelling** of its name, as its own stored press releases write it (`catalyst.company_names`):
   - the releases read are the company's newest 3 stored 8-K Exhibit 99 press releases linked by its own SEC number. Filings' cover pages (all capitals), registry records, and documents linked by sponsor name are not read;
   - a spelling is a run of whole words that equals the SEC name after the resolver's normalization (case, punctuation, legal-form words: `normalize_name`), together with the legal form the text writes right after it (", Inc.", " Corporation", " Company N.V."). A trailing full stop is kept only after an abbreviation (Inc., Corp., Co., Ltd.);
   - it must start with a capital letter or a digit, and a spelling with no small letter (a headline in capitals) does not count;
   - a spelling with a legal form beats one without, then the most frequent wins, then the one seen first, newest release first;
   - with no spelling, the display name is the SEC name with only the state suffix removed ("\DE", " /DE/", " / MA", "/MD"). Nothing is title-cased.
2. **Stored on the company record, schema first.** Three columns, all empty until the first run: `display_name`; `display_name_document_id`, the release that spells it (its provenance); and `display_name_inputs`, a SHA-256 of this code's version, the SEC name, and the releases read. The migration is reversible and its downgrade is tested.
3. **Recomputed idempotently.** A run reads a company's releases only when that digest changed (a new release, a new SEC name, new code), as ADR 0064 does for checks; a re-run with nothing new reads and writes nothing. A copy that cannot be read keeps no digest, so it is tried again next run. The step runs free, last in `catalyst extract-catalysts` (inside the run's time budget; past it the rest wait a run), and on its own as `catalyst company-names`. No workflow changes.
4. **Shown everywhere a name is shown.** The read layer's company view keeps `name` (the SEC name) and adds `display_name`; the digest pages and the export's order use the display name. A company not computed yet, or an export written before this change, shows the SEC name without its suffix.

## Alternatives

- **Title case, or a hand list of spellings.** Title case is wrong for many names; a hand list goes stale and needs the owner.
- **Ask a model.** Costs money and adds a model's words to published data; the release text already holds the answer.
- **The name ClinicalTrials.gov registers.** Often a subsidiary or another legal entity, and not the company's own document.

## Consequences

- MacroGenics shows as "MacroGenics, Inc.", Vertex as "Vertex Pharmaceuticals Incorporated" (from its recorded release), Viridian as "Viridian Therapeutics, Inc."
- A company with no stored press release keeps its SEC name in capitals until one is stored.
- The first run reads up to 3 copies for each covered company (about 1,400 reads); later runs read only what changed.
- The display name always normalizes to the same name as the SEC name (a property test), so it can never name another company.
