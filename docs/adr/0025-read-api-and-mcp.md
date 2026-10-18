# 0025 — Read-only data API and MCP server

- **Status:** accepted
- **Date:** 2026-10-17
- **Roadmap item:** Phase 2, "Read-only MCP server and directory listings" (groundwork for Phase 3, "Full MCP tools and research assistant")

## Context

The spec (Machine access) puts one API under the web app, the MCP server, and the research assistant. It asks for versioned paths, cursor pagination, a `sources` list on every object, and free and paid tiers (Offering). It also sets two hard rules: the API and MCP never return price or return series, and every response and tool description says nothing is financial advice. It leaves open:

- what "published" means before the publish gate exists;
- the endpoint shapes and filters;
- how pages work;
- how free and paid differ on each endpoint;
- how the MCP server runs before hosting and accounts exist.

## Decision

1. **Published data only.** The publish gate is the only writer of `catalyst` and its link tables (ADR 0003), so the read layer (`catalyst.api.read`) reads those tables. It reaches claims only through `catalyst_change` rows. A `catalyst_drug` link's claim is shown only through the `drugs` change row that cites it (rule 2).

   It never reads `claim_check`, `entity_match`, `document_reading`, or `model_call`. It never looks up claims by subject either, since that would reach the review queue. Every request runs in a transaction the database itself keeps read-only. A test records every SQL statement sent and checks this.
2. **Every value has a source, or the record is not shown.** A catalyst appears only when each of its non-empty fields has a latest change row, and that row's `new_value` equals the value shown. The links to drugs count as a field, `drugs`. Anything else is held back and logged as `catalyst_withheld`.

   This is a contract for the publish gate: `new_value` holds the field's JSON form, as Pydantic writes it:
   - dates as `YYYY-MM-DD`;
   - ids as text;
   - controlled values as their text;
   - `drugs` as the full list of the catalyst's links after the change, each `{"drug_id": "<id>", "role": "<role>"}`, sorted by `drug_id`, then `role`. Removing the last link writes `[]`.

   Two more rules for drug links:
   - Each `catalyst_drug` row's `claim_id` must be the claim of one of that catalyst's `drugs` change rows. Its source is that change row's claim. A link citing any other claim holds the record back, so a hand-added or hand-edited link cannot show a claim the gate never accepted.
   - Once a catalyst has a `drugs` change row, its links must match the latest one, even when there are no links.

   One date move is the set of date rows (`window_*` fields with an old value) that cite the same claim. The latest move is the one whose claim the newest such row cites. Rows are ordered by `learned_at`, then row id, so ties break the same way every time. A move whose rows were written at slightly different times stays whole.
3. **Trials are not shown yet.** When this was decided, `catalyst_trial` had no claim column, so a trial link had no quote behind it. The catalyst writer's migration now gives each trial link its claim (ADR 0018, part 5b); showing trials, and the phase filter, are left for a later item.
4. **Endpoints.** All are GET, under `/v1`:
   - `/v1/catalysts`: upcoming and slipped catalysts whose window overlaps a date window (spec: Search and filters, "Date window"), earliest first. Filters are `from`, `to`, `company`, `drug`, and `type` (repeatable).
   - `/v1/catalysts/{id}`: one catalyst.
   - `/v1/companies/{company}/catalysts`: one company's upcoming catalysts.

   A company is found by internal id, SEC number (one to ten ASCII digits), or current ticker. Any other key is looked up as a ticker. A drug is found by id, or by any of its names, ignoring case. The drug filter is open to free callers. The spec's free filters name ticker, date, phase, and type; a drug name is the same kind of lookup as a ticker, and a free caller still sees only the next 30 days. The OpenAPI document comes from the same Pydantic shapes (`catalyst.api.views`).
5. **Pages.** A cursor holds the last row's window start and id, encoded. Results are ordered by window start, then id. `limit` runs from 1 to 200 and defaults to 50. A newly published record therefore never shifts a page the way offset paging would. A cursor that does not decode to a date and an id is refused (HTTP 400). A well-formed cursor the API did not give out is accepted: it only sets where the page starts, inside the caller's own window, so it can reveal nothing the caller could not page to.
6. **Free versus paid** (spec: Offering). The caller passes an `Access` level: free or paid.
   - **HTTP:** a plain dependency returns free until accounts exist. Paid tests override it.
   - **MCP over stdio:** always free.
   - **Free callers get:**
     - the calendar from today through today + 30 days, whatever window they ask for; the page's `window_to` never reports a day past today + 30, even when the window they asked for is empty;
     - the summary fields;
     - every source link and quote;
     - only the latest date change: every date row of the most recent move (rule 2).
   - **Paid callers get:**
     - the window they ask for, open-ended by default;
     - `history`, every change with its source.

   Paid-only fields exist only on `CatalystDetail`. A free response is built as a `CatalystSummary`, a separate type, so a forgotten filter cannot leak a paid field. A test fails if a field is added to the detail without being decided as paid. One catalyst by id is free (spec: `get_catalyst`, Free).
7. **The notice.** Every response body carries `notice`: "Nothing here is financial advice…". This includes errors (400, 404, 422, and 500 from a catch-all handler that hides the error's detail). Every MCP tool description and tool error carries it too. Each description also says what the tool never returns: price or return series, buy or sell ratings, price targets, or answers about anyone's holdings. A test walks every schema and response for field names that would carry prices, returns, ratings, or holdings.
8. **MCP server.** It uses the official SDK (`mcp` 2.x, `MCPServer`), runs over stdio (`catalyst mcp`), and has three tools marked read-only:
   - `list_upcoming_catalysts`;
   - `get_catalyst`;
   - `list_company_catalysts`.

   It calls the same read layer as the HTTP API in the same process, not the HTTP API itself. Both are thin layers over one code path, so the spec's "one API under everything" holds without a network hop. The remote server (streamable HTTP, OAuth for paid users) comes with hosting and accounts.
9. **Local only.** `catalyst api` listens on 127.0.0.1. Nothing is deployed.
10. **Deferred:**
    - **ETags and rate limits by tier:** needed once the API is public.
    - **The change-history link and endpoint:** paid; Phase 3 `get_catalyst_history`.
    - **Outcomes:** `get_outcomes`.
    - **Text-fragment links that scroll to the quote.**
    - **The phase filter:** needs trial links with claims.

## Alternatives

- **One shape with optional paid fields, dropped when serialized.** A single missed exclusion would leak paid data. Separate types make the free shape unable to hold it.
- **Offset pages.** Simpler, but a record published between two requests shifts every later page.
- **Reading claims by `subject_id`.** This would find claims the publish gate never accepted, including the verifier's own. That breaks "never shown while in review".
- **The MCP server calling the HTTP API.** It would add a running web server and a network hop for a local stdio tool, with no gain in correctness. The shared read layer gives the same answers.
- **Trusting `catalyst` values without checking their change rows.** That is cheaper, but a hand edit or a gate bug could then publish a value no quote supports.

## Consequences

- The publish gate must write a change row for every field it sets, with `new_value` in JSON form. If it does not, the record is held back, never shown without a source.
- Adding a paid field means adding it to `CatalystDetail` and to the test that lists paid fields. That makes it a reviewed decision.
- Revisit when accounts land (real access levels), when hosting lands (ETags, rate limits, remote MCP), and when the publish gate gives trial links a claim.
