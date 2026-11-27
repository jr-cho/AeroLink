"""Service layer (PLAN E6.1-E6.3): device flow, registry rules, persistence, API.

Runs against SQLite and calls ``Service.handle`` directly, so no broker or Postgres is needed.
``test_service_mqtt.py`` covers the real broker.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aerolink.contracts import FlightStatus, RouteResponse, Traffic
from aerolink.network import Network
from aerolink.service.app import create_app
from aerolink.service.core import Service
from aerolink.service.live import LiveState
from aerolink.service.planner import Planner
from aerolink.service.settings import Settings
from aerolink.service.store import Store
from aerolink.service.topics import parse

T0 = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)


class Clock:
    def __init__(self, t: datetime = T0):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def run(coro):
    return asyncio.run(coro)


def telemetry(
    aircraft_id: str, lat: float, lon: float, t: datetime, vt: str = "passenger", speed: float = 0.0
) -> bytes:
    return json.dumps(
        {
            "schema_version": "4.0",
            "message_id": str(uuid4()),
            "aircraft_id": aircraft_id,
            "vehicle_type": vt,
            "timestamp": t.isoformat(),
            "position": {"lat": lat, "lon": lon, "alt_msl_m": 30.0},
            "motion": {"ground_speed_mps": speed, "course_deg": 0.0, "vertical_speed_mps": 0.0},
            "fix": {"type": "3d", "hdop": 0.9, "satellites": 11, "replayed": False},
            "active_flight_id": None,
        }
    ).encode()


def route_request(aircraft_id: str, dest: str, vt: str = "passenger", request_id: str | None = None) -> bytes:
    return json.dumps(
        {
            "schema_version": "4.0",
            "message_id": str(uuid4()),
            "request_id": request_id or str(uuid4()),
            "aircraft_id": aircraft_id,
            "vehicle_type": vt,
            "timestamp": T0.isoformat(),
            "destination_node": dest,
        }
    ).encode()


def selection(aircraft_id: str, request_id: str, route_id: str) -> bytes:
    return json.dumps(
        {
            "schema_version": "4.0",
            "message_id": str(uuid4()),
            "request_id": request_id,
            "aircraft_id": aircraft_id,
            "route_id": route_id,
            "timestamp": T0.isoformat(),
        }
    ).encode()


async def make_service(tmp_path: Path, network: Network, clock: Clock) -> Service:
    store = Store.connect(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await store.create_all()
    service = Service(store, Planner(network, Settings()), clock=clock)
    await service.start()
    return service


def node(network: Network, nid: str) -> tuple[float, float]:
    return network.nodes[nid]["lat"], network.nodes[nid]["lon"]


# --- pure pieces ------------------------------------------------------------------------------
def test_topics_parse_and_reject():
    t = parse("aerolink/aircraft/dev-001/route-request")
    assert t and (t.source, t.aircraft_id, t.kind) == ("aircraft", "dev-001", "route-request")
    sim = parse("aerolink/sim/sim-003/telemetry")
    assert sim is not None and sim.source == "sim"
    for bad in ("aerolink/other/x/telemetry", "aerolink/aircraft/x/unknown", "foo/aircraft/x/telemetry", "a/b"):
        assert parse(bad) is None


def test_live_state_drops_duplicates_out_of_order_and_poor_fixes():
    from aerolink.contracts import Telemetry

    live = LiveState()
    m1 = Telemetry.model_validate_json(telemetry("dev-001", 27.5, -81.3, T0))
    assert live.accept("aircraft", m1) is None
    assert live.accept("aircraft", m1) == "duplicate"
    older = Telemetry.model_validate_json(telemetry("dev-001", 27.5, -81.3, T0 - timedelta(seconds=1)))
    assert live.accept("aircraft", older) == "out_of_order"
    bad = json.loads(telemetry("dev-001", 27.5, -81.3, T0 + timedelta(seconds=1)))
    bad["fix"]["hdop"] = 9.0
    assert live.accept("aircraft", Telemetry.model_validate_json(json.dumps(bad))) == "poor_fix"


# --- device flow ------------------------------------------------------------------------------------
def test_full_device_flow_and_restart(tmp_path: Path, fl_network: Network):
    async def go():
        clock = Clock()
        s = await make_service(tmp_path, fl_network, clock)
        await s.store.register_device("dev-001", "passenger")
        lat, lon = node(fl_network, "ev-sef")
        base = "aerolink/aircraft/dev-001"

        assert await s.handle(f"{base}/telemetry", telemetry("dev-001", lat, lon, T0)) == []
        assert s.live.tracks["dev-001"].last.position.lat == lat

        rid = str(uuid4())
        out = await s.handle(f"{base}/route-request", route_request("dev-001", "ev-orl", request_id=rid))
        assert [t for t, _ in out] == [f"{base}/route-response"]
        resp = RouteResponse.model_validate_json(out[0][1])
        assert resp.status == "ok" and 1 <= len(resp.routes) <= 3 and resp.method == "aerolink"
        assert resp.routes[0].eta[-1].node == "ev-orl" and len(resp.routes[0].waypoints) >= 2

        again = await s.handle(f"{base}/route-request", route_request("dev-001", "ev-orl", request_id=rid))
        assert RouteResponse.model_validate_json(again[0][1]).routes == resp.routes  # idempotent

        out = await s.handle(f"{base}/route-selection", selection("dev-001", rid, str(resp.routes[0].route_id)))
        status = FlightStatus.model_validate_json(out[-1][1])
        assert out[-1][0] == f"{base}/flight-status" and status.status == "planned"
        flight_id = str(status.flight_id)

        # Restart: a new Service on the same database still has the planned flight.
        s2 = Service(s.store, s.planner, clock=clock)
        await s2.start()
        assert s2.flights["dev-001"]["flight_id"] == flight_id
        assert s2.flights["dev-001"]["path"][0] == "ev-sef" and s2.flights["dev-001"]["path"][-1] == "ev-orl"

        clock.t = T0 + timedelta(seconds=10)
        out = await s2.handle(f"{base}/telemetry", telemetry("dev-001", lat, lon, clock.t, speed=40.0))
        assert FlightStatus.model_validate_json(out[0][1]).status == "active"

        dlat, dlon = node(fl_network, "ev-orl")
        clock.t = T0 + timedelta(minutes=40)
        out = await s2.handle(f"{base}/telemetry", telemetry("dev-001", dlat, dlon, clock.t, speed=0.0))
        assert FlightStatus.model_validate_json(out[0][1]).status == "ended"
        assert (await s2.store.flights())[0]["status"] == "ended" and "dev-001" not in s2.flights
        await s.store.close()

    run(go())


def test_passenger_device_cannot_request_medical_routes(tmp_path: Path, fl_network: Network):
    async def go():
        s = await make_service(tmp_path, fl_network, Clock())
        await s.store.register_device("dev-001", "passenger")
        hp = fl_network.nodes_for("medical")[0]
        out = await s.handle("aerolink/aircraft/dev-001/route-request", route_request("dev-001", hp, vt="medical"))
        resp = RouteResponse.model_validate_json(out[0][1])
        assert resp.status == "no_route" and "registered as passenger" in (resp.reason or "")
        # ... and its medical-typed telemetry is ignored.
        lat, lon = node(fl_network, "ev-sef")
        await s.handle("aerolink/aircraft/dev-001/telemetry", telemetry("dev-001", lat, lon, T0, vt="medical"))
        assert "dev-001" not in s.live.tracks
        await s.store.close()

    run(go())


def test_demo_device_may_switch_type(tmp_path: Path, fl_network: Network):
    async def go():
        s = await make_service(tmp_path, fl_network, Clock())
        await s.store.register_device("dev-002", "passenger", demo=True)
        lat, lon = node(fl_network, "ev-sef")
        await s.handle("aerolink/aircraft/dev-002/telemetry", telemetry("dev-002", lat, lon, T0, vt="medical"))
        dev = await s.store.get_device("dev-002")
        assert dev is not None and dev["vehicle_type"] == "medical"
        await s.store.close()

    run(go())


def test_unregistered_and_spoofed_messages_are_rejected(tmp_path: Path, fl_network: Network):
    async def go():
        s = await make_service(tmp_path, fl_network, Clock())
        lat, lon = node(fl_network, "ev-sef")
        await s.handle("aerolink/aircraft/dev-404/telemetry", telemetry("dev-404", lat, lon, T0))
        await s.store.register_device("dev-001", "passenger")
        await s.handle("aerolink/aircraft/dev-001/telemetry", telemetry("dev-002", lat, lon, T0))
        await s.handle("aerolink/aircraft/dev-001/telemetry", b"{not json")
        assert s.live.tracks == {}
        assert s.stats["telemetry_rejected"] == 1 and s.stats["topic_mismatch"] == 1
        assert s.stats["invalid_telemetry"] == 1
        # Simulator aircraft need no registration.
        await s.handle("aerolink/sim/sim-001/telemetry", telemetry("sim-001", lat, lon, T0))
        assert s.live.tracks["sim-001"].source == "sim"
        await s.store.close()

    run(go())


def test_no_route_without_a_position_fix(tmp_path: Path, fl_network: Network):
    async def go():
        s = await make_service(tmp_path, fl_network, Clock())
        await s.store.register_device("dev-001", "passenger")
        out = await s.handle("aerolink/aircraft/dev-001/route-request", route_request("dev-001", "ev-orl"))
        resp = RouteResponse.model_validate_json(out[0][1])
        assert resp.status == "no_route" and "position" in (resp.reason or "")
        await s.store.close()

    run(go())


def test_traffic_messages_cover_nearby_aircraft_and_route_edges(tmp_path: Path, fl_network: Network):
    async def go():
        clock = Clock()
        s = await make_service(tmp_path, fl_network, clock)
        await s.store.register_device("dev-001", "passenger")
        lat, lon = node(fl_network, "ev-sef")
        await s.handle("aerolink/aircraft/dev-001/telemetry", telemetry("dev-001", lat, lon, T0))
        sim_fix = telemetry("sim-001", lat + 0.01, lon, T0, speed=20.0)  # airborne: parked aircraft are no traffic
        await s.handle("aerolink/sim/sim-001/telemetry", sim_fix)
        rid = str(uuid4())
        out = await s.handle(
            "aerolink/aircraft/dev-001/route-request", route_request("dev-001", "ev-orl", request_id=rid)
        )
        resp = RouteResponse.model_validate_json(out[0][1])
        await s.handle(
            "aerolink/aircraft/dev-001/route-selection", selection("dev-001", rid, str(resp.routes[0].route_id))
        )

        msgs = dict(s.traffic_messages())
        mine = Traffic.model_validate_json(msgs["aerolink/aircraft/dev-001/traffic"])
        assert [a.id for a in mine.aircraft] == ["sim-001"]
        assert mine.edges and mine.edges[0].edge.startswith("ev-sef->")
        assert "aerolink/sim/sim-001/traffic" in msgs

        clock.t = T0 + timedelta(minutes=5)  # silent for > 30 s: no more updates
        assert s.traffic_messages() == []
        await s.store.close()

    run(go())


# --- HTTP API -------------------------------------------------------------------------------------------
def test_api_health_plan_airspace_and_method(tmp_path: Path):
    async def prepare() -> Store:
        store = Store.connect(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
        await store.create_all()
        await store.close()
        return store

    url = f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"
    run(prepare())
    app = create_app(Settings(database_url=url), mqtt=False)
    with TestClient(app) as c:
        from test_g2_auth import operator_login

        operator_login(c)  # the PUTs below need an operator session (G2)
        assert c.get("/healthz").json()["ok"] is True
        body = c.post(
            "/api/plan", json={"vehicle_type": "passenger", "origin_node": "ev-sef", "destination_node": "ev-orl"}
        )
        assert body.status_code == 200 and body.json()["status"] == "ok"
        assert c.post("/api/plan", json={"vehicle_type": "car", "destination_node": "ev-orl"}).status_code == 422
        geo = c.get("/api/airspace")
        assert geo.status_code == 200 and geo.json()["type"] == "FeatureCollection"
        assert c.put("/api/method", json={"method": "classical"}).json() == {"method": "classical"}
        assert c.get("/api/state").json() == {"aircraft": [], "method": "classical"}
        assert c.get("/api/route-requests/nope").status_code == 404
        assert c.get("/api/devices").json() == []

        models = c.get("/api/models")
        assert models.status_code == 200
        models_json = models.json()
        assert models_json["method"] == "classical"
        assert models_json["default_forecaster"] == "chronos"
        assert any(m["name"] == "chronos" for m in models_json["models"])

        assert c.put("/api/forecaster", json={"forecaster": "cv"}).json() == {"forecaster": "cv"}
        assert c.get("/api/models").json()["forecaster"].startswith("cv")

        eval_resp = c.get("/api/eval")
        assert eval_resp.status_code == 200 and "scenarios" in eval_resp.json()

        traffic = c.get("/api/traffic")
        assert traffic.status_code == 200
        assert traffic.json()["nodes"]["ev-sef"]["now"] in ("low", "moderate", "heavy")

        assert c.put("/api/scenario", json={"scenario": "bad"}).status_code == 422  # not in backend/scenarios
        assert c.put("/api/scenario", json={"scenario": "nominal_day"}).status_code == 503  # mqtt disabled in test


def test_snap_respects_vehicle_type(fl_network: Network):
    p = Planner(fl_network, Settings())
    lat, lon = node(fl_network, "ev-sef")
    assert p.snap(lat, lon, "passenger") == "ev-sef"
    med = p.snap(lat, lon, "medical")
    assert med is not None and med != "ev-sef"
    assert "waypoint" in fl_network.nodes[med]["types"] or "medical" in fl_network.serves(med)
    dest = next(n for n in fl_network.nodes_for("medical") if n != med and fl_network.is_reachable(med, n))
    resp = p.plan(
        request_id=uuid4(), aircraft_id="dev-001", vehicle_type="medical", origin=med,
        destination=dest, now=T0, planned=[],
    )  # fmt: skip
    assert resp.status == "ok"


@pytest.mark.parametrize("method", ["aerolink", "classical"])
def test_planner_methods_produce_valid_responses(fl_network: Network, method: str):
    p = Planner(fl_network, Settings(method=method))
    resp = p.plan(
        request_id=uuid4(), aircraft_id="dev-001", vehicle_type="medical", origin="ev-sef",
        destination=fl_network.nodes_for("medical")[0], now=T0, planned=[],
    )  # fmt: skip
    RouteResponse.model_validate_json(resp.model_dump_json())  # round-trips through the contract
    assert resp.method == method


def utc_now() -> datetime:
    return datetime.now(UTC)


def test_suggestion_loop_calls_planner_suggest_and_publishes_response(tmp_path: Path, fl_network: Network):
    async def go():
        clock = Clock()
        s = await make_service(tmp_path, fl_network, clock)
        await s.store.register_device("dev-001", "passenger", demo=True)
        lat, lon = node(fl_network, "ev-sef")
        base = "aerolink/aircraft/dev-001"
        await s.handle(f"{base}/telemetry", telemetry("dev-001", lat, lon, T0))
        rid = str(uuid4())
        out = await s.handle(f"{base}/route-request", route_request("dev-001", "ev-orl", request_id=rid))
        resp = RouteResponse.model_validate_json(out[0][1])
        await s.handle(f"{base}/route-selection", selection("dev-001", rid, str(resp.routes[0].route_id)))
        # mark active
        clock.t = T0 + timedelta(seconds=10)
        await s.handle(f"{base}/telemetry", telemetry("dev-001", lat, lon, clock.t, speed=40.0))
        assert s.flights["dev-001"]["status"] == "active"

        # Patch suggest() to always return a revision; this is a unit test of the loop wiring.
        called_with = {}

        def fake_suggest(aircraft_id, from_node, vehicle_type, planned, **_):
            called_with["args"] = (aircraft_id, from_node, vehicle_type)
            return s.planner.plan(
                request_id=UUID(s.flights["dev-001"]["request_id"]),
                aircraft_id=aircraft_id,
                vehicle_type=vehicle_type,
                origin=from_node,
                destination=s.flights["dev-001"]["destination"],
                now=clock.t,
                planned=[],
                revision=2,
            )

        cast(Any, s.planner).suggest = fake_suggest  # deliberate stub of the planner
        clock.t = T0 + timedelta(seconds=20)
        suggestions = await s.suggest()
        assert len(suggestions) == 1
        topic, payload = suggestions[0]
        assert topic == f"{base}/route-response"
        revised = RouteResponse.model_validate_json(payload)
        assert revised.revision == 2
        assert revised.status == "ok"
        assert called_with["args"][0] == "dev-001"

    run(go())
