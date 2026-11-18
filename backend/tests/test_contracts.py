"""Contract tests: every valid example must pass its schema, every invalid one must fail.

Layout:
    contracts/schemas/<name>.schema.json
    contracts/examples/<name>/valid/*.json
    contracts/examples/<name>/invalid/*.json
"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
SCHEMAS = CONTRACTS / "schemas"
EXAMPLES = CONTRACTS / "examples"


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
    schema = _load(SCHEMAS / f"{name}.schema.json")
    return Draft202012Validator(
        schema,
        registry=REGISTRY,
        format_checker=Draft202012Validator.FORMAT_CHECKER,  # enforce uuid / date-time
    )


def _cases(kind: str):
    return [
        pytest.param(p.parent.parent.name, p, id=f"{p.parent.parent.name}/{p.stem}")
        for p in sorted(EXAMPLES.glob(f"*/{kind}/*.json"))
    ]


def test_every_schema_has_examples():
    for path in SCHEMAS.glob("*.schema.json"):
        name = path.name.removesuffix(".schema.json")
        if name == "common":
            continue
        assert list((EXAMPLES / name / "valid").glob("*.json")), f"{name}: no valid examples"
        assert list((EXAMPLES / name / "invalid").glob("*.json")), f"{name}: no invalid examples"


def test_schemas_are_valid_json_schema():
    for path in SCHEMAS.glob("*.schema.json"):
        Draft202012Validator.check_schema(_load(path))


@pytest.mark.parametrize("name,path", _cases("valid"))
def test_valid_examples_pass(name, path):
    errors = list(_validator(name).iter_errors(_load(path)))
    assert not errors, [e.message for e in errors]


@pytest.mark.parametrize("name,path", _cases("invalid"))
def test_invalid_examples_fail(name, path):
    errors = list(_validator(name).iter_errors(_load(path)))
    assert errors, f"{path.name} should have failed validation"
