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
