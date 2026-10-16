# 0001 — Stack and repository layout

- **Status:** accepted
- **Date:** 2026-10-16
- **Roadmap item:** Repository skeleton

## Context

The spec (Engineering standards: Stack) and AGENTS.md (Starting conventions) fix the stack. The spec asks for an ADR covering it, so later changes have a record to replace.

## Decision

- Python 3.12 managed by `uv`, with `uv.lock` committed. One `pyproject.toml` at the repository root; the package `catalyst` lives in `pipeline/catalyst`, built with `uv_build`.
- Pydantic v2 models in `catalyst.schemas` are the source of truth. SQLAlchemy 2.0 tables in `catalyst.db.models` mirror them field for field, and a test fails if they drift.
- Alembic migrations in `pipeline/migrations`, configured by `alembic.ini` at the root. CI runs upgrade, downgrade, upgrade, then `alembic check` to prove the migrations match the tables.
- Supabase Postgres in production. Local work and CI use a disposable Postgres 16 (Docker locally, a service container in CI).
- Quality tools: ruff (lint and format, including docstring rules `D` and complexity limit C901 at 10), pyright strict, pytest with coverage, pre-commit hooks, gitleaks, pip-audit, Dependabot for `uv` and GitHub Actions.
- GitHub Actions are pinned to full commit hashes, with the version in a comment.
- Web (Next.js) arrives in Phase 2 under `web/`.

## Alternatives

- A `pyproject.toml` inside `pipeline/`: keeps Python files together, but every tool and CI step would need a working-directory switch. Rejected for simplicity.
- Generating SQLAlchemy tables from Pydantic automatically (for example SQLModel): fewer duplicated lines, but less control over constraints and types. Revisit in the "Data schemas" item if the mirror test becomes a burden.

## Consequences

Contributors run everything from the repository root with `uv run`. Adding a table means editing the Pydantic model and the table, then generating a migration.
