"""F3: parked aircraft report too. The sim publishes them 1 Hz; the backend derives ``on_ground`` from motion."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from conftest import portal_of
from fastapi.testclient import TestClient
from test_e2_sim_live import FAST, Bus, make_sim, short_scenario
from test_service import T0, Clock, make_service, node, route_request, run, selection, telemetry

from aerolink.contracts import RouteResponse
from aerolink.network import Network
from aerolink.service.app import create_app
from aerolink.service.settings import Settings
from aerolink.service.store import Store
from aerolink.sim.live import build_schedule, ground_records
from aerolink.sim.runner import run_simulation

SIM = "aerolink/sim/sim-001/telemetry"


def vs_telemetry(lat: float, lon: float, vs: float, speed: float = 0.0, aid: str = "sim-001", t=T0) -> bytes:
    rec = json.loads(telemetry(aid, lat, lon, t, speed=speed))
    rec["motion"]["vertical_speed_mps"] = vs
    return json.dumps(rec).encode()


def test_ground_records_are_the_pad_rows_of_the_truth(fl_network: Network):
    result = run_simulation(fl_network, short_scenario(), seed=7)
    ground = ground_records(result)
    assert ground and all(r["motion"]["ground_speed_mps"] == 0.0 and r["active_flight_id"] is None for r in ground)
    # the whole fleet shows up, and a second is never both airborne and parked for one aircraft
    assert {r["aircraft_id"] for r in ground} | {r["aircraft_id"] for r in result.telemetry} == set(
        result.final_world.aircraft
    )
    air = {(r["aircraft_id"], r["timestamp"]) for r in result.telemetry}
    assert not air & {(r["aircraft_id"], r["timestamp"]) for r in ground}
    # the schedule is deterministic with them (message ids are renewed when sent)
    assert build_schedule(result) == build_schedule(result)
    assert sum(a.kind == "ground" for a in build_schedule(result)) == len(ground)


def test_every_fleet_aircraft_is_visible_and_flags_follow_the_flight(tmp_path: Path, fl_network: Network):
    async def go():
        service = await make_service(tmp_path, fl_network, Clock())
        bus = Bus(service)
        sim = make_sim(fl_network, bus)
        offline = run_simulation(fl_network, short_scenario(), seed=7)
        schedule = build_schedule(offline, FAST)
        first_flight = next(a.offset_s for a in schedule if a.kind == "telemetry")
        t_cut = first_flight - 1e-9  # before anything flies
        await sim.run_schedule([a for a in schedule if a.offset_s <= t_cut and a.kind == "ground"])
        fleet = set(offline.final_world.aircraft)
        assert set(service.live.tracks) == fleet
        assert all(tr.on_ground for tr in service.live.tracks.values())
        assert [await service.store.telemetry_history(a) for a in sorted(fleet)] == [[]] * len(
            fleet
        )  # parked sim aircraft are not written to the telemetry table
        # run on: whoever flies is not on the ground, the track survives landing (flight ended, aircraft parked)
        await sim.run_schedule(schedule)
        await asyncio.gather(*list(sim._tasks))
        assert set(service.live.tracks) == fleet
        flying = {r["aircraft_id"] for r in service.flights.values()}
        assert all(not service.live.tracks[a].on_ground for a in flying if service.flights[a]["status"] == "active")
        await service.store.close()

    run(go())


def test_on_ground_rules(tmp_path: Path, fl_network: Network):
    async def go():
        clock = Clock()
        s = await make_service(tmp_path, fl_network, clock)
        lat, lon = node(fl_network, "ev-sef")
        await s.handle(SIM, vs_telemetry(lat, lon, 0.0))
        assert s.live.tracks["sim-001"].on_ground
        clock.t = T0 + timedelta(seconds=1)
        await s.handle(SIM, vs_telemetry(lat, lon, 3.0, t=clock.t))  # lifting off: vertical speed
        assert not s.live.tracks["sim-001"].on_ground
        clock.t = T0 + timedelta(seconds=2)
        await s.handle(SIM, vs_telemetry(lat, lon, 0.0, speed=30.0, t=clock.t))
        assert not s.live.tracks["sim-001"].on_ground

        # a hover (holding for a pad) on an active flight is airborne, though motion alone looks parked
        await s.store.register_device("dev-001", "passenger")
        await s.handle("aerolink/aircraft/dev-001/telemetry", telemetry("dev-001", lat, lon, clock.t))
        assert s.live.tracks["dev-001"].on_ground  # a powered unit on its pad
        rid = str(uuid4())
        out = await s.handle(
            "aerolink/aircraft/dev-001/route-request", route_request("dev-001", "ev-orl", request_id=rid)
        )
        resp = RouteResponse.model_validate_json(out[0][1])
        await s.handle(
            "aerolink/aircraft/dev-001/route-selection", selection("dev-001", rid, str(resp.routes[0].route_id))
        )
        clock.t += timedelta(seconds=1)
        await s.handle("aerolink/aircraft/dev-001/telemetry", telemetry("dev-001", lat, lon, clock.t, speed=20.0))
        assert s.flights["dev-001"]["status"] == "active" and not s.live.tracks["dev-001"].on_ground
        clock.t += timedelta(seconds=1)
        await s.handle("aerolink/aircraft/dev-001/telemetry", telemetry("dev-001", lat, lon, clock.t, speed=0.0))
        assert not s.live.tracks["dev-001"].on_ground  # still on an active flight

        # parked aircraft are not traffic for anyone, and never raise the off-route scan
        clock.t += timedelta(seconds=1)
        await s.handle("aerolink/sim/sim-002/telemetry", vs_telemetry(lat, lon, 0.0, aid="sim-002", t=clock.t))
        assert s.live.tracks["sim-002"].on_ground
        near = s.live.nearby(lat, lon, 5000.0, exclude="dev-001")
        assert "sim-002" not in {t.aircraft_id for t in near}
        assert await s.scan_off_route() == []
        await s.store.close()

    run(go())


def test_api_state_exposes_on_ground(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"

    async def prepare() -> None:
        store = Store.connect(url)
        await store.create_all()
        await store.close()

    run(prepare())
    app = create_app(Settings(database_url=url), mqtt=False)
    with TestClient(app) as c:
        svc = app.state.service

        async def feed() -> None:
            await svc.handle(SIM, vs_telemetry(28.0, -81.9, 0.0, t=svc.clock()))
            await svc.handle(
                "aerolink/sim/sim-002/telemetry",
                vs_telemetry(28.1, -81.9, 0.0, speed=40.0, aid="sim-002", t=svc.clock()),
            )

        portal_of(c).call(feed)
        rows = {r["id"]: r for r in c.get("/api/state").json()["aircraft"]}
        assert rows["sim-001"]["on_ground"] is True and rows["sim-002"]["on_ground"] is False
