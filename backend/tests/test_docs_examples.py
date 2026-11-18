"""Doc tests: the message examples in docs/ARCHITECTURE.md §12.2 must stay valid.

Each **Title** + ```json block in §12.2 is validated against its JSON Schema and
its Pydantic model. "…" placeholders are filled with a well-formed UUID
(for *_id keys) or timestamp (everything else) before validation.
The route-response example is also checked for internal consistency.
"""

import json
import math
import re
from datetime import datetime
from pathlib import Path

import pytest
from test_models import MODELS, SCHEMAS, _validator

ARCHITECTURE = Path(__file__).resolve().parents[2] / "docs" / "ARCHITECTURE.md"
PLACEHOLDER_UUID = "6f1c2a4e-8b3d-4c1a-9e2f-0a1b2c3d4e5f"
PLACEHOLDER_TIME = "2026-11-17T15:00:30Z"
MEDICAL_CRUISE_MPS = 65.0  # ARCHITECTURE §4; the §12.2 route example is a medical flight


def _doc_examples() -> dict[str, dict]:
    text = ARCHITECTURE.read_text()
    section = text[text.index("### 12.2") : text.index("## 13.")]
    blocks = re.findall(r"\*\*(.+?)\*\*\s*```json\n(.*?)```", section, re.S)
    # "Route response" -> "route-response", matching the schema file names.
    return {title.lower().replace(" ", "-"): json.loads(body) for title, body in blocks}


def _fill(obj, key=""):
    if isinstance(obj, dict):
        return {k: _fill(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, key) for v in obj]
    if obj == "…":
        return PLACEHOLDER_UUID if key.endswith("_id") else PLACEHOLDER_TIME
    return obj


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _haversine_m(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


DOC_EXAMPLES = _doc_examples()


def test_every_schema_has_one_doc_example():
    names = {
        p.name.removesuffix(".schema.json") for p in SCHEMAS.glob("*.schema.json")
    } - {"common"}
    assert set(DOC_EXAMPLES) == names


@pytest.mark.parametrize("name", sorted(DOC_EXAMPLES))
def test_doc_example_matches_schema_and_model(name):
    message = _fill(DOC_EXAMPLES[name])
    errors = [
        f"{'/'.join(map(str, e.path))}: {e.message}"
        for e in _validator(name).iter_errors(message)
    ]
    assert not errors, errors
    MODELS[name].model_validate_json(json.dumps(message))


def test_doc_route_response_is_consistent():
    doc = DOC_EXAMPLES["route-response"]
    assert _ts(doc["forecast_time"]) < _ts(doc["expires_at"])
    routes = doc["routes"]
    assert sorted(r["rank"] for r in routes) == list(range(1, len(routes) + 1))
    for r in routes:
        for term in ("est_time_s", "expected_hold_s", "conflict_probability"):
            assert r["cost_terms"][term] == r[term], term
        path_m = sum(
            _haversine_m(a, b) for a, b in zip(r["waypoints"], r["waypoints"][1:])
        )
        assert abs(r["distance_m"] - path_m) <= 100, (r["distance_m"], round(path_m))
        assert r["est_time_s"] == round(r["distance_m"] / MEDICAL_CRUISE_MPS)
        windows = [
            tuple(_ts(w[k]) for k in ("time_start", "eta_p50", "time_end"))
            for w in r["intent"]
        ]
        assert all(start <= p50 <= end for start, p50, end in windows)
        p50s = [p50 for _, p50, _ in windows]
        assert p50s == sorted(p50s)
        assert (p50s[-1] - p50s[0]).total_seconds() == r["est_time_s"]
