"""The committed data dictionary matches the models (CP2: every field has a description)."""

from catalyst.data_dictionary import OUTPUT, main, render
from catalyst.schemas import ALL_MODELS


def test_committed_dictionary_is_current() -> None:
    assert main(["--check"]) == 0, "run: uv run python -m catalyst.data_dictionary"


def test_every_table_and_field_is_listed() -> None:
    text = render()
    for model in ALL_MODELS:
        assert f"## `{model.__tablename__}`" in text
        for name in model.model_fields:
            assert f"| `{name}` |" in text


def test_links_and_allowed_values_are_spelled_out() -> None:
    text = OUTPUT.read_text()
    assert "| `company_id` | link to `company` | yes |" in text
    assert "one of: `day`, `month`, `quarter`, `half`, `year`" in text
