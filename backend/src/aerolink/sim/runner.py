"""Fixed-step simulation loop (PLAN E3.2).

``run_simulation(network, scenario, seed)`` is a pure function: the same inputs
always give byte-identical telemetry and events (see ``SimulationResult.digest``).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import numpy as np

from aerolink.contracts import SCHEMA_VERSION
from aerolink.network import Network
from aerolink.sim.background import (
    BackgroundAircraft,
    BackgroundState,
    generate_tracks,
    step_background,
)
from aerolink.sim.clock import SimClock
from aerolink.sim.controller import Controller, ControllerConfig, RouteAdvisor, ShortestPathAdvisor
from aerolink.sim.fleet import create_fleet
from aerolink.sim.invariants import InvariantChecker, Violation
from aerolink.sim.pads import init_pads
from aerolink.sim.scenarios import Scenario
from aerolink.sim.state import WindField, WorldState
from aerolink.sim.telemetry import telemetry_record


@dataclass
class SimulationResult:
    scenario: Scenario
    seed: int
    telemetry: list[dict]  # noisy, contract-shaped Telemetry records
    events: list[dict]  # structured event log
    truth: list[dict]  # noise-free per-step state of every aircraft
    final_world: WorldState
    steps: int = 0
    stats: dict = field(default_factory=dict)
    violations: list[Violation] = field(default_factory=list)  # only with check_invariants=True

    def digest(self, precision: int = 6) -> str:
        """SHA-256 over telemetry + events + truth; equal iff runs are identical.

        Floats are rounded to ``precision`` decimal places so tiny platform
        differences in numpy/libm trig/exp don't break cross-platform tests.
        """
        h = hashlib.sha256()
        for part in (self.telemetry, self.events, self.truth):
            h.update(json.dumps(_round_floats(part, precision), sort_keys=True, separators=(",", ":")).encode())
        return h.hexdigest()


def _round_floats(obj, precision: int):
    """Recursively round floats in dicts/lists for stable digests."""
    if isinstance(obj, float):
        return round(obj, precision)
    if isinstance(obj, dict):
        return {k: _round_floats(v, precision) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, precision) for v in obj]
    return obj


def build_world(network: Network, scenario: Scenario, seed: int) -> WorldState:
    rng = np.random.default_rng(seed)
    pads = init_pads(network, scenario.pad_capacity, scenario.stand_capacity, default_stand_count=True)
    fleet = create_fleet(
        network,
        pads,
        rng,
        passenger=scenario.fleet.get("passenger", 0),
        medical=scenario.fleet.get("medical", 0),
    )
    wind = WindField(
        speed_mps=scenario.wind.speed_mps,
        direction_deg=scenario.wind.direction_deg,
        gust_fraction=scenario.wind.gust_fraction,
    )
    return WorldState(t=0.0, aircraft=fleet, pads=pads, rng=rng, wind=wind)


def run_simulation(
    network: Network,
    scenario: Scenario,
    seed: int | None = None,
    advisor: RouteAdvisor | None = None,
    record_truth: bool = True,
    check_invariants: bool = False,
) -> SimulationResult:
    """Run ``scenario`` for ``duration_s``. ``seed`` overrides ``scenario.seed``.

    ``check_invariants`` runs the read-only ``InvariantChecker`` after every
    step and stores what it finds in ``result.violations``; output is unchanged.
    """
    seed = scenario.seed if seed is None else seed
    clock = SimClock.from_iso(scenario.start_time, dt_s=scenario.dt_s)
    world = build_world(network, scenario, seed)
    background_aircraft: list[BackgroundAircraft] = []
    background_states: list[BackgroundState] = []
    if scenario.world.background_traffic:
        if scenario.background_traffic.replay_file:
            from aerolink.realdata.replay import load_replay  # real flights (PLAN E5.1)

            background_aircraft = load_replay(
                Path(scenario.background_traffic.replay_file),
                network,
                scenario.start_time,
                scenario.duration_s,
                limit=scenario.background_traffic.count,
            )
        else:
            background_aircraft = generate_tracks(network, scenario.background_traffic, world.rng)
    cfg = ControllerConfig(
        dt_s=scenario.dt_s,
        start_local_hour=clock.local_hour(),
        turnaround_s=scenario.pad_turnaround_time_s,
        max_hold_s=scenario.max_hold_s,
        suggestion_acceptance_rate=scenario.suggestion_acceptance_rate,
        destination_change_probability=scenario.destination_change_probability,
        cruise_speed_sigma=scenario.cruise_speed_sigma,
        demand=scenario.demand,
        world=scenario.world,
        separation=scenario.separation,
        energy=scenario.energy,
        pad_booking=scenario.pad_booking,
        schedule=scenario.schedule,
        background_traffic=scenario.background_traffic,
    )
    controller = Controller(
        network,
        world,
        clock,
        advisor or ShortestPathAdvisor(network, k=scenario.route_options),
        cfg,
        background_aircraft=background_aircraft,
    )
    bind = getattr(controller.advisor, "bind", None)  # route engines that read live traffic (PLAN E5.7)
    if callable(bind):
        bind(world, controller)
    telemetry: list[dict] = []
    truth: list[dict] = []
    steps = math.ceil(scenario.duration_s / scenario.dt_s - 1e-9)
    checker = (
        InvariantChecker(network, scenario.dt_s, separation=cfg.separation if cfg.world.separation else None)
        if check_invariants
        else None
    )
    if checker is not None:
        checker.check_step(world)
    for _ in range(steps):
        if scenario.world.background_traffic:
            background_states = step_background(background_aircraft, world.t, network)
        controller.step()
        if scenario.world.background_traffic:
            ts = controller.clock.now()
            for st in background_states:
                if st.finished:
                    continue
                telemetry.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "message_id": str(UUID(bytes=world.rng.bytes(16), version=4)),
                        "aircraft_id": st.aircraft_id,
                        "vehicle_type": "background",
                        "timestamp": ts.isoformat().replace("+00:00", "Z"),
                        "position": {
                            "lat": round(st.position.lat, 8),
                            "lon": round(st.position.lon, 8),
                            "alt_msl_m": round(st.position.alt_msl_m, 2),
                        },
                        "motion": {
                            "ground_speed_mps": round(st.velocity_mps, 2),
                            "course_deg": round(st.heading_deg, 2),
                            "vertical_speed_mps": round(st.vertical_speed_mps, 2),
                        },
                        "fix": {"type": "3d", "hdop": 0.9, "satellites": 11, "replayed": True},
                        "active_flight_id": None,
                    }
                )
                if record_truth:
                    truth.append(
                        {
                            "t": round(world.t, 6),
                            "aircraft_id": st.aircraft_id,
                            "status": "background",
                            "node": None,
                            "lat": st.position.lat,
                            "lon": st.position.lon,
                            "alt_msl_m": st.position.alt_msl_m,
                            "velocity_mps": st.velocity_mps,
                            "vertical_speed_mps": st.vertical_speed_mps,
                            "heading_deg": st.heading_deg,
                            "battery_soc": 1.0,
                        }
                    )
        if checker is not None:
            checker.check_step(world)
        ts = controller.clock.now()
        for aid in sorted(world.aircraft):
            ac = world.aircraft[aid]
            rec = telemetry_record(ac, ts, scenario.telemetry_noise)
            if rec is not None:
                telemetry.append(rec)
            if record_truth:
                truth.append(
                    {
                        "t": round(world.t, 6),
                        "aircraft_id": aid,
                        "status": ac.status,
                        "node": ac.current_node,
                        "lat": ac.position.lat,
                        "lon": ac.position.lon,
                        "alt_msl_m": ac.position.alt_msl_m,
                        "velocity_mps": ac.velocity_mps,
                        "vertical_speed_mps": ac.vertical_speed_mps,
                        "heading_deg": ac.heading_deg,
                        "battery_soc": ac.battery_soc,
                    }
                )
    if checker is not None:
        checker.check_events(world.events, world)
    kinds: dict[str, int] = {}
    for e in world.events:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    return SimulationResult(
        scenario=scenario,
        seed=seed,
        telemetry=telemetry,
        events=world.events,
        truth=truth,
        final_world=world,
        steps=steps,
        stats={"event_counts": dict(sorted(kinds.items())), "telemetry_records": len(telemetry)},
        violations=checker.violations if checker is not None else [],
    )
