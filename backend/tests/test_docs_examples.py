"""The JSON examples in ARCHITECTURE §12.2 must match the schemas' fields.

The doc examples use "…" as a placeholder value, so this checks structure
(every top-level field exists in the schema, every required field is present),
not values. Full value checks live in contracts/examples/.
"""

import json
import re

import pytest
from conftest import REPO, SCHEMAS, load

DOC = REPO / "docs" / "ARCHITECTURE.md"

# Heading in §12.2 -> schema name
SECTIONS = {
    "Telemetry": "telemetry",
    "Route request": "route-request",
    "Route response": "route-response",
    "Route selection": "route-selection",
    "Flight status": "flight-status",
    "Traffic": "traffic",
}


def _doc_examples() -> dict[str, dict]:
    text = DOC.read_text()
    section = text[text.index("### 12.2") : text.index("### 12.3")]
    found = {}
    for title, name in SECTIONS.items():
        m = re.search(rf"\*\*{re.escape(title)}\*\*\s*```json\n(.*?)```", section, re.S)
        assert m, f"no JSON example under **{title}** in ARCHITECTURE §12.2"
        found[name] = json.loads(m.group(1))
    return found


EXAMPLES = _doc_examples()


@pytest.mark.parametrize("name", list(SECTIONS.values()))
def test_doc_example_fields_match_schema(name: str):
    schema = load(SCHEMAS / f"{name}.schema.json")
    example = EXAMPLES[name]
    assert set(example) <= set(schema["properties"]), set(example) - set(schema["properties"])
    assert set(schema["required"]) <= set(example), set(schema["required"]) - set(example)
    assert example["schema_version"] == "4.0"
