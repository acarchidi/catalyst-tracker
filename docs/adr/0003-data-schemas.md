# 0003 — Data schemas: tables generated from the models

- **Status:** accepted
- **Date:** 2026-10-16
- **Roadmap item:** Data schemas (checkpoint CP2)

## Context

The spec's Data model lists ten entities and says tables, API specs, and documentation are generated from one set of schemas. ADR 0001 deferred table generation to this item. The spec leaves the storage details open.

## Decision

1. **Generation.** `catalyst.db.tables` builds every table from the Pydantic models in `catalyst.schemas`. Each field supplies its column's type, NOT NULL, text length limit, allowed values, foreign key, and comment. A model declares only extra unique groups and SQL checks. The generator refuses any type it does not know, so a new type cannot silently become JSON.
2. **Controlled lists** (modality, catalyst type, date precision, and others) are stored as text with a database check on the allowed values, not as Postgres enum types. Adding a value is a one-line migration; Postgres enums cannot drop values.
3. **Current values plus history.** `catalyst` holds current values. Every change goes to `catalyst_change` (old value, new value, the claim that caused it, when we learned it, and whether it was a correction). A database trigger refuses UPDATE and DELETE on `catalyst_change`. TRUNCATE is not blocked; only the database owner can run it, and production data is never edited by hand (AGENTS.md).
4. **Rules in two places.** Every rule that can be checked in SQL (date window order, day precision means one day, non-blank quotes, forward quote spans, a change must change something, advisory votes present) is enforced by Pydantic and again by the database. A code bug or a manual fix cannot store a value the models reject.
5. **Date windows.** `day` precision means start equals end; every wider precision means start is before end. Windows are not forced onto calendar boundaries, because phrases like "by year-end" or "mid-2027" do not align to them.
6. **Claims.** A claim stores its quote (at most 1,000 characters, so a quote verifies a fact and never republishes a release), its character span in the stored copy's text, and the agent, prompt version, provider, and model that produced it. `subject_id` stays empty until entity resolution matches the claim to a record.
7. **Deferred to the items that need them.** The outcome's price reaction waits for the Phase 3 price licence. Review-queue status lives with the publish gate (Phase 1): extracted claims wait in `claim`, and only the publish gate writes `catalyst`, so nothing in the review queue is ever in the live tables. Per-number sources for outcome key numbers, outcome history, and catalysts linked to several drugs or indications are added with the outcome agent and extractor items, which will show the shapes they need.
8. **Foreign keys** use ON DELETE RESTRICT: nothing that a claim or change points at can disappear.
9. **Source tier.** Every source document carries its rank from the spec's provenance ranking (regulator or SEC, company site, wire, registry, conference or journal). There is no news tier, so a news article cannot be stored as a source.
10. **Claims are idempotent and verbatim.** A claim is unique by document, field, quote span, agent, prompt version, and model, so re-running an extractor adds nothing. Quotes keep their exact characters, including spaces, and the span length must equal the quote length.
11. **Empty JSON means SQL NULL.** A missing old value is stored as SQL NULL, never as JSON `null`; required JSON values reject both.
12. **One current ticker per symbol.** A partial unique index allows each ticker to be current (no end date) for only one company.
13. **Check names carry a hash of their SQL.** Alembic cannot compare CHECK constraints, so each name ends in an 8-character hash of its condition, and a test compares the checks in the migrated database with the models. A changed rule without a migration fails CI.

## Alternatives

- SQLModel (one class for model and table): less code, but table classes skip validation on creation, which would let invalid records reach the database.
- Hand-written tables with a drift test (ADR 0001's interim choice): workable for one table; with fifteen, the generator is less code than the tests that would guard the copies.
- A versioned `catalyst` table (one row per version): avoids a second table, but makes every read pick the latest row and does not record which claim caused the change.

## Consequences

Adding a field is one line in a model plus a generated migration. The data dictionary (`docs/data-dictionary.md`) is generated too, and a test fails if it is stale.
