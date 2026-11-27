"""End to end over a real Mosquitto broker with the project ACL (PLAN E6.2, E6.3).

Skipped when the `mosquitto` binary isn't installed (CI installs it: `apt-get install mosquitto`).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import aiomqtt
import pytest
from test_service import route_request, selection, telemetry, utc_now

from aerolink.contracts import FlightStatus, RouteResponse
from aerolink.network import Network
from aerolink.service.core import Service
from aerolink.service.mqtt import MqttLink, payload_bytes
from aerolink.service.planner import Planner
from aerolink.service.settings import REPO, Settings
from aerolink.service.store import Store

pytestmark = pytest.mark.skipif(shutil.which("mosquitto") is None, reason="mosquitto not installed")
USERS = {"backend": "pw-backend", "sim": "pw-sim", "dev-001": "pw-dev-001", "dev-002": "pw-dev-002"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def broker(tmp_path: Path):
    port = free_port()
    passwd = tmp_path / "passwd"
    passwd.touch()
    for user, pw in USERS.items():
        subprocess.run(["mosquitto_passwd", "-b", str(passwd), user, pw], check=True, capture_output=True)
    conf = tmp_path / "mosquitto.conf"
    conf.write_text(
        f"allow_anonymous false\npassword_file {passwd}\nacl_file {REPO / 'mosquitto' / 'acl'}\n"
        f"listener {port} 127.0.0.1\n" + ("user root\n" if os.geteuid() == 0 else "")
        # Started as root, mosquitto drops to user "mosquitto", which can't read pytest's tmp dir.
    )
    proc = subprocess.Popen(["mosquitto", "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 5
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.05)
    yield port
    proc.terminate()
    proc.wait(5)


async def next_message(client: aiomqtt.Client, timeout: float = 10.0) -> aiomqtt.Message:
    async with asyncio.timeout(timeout):
        async for m in client.messages:
            return m
    raise AssertionError("no message")


def test_device_flow_over_mqtt_with_acl(broker: int, tmp_path: Path, fl_network: Network):
    async def go():
        settings = Settings(mqtt_host="127.0.0.1", mqtt_port=broker, mqtt_password=USERS["backend"])
        store = Store.connect(f"sqlite+aiosqlite:///{tmp_path / 'mq.db'}")
        await store.create_all()
        await store.register_device("dev-001", "passenger")
        service = Service(store, Planner(fl_network, settings))
        await service.start()
        link = MqttLink(settings, service)
        tasks = [asyncio.create_task(link.run()), asyncio.create_task(link.worker())]
        try:
            async with asyncio.timeout(10):
                while not link.connected:
                    await asyncio.sleep(0.05)
            async with aiomqtt.Client("127.0.0.1", broker, username="dev-001", password=USERS["dev-001"]) as dev:
                base = "aerolink/aircraft/dev-001"
                await dev.subscribe(f"{base}/route-response", qos=1)
                await dev.subscribe(f"{base}/flight-status", qos=1)
                lat, lon = fl_network.nodes["ev-sef"]["lat"], fl_network.nodes["ev-sef"]["lon"]
                await dev.publish(f"{base}/telemetry", telemetry("dev-001", lat, lon, utc_now()), qos=1)
                # Spoofing another device's topic is dropped by the broker ACL.
                await dev.publish(
                    "aerolink/aircraft/dev-002/telemetry", telemetry("dev-002", lat, lon, utc_now()), qos=1
                )
                async with asyncio.timeout(5):
                    while "dev-001" not in service.live.tracks:
                        await asyncio.sleep(0.05)

                rid = str(uuid4())
                await dev.publish(f"{base}/route-request", route_request("dev-001", "ev-orl", request_id=rid), qos=1)
                m = await next_message(dev)
                assert m.topic.value == f"{base}/route-response"
                resp = RouteResponse.model_validate_json(payload_bytes(m))
                assert resp.status == "ok"

                await dev.publish(
                    f"{base}/route-selection", selection("dev-001", rid, str(resp.routes[0].route_id)), qos=1
                )
                m = await next_message(dev)
                assert FlightStatus.model_validate_json(payload_bytes(m)).status == "planned"
            assert "dev-002" not in service.live.tracks and service.stats["topic_mismatch"] == 0
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await store.close()

    asyncio.run(go())


def test_anonymous_connections_are_refused(broker: int):
    async def go():
        with pytest.raises(aiomqtt.MqttCodeError):  # refused by the broker (not authorised), not unreachable
            async with aiomqtt.Client("127.0.0.1", broker):
                pass

    asyncio.run(go())


def test_simulator_reports_plans_and_switches_scenario_over_the_broker(
    broker: int, tmp_path: Path, fl_network: Network
):
    """Replay + report plans through real Mosquitto with the project ACL, then a scenario switch."""
    from aerolink.sim.live import CONTROL_TOPIC, SimLive
    from aerolink.sim.runner import run_simulation
    from aerolink.sim.scenarios import Scenario

    scenario = Scenario(duration_s=1200, fleet={"passenger": 4, "medical": 1}, seed=7)
    deps = [
        e for e in run_simulation(fl_network, scenario, seed=7, record_truth=False).events if e["kind"] == "departure"
    ]
    assert deps
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    from aerolink.sim.scenarios import save_scenario

    save_scenario(scenario, scenarios / "short.yaml")
    save_scenario(Scenario(duration_s=600, fleet={"passenger": 1}, seed=3), scenarios / "other.yaml")

    async def go():
        settings = Settings(mqtt_host="127.0.0.1", mqtt_port=broker, mqtt_password=USERS["backend"])
        store = Store.connect(f"sqlite+aiosqlite:///{tmp_path / 'sim.db'}")
        await store.create_all()
        service = Service(store, Planner(fl_network, settings))
        await service.start()
        link = MqttLink(settings, service)
        tasks = [asyncio.create_task(link.run()), asyncio.create_task(link.worker())]
        try:
            async with asyncio.timeout(10):
                while not link.connected:
                    await asyncio.sleep(0.05)
            async with aiomqtt.Client("127.0.0.1", broker, username="sim", password=USERS["sim"]) as client:

                async def send(topic: str, payload: bytes) -> None:
                    await client.publish(topic, payload, qos=1)

                sim = SimLive(fl_network, send, scenarios, speed=3000.0)
                await client.subscribe("aerolink/sim/+/route-response", qos=1)
                await client.subscribe(CONTROL_TOPIC, qos=1)

                async def listen() -> None:
                    async for m in client.messages:
                        sim.on_message(m.topic.value, payload_bytes(m))

                listener = asyncio.create_task(listen())
                await sim.start("short")
                async with asyncio.timeout(60):
                    await sim.wait()
                    await asyncio.gather(*list(sim._tasks))
                    while len(await store.flights()) < len(deps):
                        await asyncio.sleep(0.1)
                assert sorted((f["aircraft_id"], f["path"]) for f in await store.flights()) == sorted(
                    (e["aircraft_id"], e["path"]) for e in deps
                )
                assert sim.sent["plans_selected"] + sim.sent["plans_reported"] == len(deps)

                # PUT /api/scenario's message: the backend publishes the control topic, the sim restarts
                await link.publish([(CONTROL_TOPIC, b'{"scenario": "other"}')])
                async with asyncio.timeout(30):
                    while sim.scenario_name != "other":
                        await asyncio.sleep(0.05)
                listener.cancel()
                await sim.stop()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await store.close()

    asyncio.run(go())
