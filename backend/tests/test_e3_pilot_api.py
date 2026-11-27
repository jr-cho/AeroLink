"""E3: pairing, screen sessions, pilot endpoints and the WebSocket hub (PLAN E6.8 backend part).

Runs the real FastAPI app on SQLite with ``mqtt=False``; device messages are fed through an
``MqttLink`` that has no broker connection, which is exactly the path real MQTT messages take.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from conftest import app_state, need, portal_of
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_service import telemetry

from aerolink.network import Network
from aerolink.service import pairing
from aerolink.service.app import create_app
from aerolink.service.devices import provision, set_new_code
from aerolink.service.hub import Hub
from aerolink.service.mqtt import MqttLink
from aerolink.service.settings import REPO, Settings
from aerolink.service.store import Store

HTTPS = "https://testserver"
BACKEND = Path(__file__).resolve().parents[1]


def node(network: Network, nid: str) -> tuple[float, float]:
    return network.nodes[nid]["lat"], network.nodes[nid]["lon"]


class Env:
    """An app on a temp SQLite database with two registered units and their pairing codes."""

    def __init__(self, tmp_path: Path):
        self.url = f"sqlite+aiosqlite:///{tmp_path / 'e3.db'}"
        self.tmp = tmp_path
        self.codes: dict[str, str] = {}

        async def prepare() -> None:
            store = Store.connect(self.url)
            await store.create_all()
            for did, vt, demo in (("dev-001", "passenger", False), ("dev-002", "medical", True)):
                r = await provision(store, did, vt, demo, "https://aerolink.test", tmp_path / "labels")
                self.codes[did] = r["code"]
            await store.close()

        asyncio.run(prepare())

    def app(self):
        return create_app(Settings(database_url=self.url, forecaster="cv", suggestion_interval_s=0), mqtt=False)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


class Rig:
    """A TestClient plus a way to feed the device side (status, telemetry) into the running app."""

    def __init__(self, c: TestClient, network: Network):
        self.c, self.network = c, network
        self.link = portal_of(c).call(self._make_link)
        app_state(c).link = self.link  # so pilot publishes go through the same hub as MQTT would

    async def _make_link(self) -> MqttLink:
        st = app_state(self.c)
        return MqttLink(Settings(), st.service, st.hub)

    def device(self, did: str, topic: str, payload: bytes) -> None:
        portal_of(self.c).call(self.link._dispatch, f"aerolink/aircraft/{did}/{topic}", payload)

    def online(self, did: str, vt: str = "passenger", at: str = "ev-sef") -> None:
        self.device(did, "status", b"online")
        lat, lon = node(self.network, at)
        self.device(did, "telemetry", telemetry(did, lat, lon, datetime.now(UTC), vt=vt))

    def ws(self, path: str):
        """Open a WebSocket carrying this client's cookies (Starlette's test client doesn't send them itself)."""
        cookie = "; ".join(f"{k}={v}" for k, v in self.c.cookies.items())
        return self.c.websocket_connect(path, headers={"cookie": cookie} if cookie else {})

    def pair(self, did: str, code: str):
        return self.c.post(f"/api/devices/{did}/pair", json={"code": code})


@pytest.fixture
def rig(env: Env, fl_network: Network):
    with TestClient(env.app(), base_url=HTTPS) as c:
        yield Rig(c, fl_network)


def err(r) -> str:
    return r.json()["detail"]["error"]


# --- pairing ----------------------------------------------------------------------------------
def test_pair_right_and_wrong_code(env: Env, rig: Rig):
    assert rig.pair("dev-001", "AAAAAAAAAA").status_code == 403
    assert rig.pair("nope-000", env.codes["dev-001"]).status_code == 403  # unknown unit: same answer
    r = rig.pair("dev-001", env.codes["dev-001"].lower())  # typed by hand
    assert r.status_code == 200 and r.json()["device_id"] == "dev-001"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert rig.c.get("/api/devices/dev-001").status_code == 200


def test_session_is_per_device_and_required(env: Env, rig: Rig):
    assert err(rig.c.get("/api/devices/dev-001")) == "session_required"
    assert rig.pair("dev-001", env.codes["dev-001"]).status_code == 200
    assert rig.c.get("/api/devices/dev-002").status_code == 401  # dev-001's session is not dev-002's
    for path, body in (
        ("route-request", {"destination": "ev-orl"}),
        ("route-selection", {"request_id": str(uuid4()), "route_id": str(uuid4())}),
    ):
        assert rig.c.post(f"/api/devices/dev-002/{path}", json=body).status_code == 401


def test_pairing_is_rate_limited(env: Env, rig: Rig):
    for _ in range(pairing.MAX_FAILURES):
        assert rig.pair("dev-001", "BBBBBBBBBB").status_code == 403
    r = rig.pair("dev-001", env.codes["dev-001"])  # even the right code waits
    assert r.status_code == 429 and err(r) == "too_many_attempts"
    assert rig.pair("dev-002", env.codes["dev-002"]).status_code == 200  # other units unaffected


def test_code_is_stored_only_as_a_hash_and_never_listed(env: Env, rig: Rig):
    db = sqlite3.connect(env.tmp / "e3.db")
    row = db.execute("select pairing_hash, pairing_salt from devices where id='dev-001'").fetchone()
    assert env.codes["dev-001"] not in "".join(row) and len(row[0]) == 64
    listed = json.dumps(rig.c.get("/api/devices").json())
    assert "pairing" not in listed and row[0] not in listed


def test_several_screens_and_sessions_survive_restart(env: Env, fl_network: Network):
    with TestClient(env.app(), base_url=HTTPS) as c1:
        assert c1.post("/api/devices/dev-001/pair", json={"code": env.codes["dev-001"]}).status_code == 200
        with TestClient(env.app(), base_url=HTTPS) as c2:  # a second screen, even on a second backend process
            assert c2.get("/api/devices/dev-001").status_code == 401
            assert c2.post("/api/devices/dev-001/pair", json={"code": env.codes["dev-001"]}).status_code == 200
            assert c1.get("/api/devices/dev-001").status_code == 200
        cookies = c1.cookies
    with TestClient(env.app(), base_url=HTTPS, cookies=cookies) as c3:  # backend restarted
        assert c3.get("/api/devices/dev-001").status_code == 200


def test_rotate_ends_sessions_and_closes_sockets(env: Env, rig: Rig):
    rig.pair("dev-001", env.codes["dev-001"])
    app_state(rig.c).ws_heartbeat_s = 0.2
    with rig.ws("/ws/devices/dev-001") as ws:
        assert ws.receive_json()["type"] == "state"

        async def rotate() -> str | None:
            store = Store.connect(env.url)
            try:
                return await set_new_code(store, "dev-001")
            finally:
                await store.close()

        new = asyncio.run(rotate())
        assert new and new != env.codes["dev-001"]
        with pytest.raises(WebSocketDisconnect) as e:
            for _ in range(20):
                ws.receive_json()
        assert e.value.code == 4401
    assert rig.c.get("/api/devices/dev-001").status_code == 401
    assert rig.pair("dev-001", env.codes["dev-001"]).status_code == 403  # old code is dead
    assert rig.pair("dev-001", new).status_code == 200


# --- device state -----------------------------------------------------------------------------
def test_device_state(env: Env, rig: Rig):
    rig.pair("dev-001", env.codes["dev-001"])
    before = rig.c.get("/api/devices/dev-001").json()
    assert before["vehicle_type"] == "passenger" and before["online"] is False and before["latest_fix"] is None
    rig.online("dev-001")
    after = rig.c.get("/api/devices/dev-001").json()
    assert after["online"] is True and after["last_seen"] and after["latest_fix"]["lat"] > 0
    assert after["current_flight"] is None and after["demo"] is False


# --- routing ----------------------------------------------------------------------------------
def test_route_request_uses_registered_type(env: Env, rig: Rig):
    rig.pair("dev-001", env.codes["dev-001"])
    rig.online("dev-001")
    r = rig.c.post(
        "/api/devices/dev-001/route-request",
        json={"destination": "ev-orl", "vehicle_type": "medical", "aircraft_id": "dev-002"},
    )
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["aircraft_id"] == "dev-001"
    stored = rig.c.get(f"/api/route-requests/{r.json()['request_id']}").json()
    assert stored["vehicle_type"] == "passenger" and stored["aircraft_id"] == "dev-001"


def test_passenger_unit_cannot_get_medical_routes(env: Env, rig: Rig):
    rig.pair("dev-001", env.codes["dev-001"])
    rig.online("dev-001")
    medical_only = next(n for n in rig.network.nodes_for("medical") if "passenger" not in rig.network.serves(n))
    r = rig.c.post("/api/devices/dev-001/route-request", json={"destination": medical_only, "vehicle_type": "medical"})
    assert r.status_code == 200 and r.json()["status"] == "no_route" and r.json()["routes"] == []
    assert "does not serve passenger" in r.json()["reason"]


def test_route_request_is_idempotent_and_unit_scoped(env: Env, rig: Rig):
    rig.pair("dev-001", env.codes["dev-001"])
    rig.pair("dev-002", env.codes["dev-002"])
    rig.online("dev-001")
    rig.online("dev-002", vt="medical")
    rid = str(uuid4())
    first = rig.c.post("/api/devices/dev-001/route-request", json={"destination": "ev-orl", "request_id": rid}).json()
    again = rig.c.post("/api/devices/dev-001/route-request", json={"destination": "ev-orl", "request_id": rid}).json()
    assert again["routes"] == first["routes"] and again["request_id"] == rid
    other = rig.c.post("/api/devices/dev-002/route-request", json={"destination": "ev-orl", "request_id": rid})
    assert other.status_code == 409 and err(other) == "request_id_conflict"
    bad = rig.c.post("/api/devices/dev-001/route-request", json={"destination": "NOT A NODE"})
    assert bad.status_code == 422


def test_full_flow_over_http_and_websocket(env: Env, rig: Rig):
    rig.pair("dev-001", env.codes["dev-001"])
    rig.online("dev-001")
    with rig.ws("/ws/devices/dev-001") as ws:
        state = ws.receive_json()
        assert state["type"] == "state" and state["payload"]["id"] == "dev-001"

        resp = rig.c.post("/api/devices/dev-001/route-request", json={"destination": "ev-orl"}).json()
        ev = ws.receive_json()
        assert ev["type"] == "route-response" and ev["payload"]["request_id"] == resp["request_id"]

        route_id = resp["routes"][0]["route_id"]
        sel = {"request_id": resp["request_id"], "route_id": route_id}
        planned = rig.c.post("/api/devices/dev-001/route-selection", json=sel)
        assert planned.status_code == 200 and planned.json()["status"] == "planned"
        ev = ws.receive_json()
        assert ev["type"] == "flight-status" and ev["payload"]["flight_id"] == planned.json()["flight_id"]

        again = rig.c.post("/api/devices/dev-001/route-selection", json=sel)  # idempotent
        assert again.json()["flight_id"] == planned.json()["flight_id"]
        flights = [f for f in rig.c.get("/api/flights").json() if f["aircraft_id"] == "dev-001"]
        assert len(flights) == 1

        unknown = rig.c.post(
            "/api/devices/dev-001/route-selection", json={"request_id": resp["request_id"], "route_id": str(uuid4())}
        )
        assert unknown.status_code == 404 and err(unknown) == "unknown_route"

        # Whatever the backend publishes to the unit (here: traffic) also reaches the screen.
        portal_of(rig.c).call(rig.link.publish, app_state(rig.c).service.traffic_messages())
        ev = ws.receive_json()
        assert ev["type"] == "traffic" and ev["payload"]["aircraft_id"] == "dev-001"
        assert rig.c.get("/api/devices/dev-001").json()["current_flight"]["status"] == "planned"


def test_websocket_requires_session(env: Env, rig: Rig):
    with pytest.raises(WebSocketDisconnect), rig.ws("/ws/devices/dev-001"):
        pass
    rig.pair("dev-002", env.codes["dev-002"])
    with pytest.raises(WebSocketDisconnect), rig.ws("/ws/devices/dev-001"):  # wrong unit's session
        pass


def test_offline_pushes_within_5s_and_locks_routing(env: Env, rig: Rig):
    rig.pair("dev-001", env.codes["dev-001"])
    rig.online("dev-001")
    with rig.ws("/ws/devices/dev-001") as ws:
        ws.receive_json()
        t0 = time.monotonic()
        rig.device("dev-001", "status", b"offline")  # the broker's Last Will
        ev = ws.receive_json()
        assert ev["type"] == "offline" and time.monotonic() - t0 < 5
        assert rig.c.get("/api/devices/dev-001").json()["online"] is False
        r = rig.c.post("/api/devices/dev-001/route-request", json={"destination": "ev-orl"})
        assert r.status_code == 409 and err(r) == "unit_offline"
        r = rig.c.post(
            "/api/devices/dev-001/route-selection", json={"request_id": str(uuid4()), "route_id": str(uuid4())}
        )
        assert r.status_code == 409 and err(r) == "unit_offline"
        rig.device("dev-001", "status", b"online")
        assert ws.receive_json()["type"] == "online"
        assert rig.c.post("/api/devices/dev-001/route-request", json={"destination": "ev-orl"}).status_code == 200


def test_demo_type_change_is_pushed_within_2s(env: Env, rig: Rig):
    rig.pair("dev-002", env.codes["dev-002"])
    rig.online("dev-002", vt="medical")
    with rig.ws("/ws/devices/dev-002") as ws:
        assert ws.receive_json()["payload"]["vehicle_type"] == "medical"
        t0 = time.monotonic()
        lat, lon = node(rig.network, "ev-sef")
        rig.device("dev-002", "telemetry", telemetry("dev-002", lat, lon, datetime.now(UTC), vt="passenger"))
        ev = ws.receive_json()
        assert ev == {"type": "vehicle-type", "payload": {"vehicle_type": "passenger", "demo": True}}
        assert time.monotonic() - t0 < 2
        assert rig.c.get("/api/devices/dev-002").json()["vehicle_type"] == "passenger"
        # the next route request now uses the new registered type
        r = rig.c.post("/api/devices/dev-002/route-request", json={"destination": "ev-orl"})
        assert r.json()["status"] == "ok"


def test_non_demo_type_change_is_not_pushed(env: Env, rig: Rig):
    rig.pair("dev-001", env.codes["dev-001"])
    rig.online("dev-001")
    app_state(rig.c).ws_heartbeat_s = 0.2
    with rig.ws("/ws/devices/dev-001") as ws:
        ws.receive_json()
        lat, lon = node(rig.network, "ev-sef")
        rig.device("dev-001", "telemetry", telemetry("dev-001", lat, lon, datetime.now(UTC), vt="medical"))
        assert ws.receive_json()["type"] == "heartbeat"  # rejected telemetry produced no event
        assert rig.c.get("/api/devices/dev-001").json()["vehicle_type"] == "passenger"


def test_heartbeat_reports_online(env: Env, rig: Rig):
    rig.pair("dev-001", env.codes["dev-001"])
    app_state(rig.c).ws_heartbeat_s = 0.2
    with rig.ws("/ws/devices/dev-001") as ws:
        ws.receive_json()
        hb = ws.receive_json()
        assert hb["type"] == "heartbeat" and hb["payload"]["online"] is False


# --- hub, provisioning, migration ---------------------------------------------------------------
def test_hub_routes_by_unit_and_drops_oldest():
    async def go() -> None:
        hub = Hub()
        a, b = hub.subscribe("dev-001"), hub.subscribe("dev-002")
        hub.publish_messages([("aerolink/aircraft/dev-001/traffic", b'{"x": 1}')])
        hub.publish_messages([("aerolink/aircraft/dev-001/telemetry", b"{}")])  # inbound kinds are not relayed
        assert a.get_nowait() == {"type": "traffic", "payload": {"x": 1}} and a.empty() and b.empty()
        for i in range(500):
            hub.publish("dev-002", "traffic", {"i": i})
        assert b.qsize() == 200 and b.get_nowait()["payload"]["i"] == 300
        hub.unsubscribe("dev-001", a)
        assert hub.subscribers("dev-001") == 0

    asyncio.run(go())


def test_provisioning_writes_labels_and_hash_only(tmp_path: Path):
    async def go() -> None:
        store = Store.connect(f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
        await store.create_all()
        r = await provision(store, "dev-009", "medical", True, "aerolink.example/", tmp_path / "lbl")
        assert r["url"] == f"https://aerolink.example/device/dev-009?code={r['code']}" and len(r["code"]) == 10
        assert sorted(p.suffix for p in r["label_files"]) == [".png", ".svg"] and all(
            p.stat().st_size for p in r["label_files"]
        )
        h, salt = need(await store.get_pairing("dev-009"))
        assert pairing.verify(r["code"], h, salt) and not pairing.verify("AAAAAAAAAA", h, salt)
        dev = need(await store.get_device("dev-009"))
        assert dev["vehicle_type"] == "medical" and dev["demo"] and "pairing_hash" not in dev
        assert await set_new_code(store, "nobody-1") is None
        await store.close()

    asyncio.run(go())


def test_migration_0002_upgrade_and_downgrade(tmp_path: Path):
    db = tmp_path / "m.db"
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")

    def tables_and_cols() -> tuple[set[str], set[str]]:
        con = sqlite3.connect(db)
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        cols = {r[1] for r in con.execute("pragma table_info(devices)")}
        con.close()
        return tables, cols

    command.upgrade(cfg, "head")
    tables, cols = tables_and_cols()
    assert "screen_sessions" in tables and {"pairing_hash", "pairing_salt"} <= cols
    command.downgrade(cfg, "0001")
    tables, cols = tables_and_cols()
    assert "screen_sessions" not in tables and "pairing_hash" not in cols and "devices" in tables
    command.upgrade(cfg, "head")
    assert "screen_sessions" in tables_and_cols()[0]
    assert REPO.exists()


def _planned_flight(rig: Rig, env: Env) -> dict:
    rig.pair("dev-001", env.codes["dev-001"])
    rig.online("dev-001")
    resp = rig.c.post("/api/devices/dev-001/route-request", json={"destination": "ev-orl"}).json()
    sel = {"request_id": resp["request_id"], "route_id": resp["routes"][0]["route_id"]}
    return rig.c.post("/api/devices/dev-001/route-selection", json=sel).json()


def test_flight_end_ends_publishes_and_is_idempotent(env: Env, rig: Rig):
    planned = _planned_flight(rig, env)
    with rig.ws("/ws/devices/dev-001") as ws:
        ws.receive_json()
        r = rig.c.post("/api/devices/dev-001/flight/end")
        assert r.status_code == 200
        body = r.json()
        assert (
            body["status"] == "ended"
            and body["flight_id"] == planned["flight_id"]
            and body["reason"] == "ended by pilot"
        )
        ev = ws.receive_json()
        assert ev["type"] == "flight-status" and ev["payload"]["status"] == "ended"
    assert rig.c.get("/api/devices/dev-001").json()["current_flight"] is None
    assert not [f for f in rig.c.get("/api/flights", params={"active": True}).json() if f["aircraft_id"] == "dev-001"]
    again = rig.c.post("/api/devices/dev-001/flight/end")  # idempotent
    assert again.status_code == 200 and again.json()["status"] == "ended"


def test_flight_end_needs_the_right_session(env: Env, rig: Rig):
    assert rig.c.post("/api/devices/dev-001/flight/end").status_code == 401
    rig.pair("dev-002", env.codes["dev-002"])
    r = rig.c.post("/api/devices/dev-001/flight/end")  # another unit's session
    assert r.status_code == 401 and err(r) == "session_required"


def test_flight_end_is_locked_while_offline(env: Env, rig: Rig):
    _planned_flight(rig, env)
    rig.device("dev-001", "status", b"offline")
    r = rig.c.post("/api/devices/dev-001/flight/end")
    assert r.status_code == 409 and err(r) == "unit_offline"
