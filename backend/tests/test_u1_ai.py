"""U1 story 4: the AI identity, its measured results and the disclaimer acknowledgements (U-API §3).

The numbers on ``GET /api/ai`` must come from the committed eval files: a test rewrites a copy of them and checks
that the endpoint follows.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient
from test_g2_auth import operator_login

from aerolink.service import ai_api
from aerolink.service.app import create_app
from aerolink.service.devices import provision
from aerolink.service.settings import Settings
from aerolink.service.store import Store

REPO_EVAL = Path(__file__).resolve().parents[2] / "backend" / "eval-results"
HTTPS = "https://aerolink.test"


def test_results_are_read_from_the_committed_eval_files():
    r = ai_api.results(REPO_EVAL)
    assert r["eta_error_ai_s"] == 61  # A5 (full AeroLink) ETA error at departure, m5-ablations.json
    assert r["eta_error_classical_s"] == 104  # B1 (classical navigation)
    assert r["window_coverage"] == 0.84  # A5 p10-p90 coverage
    assert r["offroute_precision"] == 1.0  # offroute-e5.5.json, the combined AI detector
    assert r["offroute_recall"] == 0.875
    assert "identical trip times" in r["route_choice_note"]  # measured: A5 and B1 trips match


def test_results_follow_the_files_not_constants(tmp_path: Path):
    for name in ("m5-ablations.json", "offroute-e5.5.json"):
        shutil.copy(REPO_EVAL / name, tmp_path / name)
    m5 = json.loads((tmp_path / "m5-ablations.json").read_text())
    for row in m5["summary"]:
        if row["config"] == "A5":
            row["eta_error_mean_s"] = 42.4
    (tmp_path / "m5-ablations.json").write_text(json.dumps(m5))
    r = ai_api.results(tmp_path)
    assert r["eta_error_ai_s"] == 42
    assert r["eta_error_classical_s"] == 104


def test_missing_files_give_nulls_not_errors(tmp_path: Path):
    r = ai_api.results(tmp_path)
    assert r["eta_error_ai_s"] is None and r["offroute_recall"] is None
    assert r["route_choice_note"].startswith("Route choices differ")


def _app(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'ai.db'}"

    async def prepare() -> str:
        store = Store.connect(url)
        await store.create_all()
        code = (await provision(store, "dev-001", "passenger", False, HTTPS, tmp_path / "labels"))["code"]
        await store.close()
        return code

    code = asyncio.run(prepare())
    app = create_app(Settings(database_url=url, forecaster="cv", suggestion_interval_s=0), mqtt=False)
    return app, code


def test_api_ai_describes_the_models_and_the_disclaimer(tmp_path: Path):
    app, _ = _app(tmp_path)
    with TestClient(app, base_url=HTTPS) as c:
        body = c.get("/api/ai").json()
    assert body["forecaster"]["name"] == "cv" and body["forecaster"]["forecast_age_s"] is not None
    assert [m["key"] for m in body["models"]] == ["forecaster", "demand", "offroute", "summary"]
    assert all(m["what"] and m["drives"] and m["title"] for m in body["models"])
    first = body["models"][0]
    assert first["name"] == "aerolink-gbm" and first["title"] == "Traffic & arrival-time forecaster"
    assert body["disclaimer"]["version"] == "2026-11-27"
    assert body["disclaimer"]["text"].startswith("AeroLink is a prototype and is advisory only.")  # pilot text
    assert "not a clearance" in body["disclaimer"]["pilot"] and "not ATC clearances" in body["disclaimer"]["ops"]
    assert body["results"]["eta_error_ai_s"] == 61


def test_pilot_acknowledges_the_advisory_once_and_the_screen_sees_it(tmp_path: Path):
    app, code = _app(tmp_path)
    with TestClient(app, base_url=HTTPS) as c:
        assert c.post("/api/devices/dev-001/ai-ack", json={"version": "2026-11-26"}).status_code == 401
        assert c.post("/api/devices/dev-001/pair", json={"code": code}).status_code == 200
        assert c.get("/api/devices/dev-001").json()["ai_ack_version"] is None
        r = c.post("/api/devices/dev-001/ai-ack", json={"version": "2026-11-26"})
        assert r.status_code == 204
        assert c.get("/api/devices/dev-001").json()["ai_ack_version"] == "2026-11-26"
        assert c.post("/api/devices/dev-001/ai-ack", json={"version": ""}).status_code == 422


def test_operator_acknowledgement_is_recorded_and_shown_on_me(tmp_path: Path):
    app, _ = _app(tmp_path)
    with TestClient(app, base_url=HTTPS) as c:
        assert c.post("/api/auth/ai-ack", json={"version": "2026-11-26"}).status_code == 401
        operator_login(c)
        assert c.get("/api/auth/me").json()["ai_ack_version"] is None
        assert c.post("/api/auth/ai-ack", json={"version": "2026-11-26"}).status_code == 204
        assert c.get("/api/auth/me").json()["ai_ack_version"] == "2026-11-26"


def test_acknowledgements_are_audit_events(tmp_path: Path):
    app, code = _app(tmp_path)
    with TestClient(app, base_url=HTTPS) as c:
        c.post("/api/devices/dev-001/pair", json={"code": code})
        c.post("/api/devices/dev-001/ai-ack", json={"version": "2026-11-26"})
        rows = asyncio.run(app.state.service.store.events(kinds=["ai_ack"]))  # the app's own store, same database
    assert rows and rows[0]["aircraft_id"] == "dev-001" and rows[0]["detail"]["version"] == "2026-11-26"
    assert rows[0]["acked_at"] is not None  # a record, not an alert
