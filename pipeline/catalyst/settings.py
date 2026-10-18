"""Read settings from environment variables only (AGENTS.md: Settings and secrets).

Error messages name the setting and the setup step, never the value.
"""

import os
import re
from urllib.parse import quote

DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/catalyst"


class MissingSettingError(RuntimeError):
    """A required setting is absent or unusable; the message names the setup step that fixes it."""


PASSWORD_PLACEHOLDER = "[YOUR-PASSWORD]"  # noqa: S105 - Supabase's placeholder text, not a password
# This project's Session pooler (Supabase: Connect, then Session pooler; found on the live run of
# 2026-10-17). Supabase's Direct address is IPv6 only and GitHub's machines have no IPv6, so a
# Direct string in SUPABASE_DB_URL is sent through this pooler instead (ADR 0011).
SESSION_POOLER_HOST = "aws-1-us-west-2.pooler.supabase.com"
_DIRECT = re.compile(r"^(postgres(?:ql)?://)postgres:(\[YOUR-PASSWORD\])@db\.([a-z0-9]+)\.supabase\.co:5432/")


def database_url() -> str:
    """Return the database address.

    In order: `CATALYST_DATABASE_URL` (a local or test database); else the live database from
    `SUPABASE_DB_URL`, Supabase's Session pooler string copied unchanged with its
    `[YOUR-PASSWORD]` placeholder, filled from `SUPABASE_DB_PASSWORD`; else the local default.
    A Direct connection string is accepted too and rewritten to the Session pooler.
    """
    if local := os.environ.get("CATALYST_DATABASE_URL"):
        return local
    pooler = os.environ.get("SUPABASE_DB_URL", "").strip()
    if not pooler:
        return DEFAULT_DATABASE_URL
    if PASSWORD_PLACEHOLDER not in pooler:
        raise MissingSettingError(
            f"SUPABASE_DB_URL must be Supabase's Session pooler string with {PASSWORD_PLACEHOLDER} "
            "left in it. See docs/setup.md Part 5."
        )
    password = _required("SUPABASE_DB_PASSWORD", "Part 3, then Part 5")
    pooler = _DIRECT.sub(rf"\1postgres.\3:\2@{SESSION_POOLER_HOST}:5432/", pooler)
    filled = pooler.replace(PASSWORD_PLACEHOLDER, quote(password, safe=""))
    return re.sub(r"^postgres(?:ql)?://", "postgresql+psycopg://", filled)


def sec_contact_email() -> str:
    """Return the contact address the SEC requires in every request's User-Agent header."""
    value = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
    if "@" not in value:
        raise MissingSettingError(
            "SEC_CONTACT_EMAIL is missing or is not an email address. See docs/setup.md Part 5."
        )
    return value


def catalyst_anthropic_api_key() -> str:
    """Return the product's own Anthropic key. Never falls back to ANTHROPIC_API_KEY (AGENTS.md)."""
    value = os.environ.get("CATALYST_ANTHROPIC_API_KEY", "").strip()
    if not value:
        raise MissingSettingError(
            "CATALYST_ANTHROPIC_API_KEY is missing. See docs/setup.md Part 2, then Part 5."
        )
    return value


def _required(name: str, step: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingSettingError(f"{name} is missing. See docs/setup.md {step}.")
    return value


def supabase_url() -> str:
    """Return the Supabase project address, for example https://<project>.supabase.co."""
    return _required("SUPABASE_URL", "Part 3, then Part 5")


def supabase_secret_key() -> str:
    """Return the Supabase secret key for server-side access (storage and database)."""
    return _required("SUPABASE_SECRET_KEY", "Part 3, then Part 5")


def github_token() -> str:
    """Return the token GitHub Actions gives each run; it posts alerts as issues (ADR 0023)."""
    return _github("GITHUB_TOKEN")


def github_repository() -> str:
    """Return the repository as owner/name, which GitHub Actions sets for each run."""
    return _github("GITHUB_REPOSITORY")


def _github(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingSettingError(
            f"{name} is missing. GitHub Actions sets it; post issues only from a workflow."
        )
    return value
