# 0004 — Model interface, Anthropic adapter, and spend caps

- **Status:** accepted
- **Date:** 2026-10-16
- **Roadmap item:** Model interface (`ModelClient`), first provider adapter, contract tests, per-role spend caps

## Context

The spec (Multi-provider model layer; Model assignments; Cost guardrails) fixes the shape: one `ModelClient`, one adapter per provider, a versioned config assigning provider and model per role, and a daily spend cap per role that stops and alerts, never switching to a pricier model. It leaves the mechanics open.

## Decision

1. **Config.** `config/models.toml` holds a version string, prices per model, and per role: provider, model, `max_tokens`, effort, and daily cap in dollars. Roles and model tiers follow the spec's Model assignments table: Haiku 5.5 for triage, Opus 5.5 for analyst drafts, Sonnet 5.5 for the rest. Prices were read from Anthropic's pricing page on 2026-10-16 and include Haiku 5.5's higher price for prompts over 100,000 tokens.
2. **Caps.** Daily caps add up to $13.50, at most about $405 a month, inside the spec's $150 to $600 estimate. The owner's $50-a-month workspace limit (setup Part 2) is lower and binds first. A day is a New York calendar day. Before each call, the client adds the call's worst-case cost (input estimated at 3 characters per token, plus `max_tokens` of output) to the role's spend today; if that passes the cap, the role stops. Two jobs running at the same moment could each pass the check, so scheduled jobs that call models will share one GitHub Actions concurrency group.
3. **Alert.** At the cap the client logs an error-level `spend_cap_reached` event and raises, which fails the job; GitHub emails the owner about failed scheduled runs. The daily status report (Phase 1) will list cap hits too.
4. **Ledger.** Every call, including refused, truncated, and invalid ones, is a row in `model_call` with tokens by kind, cost, latency, provider request id, status, and config version. Caps are summed from this table, so all jobs share one total per role.
5. **No model switching.** The client calls only the role's configured model. The adapter does not request Anthropic's server-side refusal fallback, which can re-run a refused request on another model; a refusal is recorded and raised, and the claim goes to review. If a response names a different model than requested, the call is recorded as `model_switched` and rejected.
6. **Structured output.** The adapter uses the Messages API's JSON-schema output format. Keywords the API does not accept (length limits, patterns, numeric bounds) are stripped from the copy sent; `ModelClient` validates every answer against the full schema with `jsonschema`, so those limits still hold.
7. **Prompt files** are `prompts/<id>/<version>.toml` with purpose, output schema, evaluation set, and tested models. Every object in an output schema must forbid extra properties.
8. **Caching.** Each prompt's system text is sent with a cache marker, so repeated calls on the same prompt pay cache-read prices for it once it passes the model's minimum cacheable length.
9. **Key.** The adapter takes `CATALYST_ANTHROPIC_API_KEY` explicitly; it never falls back to `ANTHROPIC_API_KEY` or a local login profile.
10. **Tests.** A provider contract suite (`tests/llm/provider_contract.py`) runs against each adapter. For Anthropic it uses responses shaped like the documented API. One recorded live response will be added once the owner has created the key.

## Alternatives

- Forced tool use for JSON: rejected; Sonnet 5.5 and Opus 5.5 refuse forced tool choice, and the JSON-schema output format is the documented way to get structured answers.
- Server-side refusal fallback: Anthropic recommends it, but it conflicts with the owner's rule that a role never moves to another model.
- Checking the cap after the call: simpler, but a single large call could overshoot by its full cost.

## Consequences

Agents call `ModelClient.run(role, prompt, document)` and get validated JSON plus provider, model, cost, and latency. Moving the verifier to a second provider is one adapter that passes the contract suite and a one-line config change.
