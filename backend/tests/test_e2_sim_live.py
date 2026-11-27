"""Simulator live mode against the real Service in-process (PLAN E6.4): plans reported, live == offline,
scenario switching. A fake bus carries what a broker would, so no mosquitto is needed."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

from test_service import make_service

from aerolink.network import Network
from aerolink.sim.live import SimLive, build_schedule
from aerolink.sim.runner import run_simulation
from aerolink.sim.scenarios import Scenario, load_scenario

SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"
FAST = 3000.0  # replay speed: a 15 min scenario in well under a second of wall time


def short_scenario() -> Scenario:
    return Scenario(duration_s=2700, fleet={"passenger": 4, "medical": 1}, seed=7)


class Bus:
    """Sim <-> Service in one process: what the sim publishes goes to Service.handle, replies come back."""

    def __init__(self, service, scenarios_dir: Path = SCENARIOS):
        self.service = service
        self.sim: SimLive | None = None
        self.log: list[tuple[str, bytes]] = []
        self.scenarios_dir = scenarios_dir

    async def publish(self, topic: str, payload: bytes) -> None:
        self.log.append((topic, payload))
        for out_topic, out_payload in await self.service.handle(topic, payload):
            if self.sim is not None and out_topic.startswith("aerolink/sim/"):
                self.sim.on_message(out_topic, out_payload)

    def by_kind(self, kind: str) -> list[dict]:
        return [json.loads(p) for t, p in self.log if t.endswith("/" + kind) and p.startswith(b"{")]


def make_sim(fl_network: Network, bus: Bus, **kw) -> SimLive:
    sim = SimLive(fl_network, bus.publish, bus.scenarios_dir, speed=FAST, **kw)
    bus.sim = sim
    return sim


def test_schedule_is_deterministic_and_matches_the_offline_run(fl_network: Network):
    a = run_simulation(fl_network, short_scenario(), seed=7)
    b = run_simulation(fl_network, short_scenario(), seed=7)
    sa, sb = build_schedule(a), build_schedule(b)
    assert sa == sb and [x.offset_s for x in sa] == sorted(x.offset_s for x in sa)
    deps = [e for e in a.events if e["kind"] == "departure"]
    assert deps, "scenario must have departures for this test to mean anything"
    assert [(x.aircraft_id, list(x.path)) for x in sa if x.kind == "depart"] == [
        (e["aircraft_id"], e["path"]) for e in deps
    ]
    assert sum(x.kind == "telemetry" for x in sa) == len(a.telemetry)
    assert any(x.kind == "ground" for x in sa)  # parked aircraft report too
    # a departure is reported after the aircraft's first telemetry, never before
    first_tel: dict[str, float] = {}
    for x in sa:
        if x.kind == "telemetry":
            first_tel.setdefault(x.aircraft_id, x.offset_s)
        elif x.kind == "depart":
            assert x.offset_s > first_tel[x.aircraft_id]


def test_live_replay_records_the_flown_paths_and_telemetry_unchanged(tmp_path: Path, fl_network: Network):
    async def go():
        from test_service import Clock

        service = await make_service(tmp_path, fl_network, Clock())
        bus = make_bus = Bus(service)
        sim = make_sim(fl_network, make_bus)
        offline = run_simulation(fl_network, short_scenario(), seed=7)  # truth: where parked aircraft sit
        await sim.run_schedule(build_schedule(offline, FAST))
        await asyncio.gather(*list(sim._tasks))  # the plan reports

        # same departures, same paths (every departure reached the backend as a planned flight)
        deps = [(e["aircraft_id"], e["path"]) for e in offline.events if e["kind"] == "departure"]
        flights = await service.store.flights()
        assert sorted((f["aircraft_id"], f["path"]) for f in flights) == sorted(deps)
        assert sim.sent["plans_selected"] + sim.sent["plans_reported"] == len(deps)

        # same telemetry modulo timestamp and message id
        everything = [json.loads(p) for t, p in bus.log if t.endswith("/telemetry")]
        sent = [r for r in everything if r["active_flight_id"] is not None or r["motion"]["ground_speed_mps"] > 0]
        parked = [r for r in everything if r not in sent]
        assert parked and all(r["motion"]["ground_speed_mps"] == 0.0 for r in parked)
        strip = lambda r: {k: v for k, v in r.items() if k not in ("timestamp", "message_id")}  # noqa: E731
        assert [strip(r) for r in sent] == [strip(r) for r in sorted(offline.telemetry, key=lambda r: r["timestamp"])]
        # every landed aircraft was told it landed: nothing flying is left active, the parked aircraft stay visible
        arrivals = sum(1 for e in offline.events if e["kind"] == "arrival")
        assert arrivals > 0 and sim.sent["landed"] == arrivals and sim.sent["offline"] == 0
        assert sum(f["status"] == "ended" for f in await service.store.flights()) == arrivals  # landed -> flight ended
        assert len(service.flights) == len(deps) - arrivals  # the rest are still in the air
        await service.store.close()

    asyncio.run(go())


def test_a_path_the_backend_does_not_offer_is_still_recorded(tmp_path: Path, fl_network: Network):
    """The sim's own path may not be among the three options: the flight-plan message covers it."""

    async def go():
        from test_service import Clock

        service = await make_service(tmp_path, fl_network, Clock())
        bus = Bus(service)
        sim = make_sim(fl_network, bus)
        offline = run_simulation(fl_network, short_scenario(), seed=7, record_truth=False)
        sched = [a for a in build_schedule(offline, FAST)]
        # force the fallback: pretend the backend always answers "no options" for the first departure
        real = sim.on_message
        cast(Any, sim).on_message = lambda t, p: None if t.endswith("/route-response") else real(t, p)
        sim.response_timeout_s = 0.05
        await sim.run_schedule(sched)
        await asyncio.gather(*list(sim._tasks))
        assert sim.sent["plans_selected"] == 0 and sim.sent["plans_reported"] > 0
        deps = sorted((e["aircraft_id"], e["path"]) for e in offline.events if e["kind"] == "departure")
        assert sorted((f["aircraft_id"], f["path"]) for f in await service.store.flights()) == deps
        await service.store.close()

    asyncio.run(go())


def test_scenario_switch_restarts_the_replay_and_clears_old_aircraft(tmp_path: Path, fl_network: Network):
    async def go():
        from test_service import Clock

        service = await make_service(tmp_path, fl_network, Clock())
        bus = Bus(service)
        sim = SimLive(fl_network, bus.publish, SCENARIOS, speed=20.0, loop=True)  # slow enough to catch mid-flight
        bus.sim = sim
        await sim.start("medical_dispatch")
        async with asyncio.timeout(60):
            while not any(tr.source == "sim" for tr in service.live.tracks.values()):
                await asyncio.sleep(0.05)
        old = {a for a, tr in service.live.tracks.items() if tr.source == "sim"}
        assert old

        # the control message the backend publishes on PUT /api/scenario
        at_restart: list[set[str]] = []
        real_start = sim.start

        async def spy_start(
            name, path=None
        ):  # runs after the old replay stopped and the old aircraft were told offline
            at_restart.append({a for a, tr in service.live.tracks.items() if tr.source == "sim"})
            at_restart.append({a for a, r in service.flights.items() if r["source"] == "sim"})
            await real_start(name, path)

        sim.start = spy_start
        sim.on_message("aerolink/sim/control/scenario", json.dumps({"scenario": "nominal_day"}).encode())
        async with asyncio.timeout(60):
            while sim.scenario_name != "nominal_day" or not sim._replay:
                await asyncio.sleep(0.05)
        assert sim.scenario_name == "nominal_day"
        assert old <= {t.split("/")[2] for t, p in bus.log if t.endswith("/status") and p == b"offline"}
        assert at_restart[0] == set() and at_restart[1] == set()  # old aircraft and their flights are gone
        # unknown scenario is ignored, the replay keeps running
        sim.on_message("aerolink/sim/control/scenario", b'{"scenario": "nope"}')
        await asyncio.sleep(0.2)
        assert sim.scenario_name == "nominal_day"
        await sim.stop()
        await service.store.close()

    asyncio.run(go())


def test_scenarios_are_loadable():
    assert {p.stem for p in SCENARIOS.glob("*.yaml")} >= {"nominal_day", "medical_dispatch"}
    for p in SCENARIOS.glob("*.yaml"):
        load_scenario(p)
