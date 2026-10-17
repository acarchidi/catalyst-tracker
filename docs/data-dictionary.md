# Data dictionary

Generated from `pipeline/catalyst/schemas` by `uv run python -m catalyst.data_dictionary`.
Do not edit by hand. Every table has an `id` (internal identifier) as its first field.

## `source_document`

One primary document we retrieved, with a hash of the private copy we stored.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Internal identifier. |
| `source_key` | text, up to 300 characters | yes | The source's own identity for this document version, such as 'sec:<accession>:<file>'. |
| `url` | text | yes | Deep link to the exact document, for example an 8-K's Exhibit 99.1. |
| `publisher` | text, up to 200 characters | yes | Who published the document, for example 'SEC EDGAR'. |
| `source_tier` | one of: `regulator_or_sec`, `company_site`, `wire`, `registry`, `conference_or_journal` | yes | Rank in the provenance ranking; decides which claim wins when sources disagree. |
| `document_type` | text, up to 100 characters | yes | Form and exhibit type as the publisher labels it, for example '8-K EX-99.1'. |
| `published_at` | date and time (with time zone) | yes | When the publisher made the document public. |
| `retrieved_at` | date and time (with time zone) | yes | When we downloaded the copy that `sha256` describes. |
| `sha256` | text, up to 64 characters | yes | SHA-256 hash of the stored copy, lowercase hex. |
| `snapshot_key` | text | yes | Where the private copy is kept in the snapshot store. |
| `content_type` | text, up to 200 characters | yes | Media type the publisher sent, for example 'text/html'. |

Rules:

- Unique: `source_key`

## `company`

A US-listed company we cover, identified by its SEC number.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Internal identifier. |
| `cik` | whole number | yes | SEC Central Index Key; the company's permanent SEC number. |
| `name` | text, up to 300 characters | yes | Current legal name as filed with the SEC. |
| `sic_code` | text, up to 4 characters | no | SEC industry code, for example 2834 (pharmaceutical preparations). |

Rules:

- Unique: `cik`

## `company_document`

Links a source document to a company it concerns, and says how the link was made.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Internal identifier. |
| `company_id` | link to `company` | yes | The company. |
| `source_document_id` | link to `source_document` | yes | The document. |
| `matched_by` | one of: `sec_cik`, `ctgov_lead_sponsor`, `openfda_sponsor` | yes | How the link was made, for example by SEC number. |

Rules:

- Unique: `company_id`, `source_document_id`

## `company_ticker`

A ticker a company traded under, with the dates it was valid.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Internal identifier. |
| `company_id` | link to `company` | yes | The company. |
| `ticker` | text, up to 10 characters | yes | Ticker symbol, upper case, for example VRTX. |
| `exchange` | text, up to 20 characters | no | Listing exchange, for example Nasdaq. |
| `valid_from` | date | yes | First day the ticker applied. |
| `valid_to` | date | no | Last day the ticker applied; empty if still current. |

Rules:

- Unique: `company_id`, `ticker`, `valid_from`
- Database check `period`: `valid_to IS NULL OR valid_to >= valid_from`

## `company_name`

A current or former company name, so searches find old names.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Internal identifier. |
| `company_id` | link to `company` | yes | The company. |
| `name` | text, up to 300 characters | yes | The name as filed. |
| `valid_from` | date | no | First day the name applied, if known. |
| `valid_to` | date | no | Last day the name applied; empty if still current. |

Rules:

- Unique: `company_id`, `name`
- Database check `period`: `valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from`

## `drug`

A drug or program (asset).

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Internal identifier. |
| `company_id` | link to `company` | no | Company developing the drug, if one is known. |
| `preferred_name` | text, up to 300 characters | yes | Name shown to users: generic name if any, else code name. |
| `modality` | one of: `small_molecule`, `antibody`, `adc`, `bispecific`, `cell_therapy`, `gene_therapy`, `rnai`, `antisense`, `mrna`, `vaccine`, `radiopharmaceutical`, `peptide`, `other` | no | What kind of medicine it is. |
| `unii` | text, up to 10 characters | no | FDA Unique Ingredient Identifier. |
| `chembl_id` | text, up to 20 characters | no | ChEMBL compound identifier. |

Rules:

- Unique: `unii`
- Unique: `chembl_id`

## `drug_name`

One name for a drug: code name, generic (INN), or brand.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Internal identifier. |
| `drug_id` | link to `drug` | yes | The drug. |
| `name` | text, up to 300 characters | yes | The name as written by its source. |
| `kind` | one of: `code`, `inn`, `brand` | yes | Code name, generic (INN) name, or brand name. |

Rules:

- Unique: `drug_id`, `name`

## `target`

A biological target, identified by its HGNC gene symbol.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Internal identifier. |
| `hgnc_symbol` | text, up to 25 characters | yes | HGNC gene symbol, for example EGFR. |
| `name` | text, up to 300 characters | no | Full gene or protein name. |
| `chembl_target_id` | text, up to 20 characters | no | ChEMBL target identifier. |

Rules:

- Unique: `hgnc_symbol`

## `drug_target`

Links a drug to a target it acts on.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Internal identifier. |
| `drug_id` | link to `drug` | yes | The drug. |
| `target_id` | link to `target` | yes | The target. |
| `mechanism` | text, up to 300 characters | no | Mechanism of action, for example 'EGFR inhibitor'. |

Rules:

- Unique: `drug_id`, `target_id`

## `indication`

A disease, optionally narrowed by line of therapy and biomarker.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Internal identifier. |
| `mesh_id` | text, up to 10 characters | yes | MeSH identifier of the disease. |
| `name` | text, up to 300 characters | yes | Disease name shown to users. |
| `efo_id` | text, up to 40 characters | no | Experimental Factor Ontology (or MONDO) identifier. |
| `icd10_code` | text, up to 8 characters | no | ICD-10 code. |
| `line_of_therapy` | one of: `first_line`, `second_line`, `third_line_plus`, `maintenance`, `adjuvant`, `neoadjuvant` | no | Treatment line, if specified. |
| `biomarker` | text, up to 100 characters | no | Biomarker subgroup, for example 'EGFR exon 19'. |

Rules:

- Unique, counting empty as a value: `mesh_id`, `line_of_therapy`, `biomarker`

## `trial`

A clinical trial, identified by its ClinicalTrials.gov number.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Internal identifier. |
| `nct_id` | text, up to 11 characters | yes | ClinicalTrials.gov identifier, for example NCT04567890. |
| `eu_ct_number` | text, up to 17 characters | no | EU trial number (CTIS or EudraCT). |
| `study_name` | text, up to 300 characters | no | The company's name for the study, for example RAINIER. |
| `phase` | one of: `early_phase_1`, `phase_1`, `phase_1_2`, `phase_2`, `phase_2_3`, `phase_3`, `phase_4`, `not_applicable` | no | Trial phase as the registry reports it. |

Rules:

- Unique: `nct_id`

## `catalyst`

An expected event that can move a company's value, with its date window. These are the current values. Every change to them is kept in `catalyst_change`.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Internal identifier. |
| `catalyst_type` | one of: `pdufa_date`, `advisory_committee`, `topline_readout`, `conference_presentation`, `regulatory_submission`, `regulatory_acceptance`, `chmp_opinion`, `complete_response_letter`, `label_expansion`, `enrollment_completion`, `offering`, `shelf_filing`, `merger_acquisition`, `earnings` | yes | What kind of event this is. |
| `company_id` | link to `company` | yes | The company the event concerns. |
| `indication_id` | link to `indication` | no | The indication, if any. |
| `title` | text, up to 300 characters | yes | One-line description shown to users. |
| `window_start` | date | yes | Earliest day the event may happen. |
| `window_end` | date | yes | Latest day the event may happen. |
| `window_precision` | one of: `day`, `month`, `quarter`, `half`, `year` | yes | How precisely the date is known. |
| `window_raw_text` | text, up to 300 characters | yes | The date exactly as the source wrote it, for example '2H26'. |
| `date_confidence` | one of: `company_confirmed`, `regulator_scheduled`, `registry_estimate`, `inferred` | yes | Who stands behind the date. |
| `status` | one of: `upcoming`, `slipped`, `resolved` | yes | Upcoming, slipped, or resolved. |

Rules:

- Database check `window_order`: `window_start <= window_end`
- Database check `day_is_single_day`: `(window_precision = 'day') = (window_start = window_end)`

## `catalyst_trial`

Links a catalyst to a trial it concerns.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Internal identifier. |
| `catalyst_id` | link to `catalyst` | yes | The catalyst. |
| `trial_id` | link to `trial` | yes | The trial. |

Rules:

- Unique: `catalyst_id`, `trial_id`

## `claim`

One fact taken from one source document, with the exact words that state it (spec: Provenance).

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Internal identifier. |
| `source_document_id` | link to `source_document` | yes | The document the fact comes from. |
| `subject_type` | one of: `company`, `drug`, `target`, `indication`, `trial`, `catalyst`, `outcome` | yes | Which kind of record the fact is about. |
| `subject_id` | identifier | no | The record the fact is about; empty until entity resolution matches it. |
| `field` | text, up to 100 characters | yes | Which field the fact sets, for example 'window' or 'outcome'. |
| `value` | structured value (JSON) | yes | The extracted value, as JSON. |
| `quote` | text, up to 1000 characters | yes | Verbatim words from the source that state the fact. |
| `quote_start` | whole number | yes | Character offset where the quote starts in the stored copy's text. |
| `quote_end` | whole number | yes | Character offset just after the quote ends. |
| `agent` | text, up to 100 characters | yes | Agent role that extracted the fact, for example 'extractor'. |
| `prompt_id` | text, up to 100 characters | yes | Identifier of the prompt file used. |
| `prompt_version` | text, up to 100 characters | yes | Version of that prompt. |
| `provider` | text, up to 100 characters | yes | Model provider, for example 'anthropic'. |
| `model` | text, up to 100 characters | yes | Exact model identifier used. |
| `extracted_at` | date and time (with time zone) | yes | When the fact was extracted. |

Rules:

- Unique: `source_document_id`, `field`, `quote_start`, `quote_end`, `agent`, `prompt_id`, `prompt_version`, `model`
- Database check `forward_span`: `quote_start >= 0 AND quote_end > quote_start`
- Database check `span_matches_quote`: `quote_end - quote_start = length(quote)`
- Database check `quote_not_blank`: `length(btrim(quote)) > 0`
- Database check `has_value`: `value <> 'null'::jsonb`

## `catalyst_drug`

Links a catalyst to one drug it concerns, tested alone or in combination (ADR 0016). A catalyst may link many drugs, and one drug both alone and in combination. Each link names the claim whose quote states it.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Internal identifier. |
| `catalyst_id` | link to `catalyst` | yes | The catalyst. |
| `drug_id` | link to `drug` | yes | A drug the catalyst concerns. |
| `role` | one of: `alone`, `in_combination` | yes | Whether the drug is tested alone or in combination. |
| `claim_id` | link to `claim` | yes | The claim whose quote states this link. |

Rules:

- Unique: `catalyst_id`, `drug_id`, `role`

## `drug_component`

One part of a combination drug, recorded only where a source states it (ADR 0016).

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Internal identifier. |
| `combination_id` | link to `drug` | yes | The combination. |
| `component_id` | link to `drug` | yes | One drug in it. |
| `claim_id` | link to `claim` | yes | The claim whose quote names this part. |

Rules:

- Unique: `combination_id`, `component_id`
- Database check `not_itself`: `combination_id <> component_id`

## `catalyst_change`

One change to a catalyst field (spec: Change history). Append-only: rows are added, never edited or deleted. The database enforces this.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Internal identifier. |
| `catalyst_id` | link to `catalyst` | yes | The catalyst that changed. |
| `field` | one of: `catalyst_type`, `company_id`, `drugs`, `indication_id`, `title`, `window_start`, `window_end`, `window_precision`, `window_raw_text`, `date_confidence`, `status` | yes | Which catalyst field changed. |
| `old_value` | structured value (JSON) | no | Value before the change; empty for a first value. |
| `new_value` | structured value (JSON) | yes | Value after the change. |
| `claim_id` | link to `claim` | yes | The claim that caused the change. |
| `learned_at` | date and time (with time zone) | yes | When we learned of the change. |
| `is_correction` | yes or no | yes | True when the change fixes our own earlier error. |

Rules:

- Database check `changes_something`: `old_value IS DISTINCT FROM new_value`
- Database check `has_new_value`: `new_value <> 'null'::jsonb`

## `outcome`

What happened once a catalyst's date passed (spec: Outcomes). Price reaction arrives in Phase 3.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Internal identifier. |
| `catalyst_id` | link to `catalyst` | yes | The resolved catalyst. |
| `result` | one of: `approved`, `complete_response_letter`, `advisory_vote`, `endpoint_met`, `endpoint_missed`, `mixed`, `delayed`, `withdrawn` | yes | Result category. |
| `key_numbers` | structured value (JSON) | yes | Headline numbers by name, for example response rate, hazard ratio, p-value. |
| `votes_for` | whole number | no | Advisory committee votes in favor. |
| `votes_against` | whole number | no | Advisory committee votes against. |
| `resolution_claim_id` | link to `claim` | yes | The claim, with its source, that reports the result. |
| `resolved_on` | date | yes | Day the result became public. |

Rules:

- Unique: `catalyst_id`
- Database check `votes_present`: `result <> 'advisory_vote' OR (votes_for IS NOT NULL AND votes_against IS NOT NULL)`
- Database check `votes_not_negative`: `coalesce(votes_for, 0) >= 0 AND coalesce(votes_against, 0) >= 0`

## `model_call`

One model call and what it cost (spec: every model call logged with its cost). Daily spend caps are summed from this table, so every job shares one total per role.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | identifier | yes | Internal identifier. |
| `role` | text, up to 100 characters | yes | Agent role that made the call, for example 'extractor'. |
| `provider` | text, up to 100 characters | yes | Model provider, for example 'anthropic'. |
| `model` | text, up to 100 characters | yes | Model that answered. |
| `prompt_id` | text, up to 100 characters | yes | Prompt used. |
| `prompt_version` | text, up to 100 characters | yes | Version of that prompt. |
| `input_tokens` | whole number | yes | Uncached input tokens billed. |
| `output_tokens` | whole number | yes | Output tokens billed, including thinking. |
| `cache_read_tokens` | whole number | yes | Input tokens read from the prompt cache. |
| `cache_write_tokens` | whole number | yes | Input tokens written to the prompt cache. |
| `cost_usd` | decimal number | yes | Cost in US dollars at the prices in config/models.toml. |
| `latency_ms` | whole number | yes | Wall-clock time of the call, in milliseconds. |
| `request_id` | text, up to 200 characters | no | Provider's request id, for support. |
| `status` | one of: `ok`, `refused`, `truncated`, `invalid_output`, `model_switched`, `error` | yes | How the call ended. |
| `called_at` | date and time (with time zone) | yes | When the call started. |
| `config_version` | text, up to 100 characters | yes | Version of config/models.toml in force. |

Rules:

- Database check `counts_not_negative`: `input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0 AND cache_write_tokens >= 0 AND cost_usd >= 0 AND latency_ms >= 0`
