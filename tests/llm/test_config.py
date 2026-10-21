"""The versioned model config: one provider and model per agent role, with a daily spend cap."""

from pathlib import Path

import pytest

from catalyst.llm.config import DEFAULT_CONFIG_PATH, ModelConfig, load_config

SPEC_ROLES = {
    "triage",
    "extractor",
    "verifier",
    "entity_resolver",
    "outcome",
    "auditor",
    "analyst",
    "assistant",
    "orchestrator",
}


def test_the_committed_config_covers_every_role_in_the_spec() -> None:
    config = load_config(DEFAULT_CONFIG_PATH)
    assert set(config.roles) == SPEC_ROLES


def test_the_committed_config_follows_the_spec_model_tiers() -> None:
    roles = load_config(DEFAULT_CONFIG_PATH).roles
    assert "haiku" in roles["triage"].model
    assert "opus" in roles["analyst"].model
    for role in SPEC_ROLES - {"triage", "analyst"}:
        assert "sonnet" in roles[role].model, role


def test_every_role_has_a_positive_daily_cap() -> None:
    for role in load_config(DEFAULT_CONFIG_PATH).roles.values():
        assert role.daily_cap_usd > 0


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.toml"
    path.write_text(body)
    return path


BASE = """
version = "test-1"
[pricing.m1]
input = "1"
cache_write = "1.25"
cache_read = "0.1"
output = "5"
"""


def test_a_role_whose_model_has_no_price_is_refused(tmp_path: Path) -> None:
    body = (
        BASE
        + '[roles.extractor]\nprovider = "anthropic"\nmodel = "m2"\nmax_tokens = 100\ndaily_cap_usd = "1"\n'
    )
    with pytest.raises(ValueError, match="no price"):
        load_config(_write(tmp_path, body))


def test_an_unknown_provider_is_refused(tmp_path: Path) -> None:
    body = (
        BASE + '[roles.extractor]\nprovider = "acme"\nmodel = "m1"\nmax_tokens = 100\ndaily_cap_usd = "1"\n'
    )
    with pytest.raises(ValueError, match="provider"):
        load_config(_write(tmp_path, body))


def test_a_zero_cap_is_refused(tmp_path: Path) -> None:
    body = (
        BASE
        + '[roles.extractor]\nprovider = "anthropic"\nmodel = "m1"\nmax_tokens = 100\ndaily_cap_usd = "0"\n'
    )
    with pytest.raises(ValueError, match="daily_cap_usd"):
        load_config(_write(tmp_path, body))


def test_the_extractor_has_an_input_limit_by_default_and_the_file_can_change_it(tmp_path: Path) -> None:
    role = '[roles.extractor]\nprovider = "anthropic"\nmodel = "m1"\nmax_tokens = 100\ndaily_cap_usd = "1"\n'
    assert load_config(_write(tmp_path, BASE + role)).max_input_tokens("extractor") == 40_000
    assert load_config(DEFAULT_CONFIG_PATH).max_input_tokens("extractor") == 40_000
    assert load_config(DEFAULT_CONFIG_PATH).max_input_tokens("verifier") is None
    changed = load_config(_write(tmp_path, BASE + role + "max_input_tokens = 25000\n"))
    assert changed.max_input_tokens("extractor") == 25_000
    with pytest.raises(ValueError, match="max_input_tokens"):
        load_config(_write(tmp_path, BASE + role + "max_input_tokens = 0\n"))


def test_the_analyst_has_an_input_limit_by_default_and_the_file_can_change_it(tmp_path: Path) -> None:
    role = '[roles.analyst]\nprovider = "anthropic"\nmodel = "m1"\nmax_tokens = 100\ndaily_cap_usd = "1"\n'
    assert load_config(_write(tmp_path, BASE + role)).max_input_tokens("analyst") == 30_000
    assert load_config(DEFAULT_CONFIG_PATH).max_input_tokens("analyst") == 30_000
    changed = load_config(_write(tmp_path, BASE + role + "max_input_tokens = 20000\n"))
    assert changed.max_input_tokens("analyst") == 20_000


def test_config_has_a_version(tmp_path: Path) -> None:
    config = ModelConfig.model_validate(
        {"version": "v9", "pricing": {}, "roles": {}},
    )
    assert config.version == "v9"
