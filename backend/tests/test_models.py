"""The Pydantic models in aerolink.contracts must agree with the JSON Schemas.

1. Every valid example parses; every invalid example is rejected by the model too.
2. Mutation test: from each valid example, generate broken variants (drop a field,
   add an unknown field, swap a value's type). Schema and model must both reject.
"""

import copy
import json
from pathlib import Path

import pytest
from conftest import EXAMPLES, load, validator
from pydantic import ValidationError

from aerolink.contracts import MODELS


def _cases(kind: str):
    return [
        pytest.param(p.parent.parent.name, p, id=f"{p.parent.parent.name}/{p.stem}")
        for p in sorted(EXAMPLES.glob(f"*/{kind}/*.json"))
    ]


def _model_accepts(name: str, data: dict) -> bool:
    try:
        MODELS[name].model_validate_json(json.dumps(data))
        return True
    except ValidationError:
        return False


def _schema_accepts(name: str, data: dict) -> bool:
    return not list(validator(name).iter_errors(data))


def test_every_schema_has_a_model():
    names = {p.name.removesuffix(".schema.json") for p in EXAMPLES.parent.joinpath("schemas").glob("*.schema.json")}
    assert names - {"common"} == set(MODELS)


@pytest.mark.parametrize("name,path", _cases("valid"))
def test_model_accepts_valid_examples(name: str, path: Path):
    assert _model_accepts(name, load(path))


@pytest.mark.parametrize("name,path", _cases("invalid"))
def test_model_rejects_invalid_examples(name: str, path: Path):
    assert not _model_accepts(name, load(path))


def _mutations(data: dict):
    """Broken variants of a valid message, one change each."""
    for key in data:
        dropped = copy.deepcopy(data)
        del dropped[key]
        yield f"drop {key}", dropped
    yield "add unknown field", {**data, "unexpected": 1}
    for key, value in data.items():
        if isinstance(value, str):
            yield f"{key} as number", {**data, key: 12345}
        elif isinstance(value, bool):
            yield f"{key} as string", {**data, key: "yes"}
        elif isinstance(value, (int, float)):
            yield f"{key} as string", {**data, key: "12"}
        elif isinstance(value, dict):
            yield f"{key} as list", {**data, key: [value]}


@pytest.mark.parametrize("name,path", _cases("valid"))
def test_schema_and_model_reject_the_same_mutations(name: str, path: Path):
    for label, broken in _mutations(load(path)):
        schema_ok, model_ok = _schema_accepts(name, broken), _model_accepts(name, broken)
        assert not schema_ok, f"schema accepted: {label}"
        assert not model_ok, f"model accepted: {label}"
