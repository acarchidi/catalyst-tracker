"""Read settings from environment variables only (AGENTS.md: Settings and secrets).

Error messages name the setting and the setup step, never the value.
"""

import os

DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/catalyst"


class MissingSettingError(RuntimeError):
    """A required setting is absent or unusable; the message names the setup step that fixes it."""


def database_url() -> str:
    """Return the database address: `CATALYST_DATABASE_URL`, or the local development database."""
    return os.environ.get("CATALYST_DATABASE_URL") or DEFAULT_DATABASE_URL


def sec_contact_email() -> str:
    """Return the contact address the SEC requires in every request's User-Agent header."""
    value = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
    if "@" not in value:
        raise MissingSettingError(
            "SEC_CONTACT_EMAIL is missing or is not an email address. See docs/setup.md Part 4."
        )
    return value


def catalyst_anthropic_api_key() -> str:
    """Return the product's own Anthropic key. Never falls back to ANTHROPIC_API_KEY (AGENTS.md)."""
    value = os.environ.get("CATALYST_ANTHROPIC_API_KEY", "").strip()
    if not value:
        raise MissingSettingError(
            "CATALYST_ANTHROPIC_API_KEY is missing. See docs/setup.md Part 2, then Part 4."
        )
    return value


def _required(name: str, step: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingSettingError(f"{name} is missing. See docs/setup.md {step}.")
    return value


def supabase_url() -> str:
    """Return the Supabase project address, for example https://<project>.supabase.co."""
    return _required("SUPABASE_URL", "Part 3, then Part 4")


def supabase_secret_key() -> str:
    """Return the Supabase secret key for server-side access (storage and database)."""
    return _required("SUPABASE_SECRET_KEY", "Part 3, then Part 4")
