"""E3 against a real Mosquitto: retained status + Last Will reach a paired screen within 5 s.

Skipped when `mosquitto` isn't on PATH (Homebrew: PATH=/opt/homebrew/sbin:$PATH).
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

import aiomqtt
import pytest
from conftest import need
from fastapi.testclient import TestClient
from test_e3_pilot_api import HTTPS, Env, Rig
from test_service_mqtt import USERS, broker  # noqa: F401  (broker is a fixture)

from aerolink.network import Network
from aerolink.service.app import create_app
from aerolink.service.settings import Settings

pytestmark = pytest.mark.skipif(shutil.which("mosquitto") is None, reason="mosquitto not installed")


def test_last_will_reaches_screen_within_5s(broker: int, tmp_path: Path, fl_network: Network):  # noqa: F811
    env = Env(tmp_path)
    settings = Settings(
        database_url=env.url,
        forecaster="cv",
        suggestion_interval_s=0,
        mqtt_host="127.0.0.1",
        mqtt_port=broker,
        mqtt_password=USERS["backend"],
    )
    with TestClient(create_app(settings, mqtt=True), base_url=HTTPS) as c:
        c.post("/api/devices/dev-001/pair", json={"code": env.codes["dev-001"]})
        deadline = time.time() + 10
        while not c.get("/healthz").json()["mqtt"] and time.time() < deadline:
            time.sleep(0.05)
        rig = Rig.__new__(Rig)
        rig.c, rig.network = c, fl_network
        with rig.ws("/ws/devices/dev-001") as ws:
            assert ws.receive_json()["type"] == "state"

            async def unclean() -> float:
                dev = aiomqtt.Client(
                    "127.0.0.1",
                    broker,
                    username="dev-001",
                    password=USERS["dev-001"],
                    will=aiomqtt.Will("aerolink/aircraft/dev-001/status", b"offline", qos=1, retain=True),
                )
                await dev.__aenter__()
                await dev.publish("aerolink/aircraft/dev-001/status", b"online", qos=1, retain=True)
                await asyncio.sleep(0.5)
                need(dev._client._sock).close()  # drop the TCP connection: the broker publishes the will
                return time.monotonic()

            t_online = asyncio.run(unclean())
            seen = {}
            while "offline" not in seen:
                ev = ws.receive_json()
                seen[ev["type"]] = time.monotonic()
            assert seen["offline"] - t_online < 5
        assert c.get("/api/devices/dev-001").json()["online"] is False


UNIT_SCRIPT = """
import asyncio, sys, aiomqtt
port, pw, payload = int(sys.argv[1]), sys.argv[2], sys.argv[3].encode()
async def main():
    will = aiomqtt.Will("aerolink/aircraft/dev-001/status", b"offline", qos=1, retain=True)
    async with aiomqtt.Client("127.0.0.1", port, username="dev-001", password=pw, keepalive=120, will=will) as c:
        await c.publish("aerolink/aircraft/dev-001/status", b"online", qos=1, retain=True)
        print("ready", flush=True)
        while True:
            await c.publish("aerolink/aircraft/dev-001/telemetry", payload, qos=0)
            await asyncio.sleep(0.5)
asyncio.run(main())
"""


def test_frozen_unit_goes_offline_within_5s_without_a_will(broker: int, tmp_path: Path, fl_network: Network):  # noqa: F811
    """SIGSTOP the unit: no FIN, no Last Will (keepalive is 120 s). The silence watchdog must catch it."""
    import os
    import signal
    import subprocess
    import sys
    from datetime import UTC, datetime

    from test_service import telemetry

    env = Env(tmp_path)
    settings = Settings(
        database_url=env.url,
        forecaster="cv",
        suggestion_interval_s=0,
        mqtt_host="127.0.0.1",
        mqtt_port=broker,
        mqtt_password=USERS["backend"],
    )
    lat, lon = fl_network.nodes["ev-sef"]["lat"], fl_network.nodes["ev-sef"]["lon"]
    payload = telemetry("dev-001", lat, lon, datetime.now(UTC)).decode()
    with TestClient(create_app(settings, mqtt=True), base_url=HTTPS) as c:
        c.post("/api/devices/dev-001/pair", json={"code": env.codes["dev-001"]})
        deadline = time.time() + 10
        while not c.get("/healthz").json()["mqtt"] and time.time() < deadline:
            time.sleep(0.05)
        rig = Rig.__new__(Rig)
        rig.c, rig.network = c, fl_network
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", UNIT_SCRIPT, str(broker), USERS["dev-001"], payload],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout and proc.stdout.readline().strip() == "ready"
            time.sleep(1.5)
            assert c.get("/api/devices/dev-001").json()["online"] is True
            with rig.ws("/ws/devices/dev-001") as ws:
                assert ws.receive_json()["type"] == "state"
                os.kill(proc.pid, signal.SIGSTOP)
                t0 = time.monotonic()
                while True:
                    ev = ws.receive_json()
                    if ev["type"] == "offline":
                        break
                assert time.monotonic() - t0 < 5 and ev["payload"]["reason"] == "no_signal"
            assert c.get("/api/devices/dev-001").json()["online"] is False
        finally:
            os.kill(proc.pid, signal.SIGCONT)
            proc.kill()
            proc.wait()
