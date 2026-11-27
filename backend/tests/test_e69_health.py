"""E6.9: unit health, watchdogs, event log and alerts, track segments, migration 0004, fake unit.

Most tests drive ``Service`` with a fake clock (no sleeping); the broker tests use a real Mosquitto + the project ACL.
"""

# ruff: noqa: F811  (pytest fixtures imported from test_e3_pilot_api)
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import aiomqtt
import pytest
from conftest import EXAMPLES, app_state, need, portal_of, validator
from test_e2_backend import fly
from test_e3_pilot_api import Env, Rig, env, rig  # noqa: F401
from test_f1_followups import FakeMono, _classical_flight
from test_g2_auth import operator_login
from test_service import T0, Clock, make_service, telemetry
from test_service_mqtt import USERS, free_port

from aerolink.adsb_live import AdsbFeed, AdsbSettings
from aerolink.contracts import Health
from aerolink.network import Network
from aerolink.service.live import SEGMENT_GAP_S
from aerolink.service.mqtt import MqttLink
from aerolink.service.settings import REPO, Settings
from aerolink.service.store import Store
from aerolink.service.watchdogs import Watchdogs

BASE = "aerolink/aircraft/dev-001"
HAVE_BROKER = shutil.which("mosquitto") is not None
needs_broker = pytest.mark.skipif(not HAVE_BROKER, reason="mosquitto not installed (PATH=/opt/homebrew/sbin:$PATH)")


def load_fake_unit():
    spec = importlib.util.spec_from_file_location("fake_unit", REPO / "scripts" / "fake_unit.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fake_unit"] = mod
    spec.loader.exec_module(mod)
    return mod


def health_msg(did: str = "dev-001", at: datetime = T0, rssi: int = -60, fix: str = "3d", **extra) -> bytes:
    return json.dumps(
        {
            "schema_version": "4.1",
            "message_id": str(uuid4()),
            "aircraft_id": did,
            "timestamp": at.isoformat(),
            "uptime_s": 100.0,
            "reset_reason": "power_on",
            "wifi_rssi_dbm": rssi,
            "gnss": {"fix": fix, "satellites": 0 if fix == "none" else 10, "hdop": 0.9},
            "free_heap_bytes": 150000,
            "firmware_version": "1.0.0",
            **extra,
        }
    ).encode()


def tel(
    did: str, lat: float, lon: float, at: datetime, fix: str = "3d", speed: float = 0.0, hdop: float = 0.9
) -> bytes:
    msg = json.loads(telemetry(did, lat, lon, at, speed=speed))
    msg["fix"] = {"type": fix, "hdop": hdop, "satellites": 0 if fix == "none" else 10, "replayed": False}
    return json.dumps(msg).encode()


async def kinds(s) -> list[str]:
    return [e["kind"] for e in reversed(await s.store.events(limit=100))]


# --- contract v4.1 ---------------------------------------------------------------------------------------
def test_pydantic_health_agrees_with_the_schema_examples():
    for path in sorted((EXAMPLES / "health" / "valid").glob("*.json")):
        Health.model_validate_json(path.read_text())
    for path in sorted((EXAMPLES / "health" / "invalid").glob("*.json")):
        with pytest.raises(ValueError):
            Health.model_validate_json(path.read_text())


def test_supply_voltage_is_optional_but_checked():
    ok = json.loads(health_msg(supply_mv=4900))
    assert not list(validator("health").iter_errors(ok))
    assert list(validator("health").iter_errors({**ok, "supply_mv": -5}))
    base = json.loads(health_msg())
    assert (
        not list(validator("health").iter_errors(base))
        and Health.model_validate_json(json.dumps(base)).supply_mv is None
    )


def test_fake_unit_messages_are_valid_contract_messages():
    fu = load_fake_unit()
    unit = fu.FakeUnit("dev-001", [(27.45, -81.34), (27.5, -81.3)], no_fix=True, weak_signal=True)
    assert not list(validator("health").iter_errors(json.loads(unit.health(5.0))))
    assert not list(validator("telemetry").iter_errors(json.loads(unit.telemetry(5.0))))
    lat, lon, _ = unit.position(10_000.0)  # holds at the end of the route
    assert (lat, lon) == pytest.approx((27.5, -81.3))
    path = fu.corridor_path(fu.DEFAULT_NETWORK, ["ev-sef", "ev-orl"])
    assert len(path) >= 2 and path[0] != path[-1]


def test_acl_lets_a_unit_write_only_its_own_health():
    acl = (REPO / "mosquitto" / "acl").read_text().splitlines()
    assert "pattern write aerolink/aircraft/%u/health" in acl
    assert not any(line.startswith("pattern read") and line.endswith("/health") for line in acl)


@needs_broker
def test_broker_accepts_own_health_and_refuses_anothers(tmp_path: Path):
    port = free_port()
    proc = start_broker(tmp_path, port)
    try:

        async def go():
            async with aiomqtt.Client("127.0.0.1", port, username="backend", password=USERS["backend"]) as b:
                await b.subscribe("aerolink/aircraft/+/health", qos=1)
                async with aiomqtt.Client("127.0.0.1", port, username="dev-001", password=USERS["dev-001"]) as d:
                    await d.publish("aerolink/aircraft/dev-002/health", health_msg("dev-002"), qos=1)  # refused
                    await d.publish("aerolink/aircraft/dev-001/health", health_msg("dev-001"), qos=1)
                async with asyncio.timeout(5):
                    async for m in b.messages:
                        return m.topic.value

        assert asyncio.run(go()) == "aerolink/aircraft/dev-001/health"  # the first one to arrive is the allowed one
    finally:
        proc.terminate()
        proc.wait(5)


def start_broker(tmp_path: Path, port: int) -> subprocess.Popen:
    passwd = tmp_path / "passwd"
    if not passwd.exists():
        passwd.touch()
        for user, pw in USERS.items():
            subprocess.run(["mosquitto_passwd", "-b", str(passwd), user, pw], check=True, capture_output=True)
    conf = tmp_path / "mosquitto.conf"
    conf.write_text(
        f"allow_anonymous false\npassword_file {passwd}\nacl_file {REPO / 'mosquitto' / 'acl'}\n"
        f"listener {port} 127.0.0.1\n" + ("user root\n" if os.geteuid() == 0 else "")
    )
    proc = subprocess.Popen(["mosquitto", "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 5
    while time.time() < deadline:
        import socket

        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.05)
    return proc


# --- unit status ------------------------------------------------------------------------------------------
async def one_unit(tmp_path: Path, net: Network):
    clock = Clock()
    s = await make_service(tmp_path, net, clock)
    await s.store.register_device("dev-001", "passenger")
    await s.handle(f"{BASE}/status", b"online")
    lat, lon = net.nodes["ev-sef"]["lat"], net.nodes["ev-sef"]["lon"]
    return s, clock, lat, lon


async def device(s) -> dict:
    return next(d for d in await s.store.list_devices() if d["id"] == "dev-001")


def test_no_fix_makes_a_unit_degraded_not_offline(tmp_path: Path, fl_network: Network):
    async def go():
        s, clock, lat, lon = await one_unit(tmp_path, fl_network)
        await s.handle(f"{BASE}/telemetry", tel("dev-001", lat, lon, clock.t))
        await s.handle(f"{BASE}/health", health_msg(at=clock.t))
        await s.check_units()
        assert s.units.view("dev-001", True, clock.t)["status"] == "healthy"

        clock.t += timedelta(seconds=1)
        await s.handle(f"{BASE}/telemetry", tel("dev-001", lat, lon, clock.t, fix="none"))  # fix lost
        for _ in range(11):  # 11 s of telemetry without a fix; the health heartbeat stays fresh
            clock.t += timedelta(seconds=1)
            await s.handle(f"{BASE}/telemetry", tel("dev-001", lat, lon, clock.t, fix="none"))
        await s.handle(f"{BASE}/health", health_msg(at=clock.t, fix="none"))
        await s.check_units()
        view = s.units.view("dev-001", (await device(s))["online"], clock.t)
        assert view["status"] == "degraded" and view["status_reasons"] == ["no_gnss_fix"]
        assert (await device(s))["online"] is True  # degraded, not offline

        clock.t += timedelta(seconds=1)
        await s.handle(f"{BASE}/telemetry", tel("dev-001", lat, lon, clock.t))  # fix back
        await s.check_units()
        assert s.units.view("dev-001", True, clock.t)["status"] == "healthy"
        assert await kinds(s) == [
            "gnss_fix_lost",
            "unit_degraded",
            "gnss_fix_gained",
            "unit_healthy",
        ]
        await s.store.close()

    asyncio.run(go())


def test_weak_signal_and_stale_health_degrade_a_unit(tmp_path: Path, fl_network: Network):
    async def go():
        s, clock, lat, lon = await one_unit(tmp_path, fl_network)
        await s.handle(f"{BASE}/health", health_msg(at=clock.t, rssi=-85))
        await s.handle(f"{BASE}/telemetry", tel("dev-001", lat, lon, clock.t))
        await s.check_units()
        v = s.units.view("dev-001", True, clock.t)
        assert (
            v["status"] == "degraded" and v["status_reasons"] == ["weak_signal"] and v["health"]["wifi_rssi_dbm"] == -85
        )
        await s.handle(f"{BASE}/health", health_msg(at=clock.t, rssi=-60))
        assert s.units.view("dev-001", True, clock.t)["status"] == "healthy"

        # telemetry keeps flowing, the heartbeat stops
        for k in range(1, 35):
            clock.t += timedelta(seconds=1)
            await s.handle(f"{BASE}/telemetry", tel("dev-001", lat, lon, clock.t))
            if k == 29:
                await s.check_units()
                assert s.units.view("dev-001", True, clock.t)["status"] == "healthy"  # 29 s: not yet
        await s.check_units()
        v = s.units.view("dev-001", True, clock.t)
        assert v["status"] == "degraded" and v["status_reasons"] == ["health_stale"]
        await s.store.close()

    asyncio.run(go())


def test_a_unit_that_never_sent_health_is_not_judged_by_it(tmp_path: Path, fl_network: Network):
    async def go():
        s, clock, lat, lon = await one_unit(tmp_path, fl_network)
        for _ in range(40):  # v4.0 firmware: telemetry only
            clock.t += timedelta(seconds=1)
            await s.handle(f"{BASE}/telemetry", tel("dev-001", lat, lon, clock.t))
        assert s.units.view("dev-001", True, clock.t)["status"] == "healthy"
        await s.store.close()

    asyncio.run(go())


def test_invalid_and_foreign_health_is_dropped(tmp_path: Path, fl_network: Network):
    async def go():
        s, clock, lat, lon = await one_unit(tmp_path, fl_network)
        await s.handle(f"{BASE}/health", health_msg(at=clock.t).replace(b'"4.1"', b'"4.0"'))
        await s.handle(f"{BASE}/health", health_msg("dev-002", at=clock.t))  # wrong aircraft id for this topic
        assert s.stats["invalid_health"] == 1 and s.stats["topic_mismatch"] == 1 and "dev-001" not in s.units.units
        await s.handle("aerolink/aircraft/dev-009/health", health_msg("dev-009", at=clock.t))  # not registered
        assert s.stats["health_unregistered"] == 1
        await s.store.close()

    asyncio.run(go())


def test_health_history_is_short_and_counts_as_liveness(env: Env, rig: Rig):
    clock = FakeMono()
    rig.link.mono = clock
    rig.online("dev-001")
    for _ in range(70):
        clock.t += 2.0
        rig.device("dev-001", "health", health_msg(at=datetime.now(UTC)))
        assert portal_of(rig.c).call(rig.link.check_silence) == []  # health alone keeps the unit online
    unit = app_state(rig.c).service.units.units["dev-001"]
    assert len(unit.history) == 60
    clock.t += 4.0
    assert portal_of(rig.c).call(rig.link.check_silence) == ["dev-001"]


# --- API: devices, WebSocket, deep healthz ------------------------------------------------------------------
def test_devices_api_and_websocket_carry_health(env: Env, rig: Rig):
    rig.pair("dev-001", env.codes["dev-001"])
    app_state(rig.c).ws_heartbeat_s = 30.0
    before = next(d for d in rig.c.get("/api/devices").json() if d["id"] == "dev-001")
    assert before["status"] == "offline" and before["health"] is None and before["stale"] is True
    rig.online("dev-001")
    with rig.ws("/ws/devices/dev-001") as ws:
        assert ws.receive_json()["payload"]["status"] == "healthy"  # in the state message too
        rig.device("dev-001", "health", health_msg(at=datetime.now(UTC), rssi=-90, supply_mv=4900))
        ev = ws.receive_json()
        assert ev["type"] == "health" and ev["payload"]["status"] == "degraded"
        assert ev["payload"]["status_reasons"] == ["weak_signal"] and ev["payload"]["health"]["supply_mv"] == 4900
    dev = next(d for d in rig.c.get("/api/devices").json() if d["id"] == "dev-001")
    assert dev["status"] == "degraded" and dev["health_age_s"] < 5 and dev["stale"] is False
    assert dev["online"] is True and dev["vehicle_type"] == "passenger"  # old fields unchanged
    assert rig.c.get("/api/devices/dev-001").json()["status"] == "degraded"


def test_healthz_is_cheap_and_deep_lists_every_watchdog(env: Env, rig: Rig):
    r = rig.c.get("/healthz").json()
    assert r["ok"] is True and r["state"] in ("starting", "ok", "warn", "fail") and "watchdogs" not in r
    deep = rig.c.get("/healthz?deep=1").json()
    names = {w["name"]: w for w in deep["watchdogs"]}
    assert set(names) == {"mqtt", "forecast", "simulator", "adsb", "database", "models"}
    assert all(w["state"] in ("ok", "warn", "fail") and w["since"] and "threshold" in w for w in names.values())
    assert deep["state"] == "ok" and rig.c.get("/healthz").json()["state"] == "ok"
    assert rig.c.get("/api/traffic").json()["stale"] is False


# --- watchdogs ----------------------------------------------------------------------------------------------
def dog(s, **kw) -> Watchdogs:
    return Watchdogs(s, kw.pop("settings", Settings()), **kw)


def states(results) -> dict[str, str]:
    return {r["name"]: r["state"] for r in results}


def test_simulator_watchdog_fails_within_30s_and_recovers(tmp_path: Path, fl_network: Network):
    async def go():
        clock = Clock()
        s = await make_service(tmp_path, fl_network, clock)
        wd = dog(s)
        assert states(await wd.evaluate())["simulator"] == "ok"  # no simulator at all: nothing to watch
        lat, lon = fl_network.nodes["ev-sef"]["lat"], fl_network.nodes["ev-sef"]["lon"]
        await s.handle("aerolink/sim/sim-000/telemetry", telemetry("sim-000", lat, lon, clock.t))
        assert states(await wd.evaluate())["simulator"] == "ok"
        for secs, want in ((5, "ok"), (15, "warn"), (25, "fail")):  # the sim is stopped; the loop runs every 5 s
            clock.t = T0 + timedelta(seconds=secs)
            await s.refresh_forecast()
            assert states(await wd.evaluate())["simulator"] == want, secs
        await s.handle("aerolink/sim/sim-000/telemetry", telemetry("sim-000", lat, lon, clock.t + timedelta(seconds=1)))
        assert states(await wd.evaluate())["simulator"] == "ok"
        log = [e for e in await s.store.events(kinds=["watchdog"]) if e["detail"]["name"] == "simulator"]
        assert [e["detail"]["to"] for e in reversed(log)] == ["warn", "fail", "ok"]
        assert [e["severity"] for e in reversed(log)] == ["warn", "critical", "info"]
        await s.store.close()

    asyncio.run(go())


def test_adsb_watchdog_fails_within_30s_and_recovers(tmp_path: Path, fl_network: Network):
    async def go():
        clock = SimpleNamespace(t=1000.0)
        down = {"v": False}

        async def fetch() -> dict:
            if down["v"]:
                raise OSError("feed unreachable")
            return {"ac": []}

        feed = AdsbFeed(AdsbSettings(poll_active_s=5.0, poll_idle_s=5.0), fetch=fetch, clock=lambda: clock.t)
        s = await make_service(tmp_path, fl_network, Clock())
        wd = dog(s, adsb=feed)
        assert states(await wd.evaluate())["adsb"] == "ok"  # never polled yet: the clock starts at the first look
        assert await feed.poll_once()
        assert states(await wd.evaluate())["adsb"] == "ok"
        down["v"] = True
        # thresholds are one poll interval (5 s here; 30 s viewed / 5 min idle by default) + 10 s warn / 20 s fail
        for secs, want in ((5, "ok"), (17, "warn"), (30, "fail")):
            clock.t = 1000.0 + secs
            await feed.poll_once()
            assert states(await wd.evaluate())["adsb"] == want, secs
        assert "unreachable" in next(r for r in wd.results if r["name"] == "adsb")["detail"]
        down["v"] = False
        clock.t += 5
        assert await feed.poll_once()
        assert states(await wd.evaluate())["adsb"] == "ok"
        disabled = dog(s, adsb=AdsbFeed(AdsbSettings(enabled=False)))
        assert states(await disabled.evaluate())["adsb"] == "ok"
        await s.store.close()

    asyncio.run(go())


def test_forecast_database_and_model_watchdogs(tmp_path: Path, fl_network: Network):
    async def go():
        clock = Clock()
        s = await make_service(tmp_path, fl_network, clock)
        reg = SimpleNamespace(loaded=lambda n: n == "cv", demand=None, offroute=None)
        wd = dog(s, settings=Settings(preload_models=True, forecaster="cv"), registry=reg)
        res = {r["name"]: r for r in await wd.evaluate()}
        assert res["forecast"]["state"] == "ok" and res["database"]["state"] == "ok"
        assert res["models"]["state"] == "warn" and "chronos" in res["models"]["detail"]
        reg.loaded = lambda n: False
        assert states(await wd.evaluate())["models"] == "fail"  # the default forecaster is missing

        clock.t = T0 + timedelta(seconds=45)  # the forecast loop is behind: 10 s interval, warn at 30, fail at 60
        assert states(await wd.evaluate())["forecast"] == "warn"
        clock.t = T0 + timedelta(seconds=70)
        assert states(await wd.evaluate())["forecast"] == "fail"
        await s.refresh_forecast()
        assert states(await wd.evaluate())["forecast"] == "ok"

        async def broken() -> None:
            raise OSError("disk is read-only")

        s.store.write_probe = broken
        res = {r["name"]: r for r in await wd.evaluate()}
        assert res["database"]["state"] == "fail" and "read-only" in res["database"]["detail"]
        assert wd.overall() == "fail"
        await s.store.close()

    asyncio.run(go())


@needs_broker
def test_mqtt_watchdog_fails_when_the_broker_stops_and_recovers(tmp_path: Path, fl_network: Network):
    async def go():
        port = free_port()
        proc = start_broker(tmp_path, port)
        settings = Settings(mqtt_host="127.0.0.1", mqtt_port=port, mqtt_password=USERS["backend"])
        store = Store.connect(f"sqlite+aiosqlite:///{tmp_path / 'wd.db'}")
        await store.create_all()
        s = await make_service(tmp_path, fl_network, Clock())
        link = MqttLink(settings, s)
        wd = dog(s, link=link)
        task = asyncio.create_task(link.run())

        async def wait(connected: bool, within: float) -> float:
            t0 = time.monotonic()
            async with asyncio.timeout(within):
                while link.connected != connected:
                    await asyncio.sleep(0.1)
            return time.monotonic() - t0

        try:
            await wait(True, 10)
            assert states(await wd.evaluate())["mqtt"] == "ok"
            proc.terminate()
            proc.wait(5)
            assert await wait(False, 30) < 30  # noticed well inside 30 s
            assert states(await wd.evaluate())["mqtt"] == "fail"
            proc = start_broker(tmp_path, port)
            await wait(True, 30)
            assert states(await wd.evaluate())["mqtt"] == "ok"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            proc.terminate()
            proc.wait(5)
            await store.close()
            await s.store.close()

    asyncio.run(go())


# --- silence watchdog against a real broker, with the fake unit script ------------------------------------------
@needs_broker
def test_fake_unit_going_silent_without_disconnecting_is_offline_within_5s(tmp_path: Path, fl_network: Network):
    async def go():
        port = free_port()
        proc = start_broker(tmp_path, port)
        settings = Settings(mqtt_host="127.0.0.1", mqtt_port=port, mqtt_password=USERS["backend"])
        s = await make_service(tmp_path, fl_network, Clock())
        s.clock = lambda: datetime.now(UTC)
        await s.store.register_device("dev-001", "passenger")
        link = MqttLink(settings, s)
        tasks = [asyncio.create_task(link.run()), asyncio.create_task(link.worker())]
        unit = None
        try:
            async with asyncio.timeout(10):
                while not link.connected:
                    await asyncio.sleep(0.05)
            script = REPO / "scripts" / "fake_unit.py"
            unit = subprocess.Popen(
                [sys.executable, str(script), "--id", "dev-001", "--password", USERS["dev-001"], "--port", str(port),
                 "--silent-after", "4", "--duration", "40"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )  # fmt: skip
            last, went_offline = 0.0, None
            async with asyncio.timeout(30):
                while went_offline is None:
                    await link.check_silence()
                    dev = need(await s.store.get_device("dev-001"))
                    if dev["online"]:
                        last = max(last, link.heard.get("dev-001", 0.0))
                    elif last:
                        went_offline = time.monotonic()
                    await asyncio.sleep(0.2)
            assert went_offline is not None and went_offline - last <= 5.0, went_offline - last
            assert unit.poll() is None  # it never disconnected: no Last Will
            assert need(s.units.units["dev-001"].health)["firmware_version"] == "fake-0.1"  # health arrived too
            assert "unit_no_signal" in await kinds(s)
        finally:
            if unit:
                unit.kill()
                unit.wait(5)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            proc.terminate()
            proc.wait(5)
            await s.store.close()

    asyncio.run(go())


# --- events and alerts ----------------------------------------------------------------------------------------
def test_events_are_persisted_listed_and_acknowledged_with_audit(env: Env, rig: Rig, caplog):
    rig.online("dev-001")
    rig.device("dev-001", "status", b"offline")  # Last Will
    rig.device("dev-001", "status", b"online")
    assert rig.c.get("/api/alerts").status_code == 401  # alerts need an operator session (G2)
    operator_login(rig.c, "alice")
    r = rig.c.get("/api/alerts")
    rows = r.json()
    assert [e["kind"] for e in rows][:3] == ["unit_online", "unit_offline", "unit_online"][::-1]
    assert all(e["acked_at"] is None and e["aircraft_id"] == "dev-001" for e in rows)
    offline = next(e for e in rows if e["kind"] == "unit_offline")
    assert rig.c.get("/api/alerts", params={"kind": "unit_offline"}).json() == [offline]
    assert rig.c.get("/api/alerts", params={"kind": "nope"}).status_code == 422
    assert rig.c.get("/api/alerts", params={"since": "2999-01-01T00:00:00Z"}).json() == []
    assert rig.c.get("/api/alerts", params={"since": "garbage"}).status_code == 422

    assert rig.c.post("/api/alerts/99999/ack").status_code == 404
    with caplog.at_level(logging.INFO, logger="aerolink.audit"):
        acked = rig.c.post(f"/api/alerts/{offline['id']}/ack").json()
    assert acked["acked_by"] == "alice" and acked["acked_at"]
    assert any("acknowledged by alice" in m for m in caplog.messages)
    operator_login(rig.c, "bob")
    again = rig.c.post(f"/api/alerts/{offline['id']}/ack").json()
    assert again["acked_by"] == "alice"  # the first acknowledgement stays

    open_ids = {e["id"] for e in rig.c.get("/api/alerts", params={"unacked": 1}).json()}
    assert offline["id"] not in open_ids and open_ids
    log = rig.c.get("/api/alerts", params={"kind": "alert_ack"}).json()
    assert len(log) == 1 and log[0]["detail"] == {"alert_id": offline["id"], "operator": "alice"}


def test_ops_websocket_pushes_new_alerts_acks_and_watchdog_changes(env: Env, rig: Rig):
    app_state(rig.c).ws_heartbeat_s = 30.0
    rig.online("dev-001")
    operator_login(rig.c, "alice")
    with rig.c.websocket_connect("/ws/ops") as ws:
        first = ws.receive_json()
        assert first["type"] == "state" and any(e["kind"] == "unit_online" for e in first["payload"]["unacked"])
        rig.device("dev-001", "status", b"offline")
        ev = ws.receive_json()
        assert ev["type"] == "alert" and ev["payload"]["kind"] == "unit_offline" and ev["payload"]["id"]
        rig.c.post(f"/api/alerts/{ev['payload']['id']}/ack")
        got = [ws.receive_json(), ws.receive_json()]
        assert {g["type"] for g in got} == {"alert", "ack"}
        ack = next(g for g in got if g["type"] == "ack")
        assert ack["payload"]["acked_by"] == "alice"
    assert app_state(rig.c).hub.subscribers("*ops*") == 0  # unsubscribed on close


def test_other_event_kinds_are_recorded(tmp_path: Path, fl_network: Network):
    async def go():
        s, clock, lat, lon = await one_unit(tmp_path, fl_network)
        await s.store.update_device("dev-001", demo=True)
        await s.handle(f"{BASE}/telemetry", tel("dev-001", lat, lon, clock.t))
        clock.t += timedelta(seconds=1)
        msg = json.loads(tel("dev-001", lat, lon, clock.t))
        msg["vehicle_type"] = "medical"
        await s.handle(f"{BASE}/telemetry", json.dumps(msg).encode())
        assert await kinds(s) == ["vehicle_type_changed"]
        await s.store.close()

    asyncio.run(go())


# --- track segments -------------------------------------------------------------------------------------------------
def test_segments_split_at_gaps_reconnects_and_fix_recovery(tmp_path: Path, fl_network: Network):
    async def go():
        s, clock, lat, lon = await one_unit(tmp_path, fl_network)

        async def send(secs: float, **kw) -> None:
            clock.t = T0 + timedelta(seconds=secs)
            await s.handle(f"{BASE}/telemetry", tel("dev-001", lat, lon, clock.t, **kw))

        for k in range(1, 6):
            await send(k)
        seg1 = s.live.tracks["dev-001"].segment_id
        await send(6 + SEGMENT_GAP_S - 1)  # a 29 s pause: the same segment
        assert s.live.tracks["dev-001"].segment_id == seg1
        await send(6 + SEGMENT_GAP_S - 1 + SEGMENT_GAP_S + 1)  # 31 s: a new one
        seg2 = s.live.tracks["dev-001"].segment_id
        assert seg2 != seg1 and s.live.last_break["dev-001"] == "gap"

        await send(90)  # 24 s later: still the same segment
        assert s.live.tracks["dev-001"].segment_id == seg2
        await s.handle(f"{BASE}/status", b"online")  # the unit reconnected
        await send(91)
        seg3 = s.live.tracks["dev-001"].segment_id
        assert seg3 not in (seg1, seg2) and s.live.last_break["dev-001"] == "reconnect"

        await send(92)
        await send(93, fix="none")  # fix lost: dropped, but the next good sample starts a new segment
        await send(94, hdop=9.0)
        await send(95)
        seg4 = s.live.tracks["dev-001"].segment_id
        assert seg4 not in (seg1, seg2, seg3) and s.live.last_break["dev-001"] == "fix_recovery"
        await send(96)
        assert s.live.tracks["dev-001"].segment_id == seg4

        rows = await s.store.telemetry_history("dev-001")
        ids = [r["segment_id"] for r in rows]
        assert ids == sorted(ids) and len(set(ids)) == 4 and ids.count(seg1) == 6
        await s.store.close()

    asyncio.run(go())


def test_trails_api_returns_segment_ids(env: Env, rig: Rig):
    rig.online("dev-001")
    rows = rig.c.get("/api/aircraft/dev-001/telemetry").json()
    assert rows and isinstance(rows[0]["segment_id"], int)


def test_off_route_does_not_fire_on_a_jump_after_a_gap(tmp_path: Path, fl_network: Network):
    async def go(gap_s: int):
        s, clock, _ = await _classical_flight(tmp_path / f"g{gap_s}", fl_network)
        path = s.flights["dev-001"]["path"]
        track = fly(fl_network, path, 120)
        for k, (lat, lon) in enumerate(track[:30], start=1):
            clock.t = T0 + timedelta(seconds=k)
            await s.handle(f"{BASE}/telemetry", telemetry("dev-001", lat, lon, clock.t, speed=30.0))
        assert await s.scan_off_route() == [] and s.flights["dev-001"]["status"] == "active"
        # the unit vanishes and reappears 700 m to the side of the route
        (la0, lo0), (la1, lo1) = track[30], track[31]
        dn, de = (la1 - la0) * 111_320.0, (lo1 - lo0) * 111_320.0 * math.cos(math.radians(la0))
        norm = math.hypot(dn, de)
        side = (
            la0 + (-de / norm) * 700 / 111_320.0,
            lo0 + (dn / norm) * 700 / (111_320.0 * math.cos(math.radians(la0))),
        )
        jumped = [side] * 8
        flagged_after = None
        for k, (la, lo) in enumerate(jumped, start=1):
            clock.t = T0 + timedelta(seconds=30 + gap_s + k)
            await s.handle(f"{BASE}/telemetry", telemetry("dev-001", la, lo, clock.t, speed=30.0))
            await s.scan_off_route()
            if flagged_after is None and s.flights["dev-001"]["status"] == "off_route":
                flagged_after = k
        kinds_ = await kinds(s)
        await s.store.close()
        return flagged_after, kinds_

    (tmp_path / "g60").mkdir()
    (tmp_path / "g1").mkdir()
    after_gap, events = asyncio.run(go(60))
    assert after_gap == 5, after_gap  # the 5th consecutive sample of the new segment, not the first
    assert events.count("off_route") == 1
    no_gap, _ = asyncio.run(go(1))
    assert no_gap == 1  # control: without a segment break the same jump is flagged at once


# --- migration 0004 --------------------------------------------------------------------------------------------------
def test_migration_0004_up_and_down_on_sqlite(tmp_path: Path):
    backend = Path(__file__).resolve().parents[1]
    db = tmp_path / "m.db"
    penv = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}"}

    def inspect() -> tuple[set[str], set[str]]:
        with sqlite3.connect(db) as c:
            tables = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
            cols = {r[1] for r in c.execute("pragma table_info(telemetry)")}
        return tables, cols

    for args in (["upgrade", "head"], ["downgrade", "0003"], ["upgrade", "head"]):
        r = subprocess.run(
            [sys.executable, "-m", "alembic", *args], cwd=backend, env=penv, capture_output=True, text=True
        )
        assert r.returncode == 0, r.stderr
        tables, cols = inspect()
        up = args[0] == "upgrade"
        assert ("events" in tables) == up and ("segment_id" in cols) == up
