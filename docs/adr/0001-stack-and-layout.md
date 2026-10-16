# 0001 — Stack and repository layout

- **Status:** accepted
- **Date:** 2026-10-16
- **Roadmap item:** Repository skeleton

## Context

The spec (Engineering standards: Stack) and AGENTS.md (Starting conventions) fix the stack. The spec asks for an ADR covering it, so later changes have a record to replace.

## Decision

- Python 3.12 managed by `uv`, with `uv.lock` committed. One `pyproject.toml` at the repository root; the package `catalyst` lives in `pipeline/catalyst`, built with `uv_build`.
- Pydantic v2 models in `catalyst.schemas` are the source of truth. SQLAlchemy 2.0 tables are generated from them (ADR 0003 replaces the walking skeleton's hand-written table).
- Alembic migrations in `pipeline/migrations`, configured by `alembic.ini` at the root. CI runs upgrade, downgrade, upgrade, then `alembic check` to prove the migrations match the tables.
- Supabase Postgres in production. Local work and CI use a disposable Postgres 16 (Docker locally, a service container in CI).
- Quality tools: ruff (lint and format, including docstring rules `D` and complexity limit C901 at 10), pyright strict, pytest with coverage, pre-commit hooks, gitleaks, pip-audit, Dependabot for `uv` and GitHub Actions.
- GitHub Actions are pinned to full commit hashes, with the version in a comment.
- Web (Next.js) arrives in Phase 2 under `web/`.

## Alternatives

- A `pyproject.toml` inside `pipeline/`: keeps Python files together, but every tool and CI step would need a working-directory switch. Rejected for simplicity.
- Generating tables from Pydantic now: what the spec asks for, but one table does not justify the generator yet. It is the first task of the "Data schemas" item, which adds about ten entities.

## Consequences

Contributors run everything from the repository root with `uv run`. Adding a table means editing the Pydantic model and the table, then generating a migration.
