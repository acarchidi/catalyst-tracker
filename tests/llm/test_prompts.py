"""Prompts are versioned files with purpose, output schema, evaluation set, and tested models (spec)."""

from pathlib import Path

import pytest

from catalyst.llm.prompts import load_prompt

GOOD = '''
id = "demo"
version = "1.0.0"
purpose = "Say whether a document mentions a PDUFA date."
eval_set = "tests/evals/demo.jsonl"
tested_models = ["claude-sonnet-5-5"]
system = "You read one document. Answer only from it."
output_schema = """
{"type": "object", "additionalProperties": false, "required": ["mentions_pdufa"],
 "properties": {"mentions_pdufa": {"type": "boolean"}}}
"""
'''


def _write(tmp_path: Path, body: str, name: str = "1.0.0") -> Path:
    folder = tmp_path / "demo"
    folder.mkdir(exist_ok=True)
    (folder / f"{name}.toml").write_text(body)
    return tmp_path


def test_a_prompt_loads_by_id_and_version(tmp_path: Path) -> None:
    prompt = load_prompt("demo", "1.0.0", root=_write(tmp_path, GOOD))
    assert prompt.purpose.startswith("Say whether")
    assert prompt.output_schema["required"] == ["mentions_pdufa"]


def test_the_file_name_must_match_the_version_inside(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="version"):
        load_prompt("demo", "2.0.0", root=_write(tmp_path, GOOD, name="2.0.0"))


def test_objects_must_forbid_extra_properties(tmp_path: Path) -> None:
    loose = GOOD.replace('"additionalProperties": false, ', "")
    with pytest.raises(ValueError, match="additionalProperties"):
        load_prompt("demo", "1.0.0", root=_write(tmp_path, loose))


def test_an_invalid_json_schema_is_refused(tmp_path: Path) -> None:
    broken = GOOD.replace('{"type": "boolean"}', '{"type": "maybe"}')
    with pytest.raises(ValueError, match="schema"):
        load_prompt("demo", "1.0.0", root=_write(tmp_path, broken))


@pytest.mark.parametrize("missing", ["purpose", "eval_set", "tested_models"])
def test_metadata_is_required(tmp_path: Path, missing: str) -> None:
    body = "\n".join(line for line in GOOD.splitlines() if not line.startswith(missing))
    with pytest.raises(ValueError, match=missing):
        load_prompt("demo", "1.0.0", root=_write(tmp_path, body))
