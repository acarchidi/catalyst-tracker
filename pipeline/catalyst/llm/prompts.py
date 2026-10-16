"""Versioned prompt files: prompts/<id>/<version>.toml (spec: Documentation and machine-readability).

Each file states its purpose, output JSON Schema, evaluation set, and the models it was tested on.
"""

import json
import tomllib
from pathlib import Path
from typing import Any, Self, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROMPTS_ROOT = Path(__file__).resolve().parents[3] / "prompts"


def _objects_are_closed(schema: object, path: str = "$") -> None:
    """Require `additionalProperties: false` on every object, as structured outputs demand."""
    if isinstance(schema, dict):
        node = cast(dict[str, object], schema)
        if node.get("type") == "object" and node.get("additionalProperties") is not False:
            raise ValueError(f"{path}: objects must set additionalProperties to false")
        for key, value in node.items():
            _objects_are_closed(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for index, item in enumerate(cast(list[object], schema)):
            _objects_are_closed(item, f"{path}[{index}]")


class Prompt(BaseModel):
    """One version of one prompt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    purpose: str = Field(min_length=1)
    eval_set: str = Field(min_length=1, description="Path of the evaluation set this prompt is scored on.")
    tested_models: list[str] = Field(min_length=1)
    system: str = Field(min_length=1)
    output_schema: dict[str, Any]

    @field_validator("output_schema", mode="before")
    @classmethod
    def _parse(cls, value: object) -> object:
        return json.loads(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _schema(self) -> Self:
        try:
            Draft202012Validator.check_schema(self.output_schema)
        except SchemaError as error:
            raise ValueError(f"output_schema is not a valid JSON schema: {error.message}") from error
        _objects_are_closed(self.output_schema)
        return self


def load_prompt(prompt_id: str, version: str, root: Path = PROMPTS_ROOT) -> Prompt:
    """Load prompts/<prompt_id>/<version>.toml and check it describes itself correctly."""
    with (root / prompt_id / f"{version}.toml").open("rb") as handle:
        prompt = Prompt.model_validate(tomllib.load(handle))
    if (prompt.id, prompt.version) != (prompt_id, version):
        raise ValueError(f"file {prompt_id}/{version}.toml declares {prompt.id} version {prompt.version}")
    return prompt
