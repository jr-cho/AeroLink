"""Shared helpers: schema registry and example loading."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.protocols import Validator
from referencing import Registry, Resource

REPO = Path(__file__).resolve().parents[2]
SCHEMAS = REPO / "contracts" / "schemas"
EXAMPLES = REPO / "contracts" / "examples"


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def _registry() -> Registry:
    resources = []
    for path in SCHEMAS.glob("*.schema.json"):
        schema = load(path)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


REGISTRY = _registry()


def validator(name: str) -> Validator:
    return Draft202012Validator(
        load(SCHEMAS / f"{name}.schema.json"),
        registry=REGISTRY,
        format_checker=Draft202012Validator.FORMAT_CHECKER,  # enforce uuid / date-time
    )
