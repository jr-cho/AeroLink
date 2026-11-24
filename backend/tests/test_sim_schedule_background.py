"""E3.3 T5 schedules and T6 background traffic tests."""

from __future__ import annotations

from aerolink.network import Network
from aerolink.sim import run_simulation
from aerolink.sim.background import generate_tracks, step_background
from aerolink.sim.events import SCHEDULE_CONFLICT, SCHEDULED_DEPARTURE
from aerolink.sim.scenarios import Scenario, scenario_from_dict
from aerolink.sim.world_config import BackgroundTrafficConfig, WorldToggles


def _line_scenario() -> dict:
    return {
        "name": "schedule_test",
        "duration_s": 1200,
        "dt_s": 1.0,
        "start_time": "2026-11-20T07:00:00-05:00",
        "fleet": {"passenger": 2, "medical": 0},
        "world": {"separation": True, "schedules": True},
        "pad_turnaround_time_s": 60,
    }


def test_scheduled_departure_fires_at_time(fl_network: Network):
    data = _line_scenario()
    data["schedule"] = [
        {"aircraft": "sim-p00", "origin": "ev-sef", "destination": "ev-lal", "departure_local": "07:05:00"}
    ]
    scenario = scenario_from_dict(data)
    result = run_simulation(fl_network, scenario)
    deps = [e for e in result.events if e["kind"] == SCHEDULED_DEPARTURE]
    assert len(deps) == 1
    assert deps[0]["aircraft_id"] == "sim-p00"
    assert deps[0]["destination"] == "ev-lal"


def test_scheduled_by_type_reassigns_when_busy(fl_network: Network):
    data = _line_scenario()
    data["fleet"] = {"passenger": 3, "medical": 0}
    data["schedule"] = [
        {"aircraft": "sim-p00", "origin": "ev-sef", "destination": "ev-lal", "departure_local": "07:01:00"},
        {"aircraft": "passenger", "origin": "ev-suntrax", "destination": "ev-lal", "departure_local": "07:05:00"},
    ]
    scenario = scenario_from_dict(data)
    result = run_simulation(fl_network, scenario)
    deps = [e for e in result.events if e["kind"] == SCHEDULED_DEPARTURE]
    assert len(deps) == 2
    # second departure must be on a passenger aircraft
    assert deps[1]["aircraft_id"].startswith("sim-p")


def test_schedule_conflict_logged_when_no_aircraft_available(fl_network: Network):
    data = _line_scenario()
    data["fleet"] = {"passenger": 1, "medical": 0}
    data["schedule"] = [
        {"aircraft": "sim-p00", "origin": "ev-sef", "destination": "ev-lal", "departure_local": "07:01:00"},
        {"aircraft": "sim-p00", "origin": "ev-sef", "destination": "ev-lal", "departure_local": "07:02:00"},
    ]
    scenario = scenario_from_dict(data)
    result = run_simulation(fl_network, scenario)
    conflicts = [e for e in result.events if e["kind"] == SCHEDULE_CONFLICT]
    assert len(conflicts) == 1
    assert conflicts[0]["departure_local"] == "07:02:00"


def test_background_tracks_are_deterministic(fl_network: Network):
    cfg = BackgroundTrafficConfig(count=3, start_time_s=0, duration_s=600, seed_offset=42)
    scenario = Scenario(
        duration_s=600,
        world=WorldToggles(separation=True, background_traffic=True),
        background_traffic=cfg,
    )
    a = run_simulation(fl_network, scenario)
    b = run_simulation(fl_network, scenario)
    assert a.digest() == b.digest()
    bg_ids = {rec["aircraft_id"] for rec in a.telemetry if rec["vehicle_type"] == "background"}
    assert bg_ids == {"bg-000", "bg-001", "bg-002"}


def test_background_traffic_changes_digest(fl_network: Network):
    base = Scenario(duration_s=600, world=WorldToggles(separation=True))
    with_bg = Scenario(
        duration_s=600,
        world=WorldToggles(separation=True, background_traffic=True),
        background_traffic=BackgroundTrafficConfig(count=2),
    )
    assert run_simulation(fl_network, base).digest() != run_simulation(fl_network, with_bg).digest()


def test_background_aircraft_are_in_telemetry(fl_network: Network):
    cfg = BackgroundTrafficConfig(count=3, start_time_s=0, duration_s=1200, speed_mps=50)
    scenario = Scenario(
        duration_s=1200,
        world=WorldToggles(separation=True, background_traffic=True),
        background_traffic=cfg,
    )
    result = run_simulation(fl_network, scenario)
    bg_ids = {rec["aircraft_id"] for rec in result.telemetry if rec["vehicle_type"] == "background"}
    assert bg_ids == {"bg-000", "bg-001", "bg-002"}


def test_generate_tracks_honours_count(fl_network: Network):
    import numpy as np

    rng = np.random.default_rng(7)
    cfg = BackgroundTrafficConfig(count=4)
    tracks = generate_tracks(fl_network, cfg, rng)
    assert len(tracks) == 4
    for tr in tracks:
        assert len(tr.path) >= 2
        assert len(tr.times_s) == len(tr.path)


def test_step_background_samples_track(fl_network: Network):
    import numpy as np

    rng = np.random.default_rng(1)
    cfg = BackgroundTrafficConfig(count=1, start_time_s=0, duration_s=600, speed_mps=50)
    tracks = generate_tracks(fl_network, cfg, rng)
    assert tracks
    states = step_background(tracks, tracks[0].times_s[0] + 1.0, fl_network)
    live = [s for s in states if not s.finished]
    assert live
    assert live[0].velocity_mps > 0
