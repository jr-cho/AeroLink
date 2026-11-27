"""F1: route revisions survive a restart; classical navigation flags off-route flights (ARCHITECTURE §6.6)."""

# ruff: noqa: F811  (pytest fixtures imported from test_e3_pilot_api)
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import app_state, portal_of
from test_e2_backend import fly, one_flight
from test_e3_pilot_api import Env, Rig, env, err, rig  # noqa: F401
from test_service import node, route_request, selection, telemetry

from aerolink.contracts import FlightStatus, RouteResponse
from aerolink.network import Network
from aerolink.service.core import Service
from aerolink.service.planner import Planner
from aerolink.service.settings import Settings
from aerolink.service.store import Store

BASE = "aerolink/aircraft/dev-001"


def test_revision_can_be_selected_after_a_backend_restart(tmp_path: Path, fl_network: Network):
    async def go():
        from test_service import T0

        s, clock, base, rid, resp = await one_flight(tmp_path, fl_network)
        worst = resp.routes[-1]
        await s.handle(f"{base}/route-selection", selection("dev-001", rid, str(worst.route_id)))
        lat, lon = node(fl_network, "ev-lal")
        clock.t = T0 + timedelta(seconds=10)
        await s.handle(f"{base}/telemetry", telemetry("dev-001", lat, lon, clock.t, speed=40.0))
        out = await s.suggest()
        rev = RouteResponse.model_validate_json(out[0][1])
        assert rev.revision == 1
        await s.store.close()

        # restart: a fresh service on the same database, nothing in memory
        store = Store.connect(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
        s2 = Service(store, Planner(fl_network, Settings()), clock=clock)
        await s2.start()
        assert s2.stats["selection_unknown_route"] == 0
        out = await s2.handle(f"{base}/route-selection", selection("dev-001", rid, str(rev.routes[0].route_id)))
        assert s2.stats["selection_unknown_route"] == 0
        assert [FlightStatus.model_validate_json(p).status for _, p in out] == ["replaced", "planned"]
        assert s2.flights["dev-001"]["path"][0] == "wp-lakeland"
        # the next revision continues from the stored ones instead of restarting at 1
        assert await s2._next_revision(rid) == 2
        await s2.store.close()

    asyncio.run(go())


def test_migration_0003_up_and_down_on_sqlite(tmp_path: Path):
    backend = Path(__file__).resolve().parents[1]
    db = tmp_path / "m.db"

    def tables() -> set[str]:
        with sqlite3.connect(db) as c:
            return {r[0] for r in c.execute("select name from sqlite_master where type='table'")}

    penv = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}"}
    for args in (["upgrade", "head"], ["downgrade", "0002"], ["upgrade", "head"]):
        r = subprocess.run(
            [sys.executable, "-m", "alembic", *args], cwd=backend, env=penv, capture_output=True, text=True
        )
        assert r.returncode == 0, r.stderr
        assert ("route_revisions" in tables()) == (args[0] == "upgrade")


async def _classical_flight(tmp_path: Path, net: Network):
    from test_service import T0

    s, clock, base, rid, resp = await one_flight(tmp_path, net)
    s.planner = Planner(net, Settings(method="classical", forecaster="cv"))
    rid = str(uuid4())
    out = await s.handle(f"{BASE}/route-request", route_request("dev-001", "ev-orl", request_id=rid))
    resp = RouteResponse.model_validate_json(out[0][1])
    await s.handle(f"{BASE}/route-selection", selection("dev-001", rid, str(resp.routes[0].route_id)))
    return s, clock, T0


@pytest.mark.parametrize("drift", [False, True])
def test_classical_off_route_uses_the_fixed_rule(tmp_path: Path, fl_network: Network, drift: bool):
    async def go():
        s, clock, T0 = await _classical_flight(tmp_path, fl_network)
        assert s.planner.method == "classical"
        path = s.flights["dev-001"]["path"]
        track = fly(fl_network, path, 200, drift_from=60 if drift else None)
        published, flagged_at = [], None
        for k, (lat, lon) in enumerate(track, start=1):
            clock.t = T0 + timedelta(seconds=k)
            await s.handle(f"{BASE}/telemetry", telemetry("dev-001", lat, lon, clock.t, speed=30.0))
            if k % 5 == 0:
                published += await s.scan_off_route()
                if flagged_at is None and s.flights["dev-001"]["status"] == "off_route":
                    flagged_at = k
        await s.store.close()
        return s, flagged_at, published

    s, flagged_at, out = asyncio.run(go())
    if not drift:
        assert flagged_at is None and out == []
        return
    assert flagged_at is not None and flagged_at > 60
    assert s.live.tracks["dev-001"].off_route is True
    statuses = [FlightStatus.model_validate_json(p).status for t, p in out if t.endswith("/flight-status")]
    assert statuses == ["off_route"]
    responses = [RouteResponse.model_validate_json(p) for t, p in out if t.endswith("/route-response")]
    assert len(responses) == 1 and responses[0].status == "ok" and responses[0].revision == 1
    assert responses[0].summary and responses[0].summary.startswith("Off route:")
    assert s.stats["offroute_recalculated"] == 1


def test_classical_rule_flags_a_late_flight_and_a_far_one(fl_network: Network):
    p = Planner(fl_network, Settings(method="classical", forecaster="cv"))
    path = p.network.shortest_paths("ev-lal", "ev-orl", vehicle_type="passenger", k=1)[0]
    lat, lon = node(fl_network, path[1])
    assert p.classical_off_route(path, lat, lon, 1000.0, 1000.0 + 59.0) is False
    assert p.classical_off_route(path, lat, lon, 1000.0 + 61.0, 1000.0) is True  # past the +60 s window
    assert p.classical_off_route(path, lat + 0.004, lon, 1000.0, None) is True  # ~445 m away
    assert p.classical_off_route(path, lat + 0.002, lon, 1000.0, None) is False  # ~222 m away


# --- unit watchdog: silence is the heartbeat (PLAN E6.8: unplugging shows "Unit not connected" within 5 s) -----


class FakeMono:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def _bad_fix(rig: Rig, did: str) -> bytes:
    lat, lon = rig.network.nodes["ev-sef"]["lat"], rig.network.nodes["ev-sef"]["lon"]
    msg = json.loads(telemetry(did, lat, lon, datetime.now(UTC)))
    msg["fix"] = {"type": "none", "hdop": 99.0, "satellites": 0, "replayed": False}
    return json.dumps(msg).encode()


def test_silent_unit_goes_offline_without_a_last_will_and_comes_back(env: Env, rig: Rig):
    clock = FakeMono()
    rig.link.mono = clock
    rig.pair("dev-001", env.codes["dev-001"])
    app_state(rig.c).ws_heartbeat_s = 30.0
    rig.online("dev-001")
    check = lambda: portal_of(rig.c).call(rig.link.check_silence)  # noqa: E731
    with rig.ws("/ws/devices/dev-001") as ws:
        assert ws.receive_json()["payload"]["online"] is True
        clock.t += 2.0
        assert check() == []  # not silent long enough
        clock.t += 1.5  # 3.5 s without a message (limit 3 s): no Will, no disconnect
        assert check() == ["dev-001"]
        ev = ws.receive_json()
        assert ev["type"] == "offline" and ev["payload"]["reason"] == "no_signal"
        assert rig.c.get("/api/devices/dev-001").json()["online"] is False
        r = rig.c.post("/api/devices/dev-001/route-request", json={"destination": "ev-orl"})
        assert r.status_code == 409 and err(r) == "unit_offline"
        assert check() == []  # reported once

        rig.online("dev-001")  # resumes
        assert ws.receive_json()["type"] == "online"
        assert rig.c.get("/api/devices/dev-001").json()["online"] is True
        assert rig.c.post("/api/devices/dev-001/route-request", json={"destination": "ev-orl"}).status_code == 200


def test_telemetry_with_a_bad_fix_still_proves_the_unit_is_alive(env: Env, rig: Rig):
    clock = FakeMono()
    rig.link.mono = clock
    rig.pair("dev-001", env.codes["dev-001"])
    rig.online("dev-001")
    for _ in range(4):  # 8 s of nothing but unusable fixes
        clock.t += 2.0
        rig.device("dev-001", "telemetry", _bad_fix(rig, "dev-001"))
        assert portal_of(rig.c).call(rig.link.check_silence) == []
    assert rig.c.get("/api/devices/dev-001").json()["online"] is True
    # silent after a bad fix, then a bad fix again: back online (not only a good one)
    clock.t += 5.0
    assert portal_of(rig.c).call(rig.link.check_silence) == ["dev-001"]
    rig.device("dev-001", "telemetry", _bad_fix(rig, "dev-001"))
    assert rig.c.get("/api/devices/dev-001").json()["online"] is True


def test_last_will_is_not_reported_twice_by_the_watchdog(env: Env, rig: Rig):
    clock = FakeMono()
    rig.link.mono = clock
    rig.online("dev-001")
    rig.device("dev-001", "status", b"offline")
    clock.t += 10.0
    assert portal_of(rig.c).call(rig.link.check_silence) == []


def test_watchdog_defaults_and_env(monkeypatch):
    assert Settings().unit_silence_s == 3.0 and Settings().ws_heartbeat_s == 5.0
    monkeypatch.setenv("UNIT_SILENCE_SECONDS", "2.5")
    monkeypatch.setenv("WS_HEARTBEAT_SECONDS", "1")
    s = Settings.from_env()
    assert s.unit_silence_s == 2.5 and s.ws_heartbeat_s == 1.0


# --- live ADS-B background traffic enters the forecast (never routed, never registered) ---------------------------
def test_adsb_background_traffic_is_added_to_the_forecast(fl_network: Network):
    from aerolink.adsb_live.state import background_mass

    lat, lon = node(fl_network, "ev-lal")

    class FakeFeed:  # no network: the same call the real AdsbFeed makes
        def background_mass(self, network, t_s):
            return background_mass([{"lat": lat, "lon": lon, "gs_kt": 120.0}], network, t_s)

    p = Planner(fl_network, Settings(forecaster="cv"))
    t = 1_800_000_000.0
    before = p.engine.off_route_mass(t)
    assert before == []
    p.adsb = FakeFeed()
    mass = p.engine.off_route_mass(t)
    assert mass and all(len(m) == 5 for m in mass)
    p.adsb = None
    assert p.engine.off_route_mass(t) == []  # switching the feed off removes it again
