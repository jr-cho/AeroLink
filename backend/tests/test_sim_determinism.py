"""End-to-end runs on the committed network: determinism, contract validity,
physical invariants and performance (PLAN E3.2 acceptance criteria)."""

import itertools
import json
import math
import time
from collections import defaultdict
from pathlib import Path

import pytest
from conftest import validator

from aerolink.contracts import Telemetry
from aerolink.network import Network
from aerolink.sim import physics as ph
from aerolink.sim import run_simulation
from aerolink.sim.demand import DemandConfig
from aerolink.sim.scenarios import SCENARIO_DIR, default_scenario, load_scenario
from aerolink.sim.telemetry import TelemetryNoise

HOUR = default_scenario()
STRESS = HOUR.with_overrides(
    name="stress",
    demand=DemandConfig(peak_rate_per_hour=6.0, off_peak_rate_per_hour=6.0, medical_dispatch_mean_interval_s=300),
    destination_change_probability=0.6,  # enough mid-route changes to exercise the path
    max_hold_s=120,
    pad_turnaround_time_s=120,
)


def congested_scenario(net: Network):
    """One slot per vertiport and a fleet that fills them: forces holds and diversions."""
    return STRESS.with_overrides(
        name="congested",
        seed=4,  # a seed that holds and diverts on central-fl-v1 (re-pick if the network changes)
        pad_capacity={n: 1 for n in net.nodes_for("passenger")},
        stand_capacity={n: 0 for n in net.nodes_for("passenger")},  # pad-only: parked aircraft block
        fleet={"passenger": 6, "medical": 3},
        demand=DemandConfig(
            peak_rate_per_hour=6,
            off_peak_rate_per_hour=6,
            return_home_probability=0.0,
            medical_dispatch_mean_interval_s=600,
        ),
        duration_s=7200,
    )


@pytest.fixture(scope="module")
def congested(fl_network: Network):
    return run_simulation(fl_network, congested_scenario(fl_network))


@pytest.fixture(scope="module")
def hour(fl_network: Network):
    return run_simulation(fl_network, HOUR)


@pytest.fixture(scope="module")
def stress(fl_network: Network):
    return run_simulation(fl_network, STRESS)


# ------------------------------------------------------------- determinism
def test_same_seed_identical_output(fl_network: Network, hour):
    again = run_simulation(fl_network, HOUR)
    assert again.telemetry == hour.telemetry
    assert again.events == hour.events
    assert again.truth == hour.truth
    assert again.digest() == hour.digest()


def test_different_seed_differs(fl_network: Network, hour):
    assert run_simulation(fl_network, HOUR, seed=43).digest() != hour.digest()


def test_seed_argument_overrides_scenario(fl_network: Network, hour):
    assert run_simulation(fl_network, HOUR.with_overrides(seed=1), seed=HOUR.seed).digest() == hour.digest()


def test_telemetry_noise_does_not_change_trajectories(fl_network: Network, hour):
    quiet = run_simulation(fl_network, HOUR.with_overrides(telemetry_noise=TelemetryNoise(0, 0, 0.3)))
    assert quiet.truth == hour.truth
    assert quiet.events == hour.events
    assert len(quiet.telemetry) < len(hour.telemetry)


def test_stress_is_deterministic(fl_network: Network, stress):
    assert run_simulation(fl_network, STRESS).digest() == stress.digest()


# ------------------------------------------------------------- performance
def test_one_hour_runs_fast(fl_network: Network):
    t0 = time.perf_counter()
    r = run_simulation(fl_network, HOUR, record_truth=False)
    assert time.perf_counter() - t0 < 60.0  # acceptance criterion; actual ~0.3 s
    assert r.steps == 3600 and r.truth == []


# ---------------------------------------------------------------- contract
@pytest.mark.parametrize("which", ["hour", "stress", "congested"])
def test_all_telemetry_is_contract_valid(which, request):
    r = request.getfixturevalue(which)
    assert r.telemetry
    schema = validator("telemetry")
    for rec in r.telemetry[:: max(1, len(r.telemetry) // 500)]:
        schema.validate(rec)
    for rec in r.telemetry:
        Telemetry.model_validate_json(json.dumps(rec))


def test_telemetry_ids_unique_and_timestamps_monotonic(stress):
    assert len({r["message_id"] for r in stress.telemetry}) == len(stress.telemetry)
    by_ac = defaultdict(list)
    for r in stress.telemetry:
        by_ac[r["aircraft_id"]].append(r["timestamp"])
    for ts in by_ac.values():
        assert ts == sorted(ts) and len(set(ts)) == len(ts)


def test_telemetry_only_while_airborne(stress):
    airborne = {(t["t"], t["aircraft_id"]) for t in stress.truth if t["status"] not in ("parked", "turnaround")}
    assert len(stress.telemetry) == len(airborne)


# -------------------------------------------------------------- invariants
def _per_aircraft(truth):
    out = defaultdict(list)
    for row in truth:
        out[row["aircraft_id"]].append(row)
    return out


@pytest.mark.parametrize("which", ["hour", "stress", "congested"])
def test_kinematic_limits(which, request):
    r = request.getfixturevalue(which)
    dt = r.scenario.dt_s
    for rows in _per_aircraft(r.truth).values():
        for a, b in itertools.pairwise(rows):
            dv = b["velocity_mps"] - a["velocity_mps"]
            assert -ph.MAX_DECEL_MPS2 * dt - 1e-6 <= dv <= ph.MAX_ACCEL_MPS2 * dt + 1e-6, (a, b)
            if b["status"] not in ("parked", "turnaround") and a["status"] not in ("parked", "turnaround", "landing"):
                dz = b["alt_msl_m"] - a["alt_msl_m"]
                assert -ph.MAX_DESCENT_MPS * dt - 1e-6 <= dz <= ph.MAX_CLIMB_MPS * dt + 1e-6, (a, b)
            assert 0 <= b["velocity_mps"] <= ph.MAX_GROUND_SPEED_MPS
            assert 0 <= b["battery_soc"] <= 1


@pytest.mark.parametrize("which", ["hour", "stress", "congested"])
def test_positions_stay_near_network_and_move_continuously(which, request, fl_network: Network):
    r = request.getfixturevalue(which)
    lats = [n["lat"] for n in fl_network.nodes.values()]
    lons = [n["lon"] for n in fl_network.nodes.values()]
    for rows in _per_aircraft(r.truth).values():
        for a, b in itertools.pairwise(rows):
            assert min(lats) - 0.05 <= b["lat"] <= max(lats) + 0.05
            assert min(lons) - 0.05 <= b["lon"] <= max(lons) + 0.05
            jump = ph.segment_length_m((a["lon"], a["lat"]), (b["lon"], b["lat"]))
            # Planning length vs polyline length differ slightly, so allow headroom.
            assert jump <= 1.5 * ph.MAX_GROUND_SPEED_MPS * r.scenario.dt_s + 1.0, (a, b)


@pytest.mark.parametrize("which", ["hour", "stress", "congested"])
def test_flight_event_sequences_are_consistent(which, request):
    r = request.getfixturevalue(which)
    state: dict[str, str] = {}
    open_flights: dict[str, str] = {}
    for e in r.events:
        aid = e.get("aircraft_id")
        k = e["kind"]
        if k == "departure":
            assert state.get(aid) not in ("arrival", "landing")  # turnaround must finish first
            assert aid not in open_flights
            open_flights[aid] = e["flight_id"]
        elif k in (
            "pad_requested",
            "hold_start",
            "hold_end",
            "landing",
            "diversion",
            "destination_change",
            "suggestion",
        ):
            assert aid in open_flights, e
        elif k == "arrival":
            assert open_flights.pop(aid) == e["flight_id"]
        elif k in ("ready", "trip_rejected"):
            assert aid not in open_flights
        state[aid] = k
    flight_ids = [e["flight_id"] for e in r.events if e["kind"] == "departure"]
    assert len(set(flight_ids)) == len(flight_ids)


@pytest.mark.parametrize("which", ["stress", "congested"])
def test_pad_capacity_never_exceeded(which, request, fl_network: Network):
    from aerolink.sim.pads import init_pads

    stress = request.getfixturevalue(which)
    sc = stress.scenario
    pads = init_pads(fl_network, sc.pad_capacity, sc.stand_capacity, default_stand_count=True)
    caps = {n: p.capacity + p.stands for n, p in pads.items()}  # aircraft on the ground at a site
    by_t = defaultdict(lambda: defaultdict(int))
    for row in stress.truth:
        if row["node"] is not None:
            by_t[row["t"]][row["node"]] += 1
    for counts in by_t.values():
        for node, n in counts.items():
            assert n <= caps[node], (node, n)
    for p in stress.final_world.pads.values():
        assert len(p.occupants) <= p.capacity and len(p.stand_occupants) <= p.stands


def test_stress_exercises_every_behaviour(stress):
    seen = set(stress.stats["event_counts"])
    for kind in ("departure", "arrival", "pad_requested", "landing", "ready", "medical_incident", "destination_change"):
        assert kind in seen, kind
    purposes = {e["purpose"] for e in stress.events if e["kind"] == "arrival"}
    assert {"passenger", "medical_dispatch"} <= purposes


def test_congestion_holds_and_diverts(congested):
    seen = congested.stats["event_counts"]
    assert seen.get("hold_start", 0) > 0 and seen.get("diversion", 0) > 0
    for e in congested.events:
        if e["kind"] == "diversion":
            assert e["from_node"] != e["to_node"]
    diverted = [e for e in congested.events if e["kind"] == "arrival" and e["diverted"]]
    assert diverted


def test_landed_aircraft_are_at_their_pad(stress, fl_network: Network):
    for row in stress.truth:
        if row["status"] in ("parked", "turnaround"):
            node = fl_network.nodes[row["node"]]
            assert row["lat"] == pytest.approx(node["lat"]) and row["lon"] == pytest.approx(node["lon"])
            assert row["velocity_mps"] == 0


def test_medical_response_times_recorded(stress):
    arr = [e for e in stress.events if e["kind"] == "arrival" and e["purpose"] == "medical_dispatch"]
    assert arr and all(e["response_time_s"] >= e["flight_time_s"] for e in arr)


def test_medical_aircraft_only_visit_medical_sites(stress, fl_network: Network):
    for e in stress.events:
        if e["kind"] == "departure":
            vtype = "medical" if e["aircraft_id"].startswith("sim-m") else "passenger"
            assert vtype in fl_network.serves(e["destination"])


# ---------------------------------------------------------------- variants
@pytest.mark.parametrize("dt", [0.5, 2.0])
def test_other_time_steps(fl_network: Network, dt):
    r = run_simulation(fl_network, HOUR.with_overrides(dt_s=dt, duration_s=1200))
    assert r.steps == math.ceil(1200 / dt)
    for rec in r.telemetry[:200]:
        Telemetry.model_validate_json(json.dumps(rec))


def test_empty_fleet_runs(fl_network: Network):
    r = run_simulation(fl_network, HOUR.with_overrides(fleet={"passenger": 0, "medical": 0}, duration_s=60))
    assert r.telemetry == [] and r.truth == []


def test_no_wind_no_demand_is_quiet(fl_network: Network):
    from aerolink.sim.scenarios import WindConfig

    r = run_simulation(
        fl_network,
        HOUR.with_overrides(
            wind=WindConfig(0, 0, 0),
            demand=DemandConfig(peak_rate_per_hour=0, off_peak_rate_per_hour=0, medical_dispatch_mean_interval_s=0),
        ),
    )
    assert r.events == [] and r.telemetry == []


def test_committed_scenario_runs(fl_network: Network):
    s = load_scenario(SCENARIO_DIR / "nominal_day.yaml").with_overrides(duration_s=600)
    assert run_simulation(fl_network, s).steps == 600


_DIGEST_SCRIPT = """
import dataclasses, sys
from aerolink.network import Network
from aerolink.sim import load_scenario, run_simulation
from aerolink.sim.scenarios import DEFAULT_NETWORK
sc = dataclasses.replace(load_scenario(sys.argv[1]), duration_s=1800)
print(run_simulation(Network.from_geojson(DEFAULT_NETWORK), sc, seed=7).digest())
"""


def test_digest_identical_across_processes_and_hash_seeds():
    """Same seed -> same digest in fresh interpreters with different PYTHONHASHSEED
    (catches iteration over sets/dicts keyed by str hashes)."""
    import os
    import subprocess
    import sys

    scenario = str(Path(__file__).resolve().parents[1] / "scenarios" / "nominal_day.yaml")
    digests = set()
    for hash_seed in ("0", "1", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        out = subprocess.run(
            [sys.executable, "-c", _DIGEST_SCRIPT, scenario], env=env, capture_output=True, text=True, check=True
        )
        digests.add(out.stdout.strip())
    assert len(digests) == 1, digests
