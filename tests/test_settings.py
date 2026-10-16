"""Settings come only from environment variables, and a missing SEC contact stops the run."""

import pytest

from catalyst import settings


def test_database_url_defaults_to_local_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CATALYST_DATABASE_URL", raising=False)
    assert settings.database_url() == settings.DEFAULT_DATABASE_URL


def test_database_url_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATALYST_DATABASE_URL", "postgresql+psycopg://u@h:1/db")
    assert settings.database_url() == "postgresql+psycopg://u@h:1/db"


def test_sec_contact_email_missing_names_the_setup_step(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEC_CONTACT_EMAIL", raising=False)
    with pytest.raises(settings.MissingSettingError, match=r"SEC_CONTACT_EMAIL.*docs/setup.md Part 4"):
        settings.sec_contact_email()


@pytest.mark.parametrize("value", ["", "   ", "not-an-email"])
def test_sec_contact_email_rejects_blank_or_malformed(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SEC_CONTACT_EMAIL", value)
    with pytest.raises(settings.MissingSettingError):
        settings.sec_contact_email()


def test_sec_contact_email_error_never_echoes_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "secret-looking-value")
    with pytest.raises(settings.MissingSettingError) as caught:
        settings.sec_contact_email()
    assert "secret-looking-value" not in str(caught.value)


def test_sec_contact_email_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "  data@example.org \n")
    assert settings.sec_contact_email() == "data@example.org"
