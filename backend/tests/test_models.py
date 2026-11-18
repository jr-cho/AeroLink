"""Model tests: Pydantic models must agree with the JSON Schemas on every example."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from referencing import Registry, Resource

from aerolink.contracts import (
    Intent,
    IntentStatus,
    RouteRequest,
    RouteResponse,
    Telemetry,
)

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
SCHEMAS = CONTRACTS / "schemas"
EXAMPLES = CONTRACTS / "examples"

MODELS = {
    "telemetry": Telemetry,
    "route-request": RouteRequest,
    "route-response": RouteResponse,
    "intent": Intent,
    "intent-status": IntentStatus,
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _registry() -> Registry:
    resources = []
    for path in SCHEMAS.glob("*.schema.json"):
        schema = _load(path)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


REGISTRY = _registry()


def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(
        _load(SCHEMAS / f"{name}.schema.json"),
        registry=REGISTRY,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def _cases(kind: str):
    return [
        pytest.param(p.parent.parent.name, p, id=f"{p.parent.parent.name}/{p.stem}")
        for p in sorted(EXAMPLES.glob(f"*/{kind}/*.json"))
    ]


def test_every_schema_has_a_model():
    names = {
        p.name.removesuffix(".schema.json") for p in SCHEMAS.glob("*.schema.json")
    } - {"common"}
    assert names == set(MODELS)


@pytest.mark.parametrize("name,path", _cases("valid"))
def test_valid_examples_round_trip(name, path):
    model = MODELS[name]
    m = model.model_validate_json(path.read_text())
    dumped = m.model_dump(mode="json")
    errors = list(_validator(name).iter_errors(dumped))
    assert not errors, [e.message for e in errors]
    assert model.model_validate_json(json.dumps(dumped)) == m


@pytest.mark.parametrize("name,path", _cases("invalid"))
def test_invalid_examples_rejected(name, path):
    with pytest.raises(ValidationError):
        MODELS[name].model_validate_json(path.read_text())


# --- Differential check: schema and model must agree beyond the examples --------
# Every leaf of every valid example is replaced, one at a time, with each value
# below (and deleted). The JSON Schema and the Pydantic model must give the same
# verdict on every mutant. The values target the places where Pydantic's parsers
# are more lenient or stricter than the schema's `format`/`type` checks.

EDGE_VALUES = [
    None, True, 0, 1, -1, 1.0, -0.0, 1.5, 1e308, 3, 4, 64, 65, 100, 101, 360,
    "", "1", "3.0", "ok", "medical", "hp-tgh", "HP-TGH", "dev-001", "dev_001", "a" * 33,
    [], [27.9, -82.4, 300], [27.9, -82.4], [27.9, -82.4, 300, 1], {},
    # UUID forms
    "6f1c2a4e-8b3d-4c1a-9e2f-0a1b2c3d4e5f", "6F1C2A4E-8B3D-4C1A-9E2F-0A1B2C3D4E5F",
    "6f1c2a4e8b3d4c1a9e2f0a1b2c3d4e5f", "{6f1c2a4e-8b3d-4c1a-9e2f-0a1b2c3d4e5f}",
    "urn:uuid:6f1c2a4e-8b3d-4c1a-9e2f-0a1b2c3d4e5f", "6f1c2a4e-8b3d-4c1a-9e2f-0a1b2c3d4e5g",
    # timestamp forms
    "2026-11-17T15:00:00Z", "2026-11-17T15:00:00.123456789+05:30", "2026-11-17t15:00:00z",
    "2026-11-17T15:00:00", "2026-11-17 15:00:00Z", "2026-11-17T15:00Z", "2026-11-17",
    "2026-11-17T15:00:00+0000", "2026-11-17T24:00:00Z", "2026-02-29T15:00:00Z",
    "2026-11-17T15:00:00+24:00", "1763391600", 1763391600,
]
# Not included: strings with a trailing "\n". Python's `re` lets `$` match before
# a final newline, so the Python jsonschema library accepts e.g. "dev-001\n"
# where the JSON Schema spec (ECMA-262 regex) and Pydantic both reject it.


def _leaf_paths(obj, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _leaf_paths(v, path + (k,))
    elif isinstance(obj, list) and obj:
        for i, v in enumerate(obj):
            yield from _leaf_paths(v, path + (i,))
    yield path


def _mutants(example):
    text = json.dumps(example)
    seen = set()
    for path in _leaf_paths(example):
        # routes/0/rank and routes/2/rank hit the same rule: mutate each shape once.
        shape = tuple("[]" if isinstance(k, int) else k for k in path)
        if not path or shape in seen:
            continue
        seen.add(shape)
        for value in [*EDGE_VALUES, KeyError]:
            mutant = json.loads(text)  # cheaper than copy.deepcopy
            parent = mutant
            for key in path[:-1]:
                parent = parent[key]
            if value is KeyError:
                if isinstance(path[-1], int):
                    continue
                del parent[path[-1]]
            else:
                parent[path[-1]] = value
            yield path, value, mutant


@pytest.mark.parametrize("name,path", _cases("valid"))
def test_schema_and_model_agree_on_mutants(name, path):
    validator, model = _validator(name), MODELS[name]
    disagreements = []
    for field, value, mutant in _mutants(_load(path)):
        schema_ok = validator.is_valid(mutant)
        try:
            model.model_validate_json(json.dumps(mutant))
            model_ok = True
        except ValidationError:
            model_ok = False
        if schema_ok != model_ok:
            side = "schema" if schema_ok else "model"
            disagreements.append(f"{'/'.join(map(str, field))} = {value!r}: only {side} accepts")
    assert not disagreements, disagreements[:20]
