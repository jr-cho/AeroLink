"""Energy model and reserve-based behaviour (PLAN E3.3 T2)."""

from __future__ import annotations

import numpy as np
import pytest

from aerolink.network import Network
from aerolink.sim import physics as ph
from aerolink.sim.controller import ControllerConfig
from aerolink.sim.energy import step_energy_drain, trip_energy_estimate
from aerolink.sim.pads import PadState
from aerolink.sim.pilot import Pilot
from aerolink.sim.state import AircraftState, SimPosition, WindField, WorldState
from aerolink.sim.world_config import EnergyConfig, WorldToggles
from aerolink.vehicle import vehicle_cfg

CALM = WindField(speed_mps=0.0, gust_fraction=0.0)


def _make_ac(net: Network, path: list[str], speed: float = 60.0, vtype: str = "passenger") -> AircraftState:
    from aerolink.vehicle import as_vehicle_type

    n0 = net.nodes[path[0]]
    ac = AircraftState(
        aircraft_id="t-1",
        vehicle_type=as_vehicle_type(vtype),
        home_node=path[0],
        status="climbing",
        current_node=None,
        position=SimPosition(n0["lat"], n0["lon"], ph.PAD_ALT_M),
        rng=np.random.default_rng(0),
        telemetry_rng=np.random.default_rng(1),
    )
    ac.legs = ph.build_legs(path, net, speed)
    return ac


def _fly(
    ac: AircraftState,
    wind: WindField = CALM,
    dt: float = 1.0,
    stop: float = 0.0,
    energy: EnergyConfig | None = None,
):
    while True:
        r = ph.advance(ac, wind, dt, stop, energy=energy)
        if r.at_stop:
            return


# ------------------------------------------------------------------- energy model


def test_energy_ordering():
    """hover/landing > climb > cruise"""
    v = vehicle_cfg("passenger")
    cruise_per_s = v.cruise_soc_per_m * v.cruise_speed_mps
    assert v.hover_soc_per_s > v.climb_soc_per_s > cruise_per_s


def test_trip_energy_includes_transition(line_network: Network):
    _make_ac(line_network, ["a", "w1"])
    total = line_network.edge_between("a", "w1").length_m
    energy = trip_energy_estimate(line_network, ["a", "w1"], "passenger", EnergyConfig())
    vehicle = vehicle_cfg("passenger")
    assert energy > total * vehicle.cruise_soc_per_m
    assert energy == pytest.approx(total * vehicle.cruise_soc_per_m + vehicle.transition_soc)


def test_step_drain_phase_ordering(line_network: Network):
    energy = EnergyConfig()
    vehicle = vehicle_cfg("passenger")
    dt = 1.0

    # holding: per-second hover drain
    ac = _make_ac(line_network, ["a", "w1"])
    ac.status = "holding"
    hover = step_energy_drain(ac, 0.0, dt, energy, CALM)
    assert hover == pytest.approx(vehicle.hover_soc_per_s * dt)

    # climbing: includes climb + horizontal cruise drain
    ac2 = _make_ac(line_network, ["a", "w1"])
    ac2.status = "climbing"
    climb = step_energy_drain(ac2, 10.0, dt, energy, CALM)
    assert climb > vehicle.climb_soc_per_s * dt

    # cruise: only horizontal drain
    ac3 = _make_ac(line_network, ["a", "w1"])
    ac3.status = "cruise"
    cruise = step_energy_drain(ac3, 10.0, dt, energy, CALM)
    assert cruise == pytest.approx(10.0 * vehicle.cruise_soc_per_m)

    assert hover > climb > cruise


def test_headwind_increases_cruise_drain(line_network: Network):
    energy = EnergyConfig(headwind_penalty_factor=0.5)
    ac = _make_ac(line_network, ["a", "w1"])
    ac.status = "cruise"
    tail = step_energy_drain(ac, 10.0, 1.0, energy, WindField(10, 180, 0))
    head = step_energy_drain(ac, 10.0, 1.0, energy, WindField(10, 0, 0))
    calm = step_energy_drain(ac, 10.0, 1.0, energy, CALM)
    assert head > calm
    assert calm >= tail


# ------------------------------------------------------------------- reserve / diversion


def test_low_energy_departure_refused(line_network: Network):
    """A trip that fits the nominal range but leaves less than reserve is refused."""
    energy = EnergyConfig()
    vehicle_cfg("passenger")
    ac = _make_ac(line_network, ["a", "w1", "w2", "b"])
    trip = trip_energy_estimate(line_network, ["a", "w1", "w2", "b"], "passenger", energy)
    ac.battery_soc = trip + energy.reserve_soc - 0.001  # just under required
    assert ac.battery_soc < 1.0
    ac.current_node = "a"
    ac.status = "parked"

    world = WorldState(t=0.0, aircraft={"t-1": ac}, pads={}, rng=np.random.default_rng(0))
    world.pads = {
        "a": PadState("a", 1, occupants=["t-1"]),
        "b": PadState("b", 1),
    }
    cfg = ControllerConfig(world=WorldToggles(energy=True), energy=energy)
    pilot = Pilot("t-1", line_network, world, _StubAdvisor(line_network), cfg)
    ok = pilot.depart("b")
    assert not ok
    assert any(e["kind"] == "route_unavailable" for e in world.events)


def test_holding_diverts_before_reserve_is_breached(line_network: Network):
    """A long hold forces a low-charge aircraft to declare an emergency and, if no slot frees at
    the destination in time, divert (E1: later than before, at the last safe moment, not at the reserve)."""
    energy = EnergyConfig(reserve_soc=0.20, charge_rate_soc_per_s=0.001)
    vehicle = vehicle_cfg("passenger")

    # Position aircraft at the destination node b, queued and holding with low charge.
    nb = line_network.nodes["b"]
    ac = AircraftState(
        aircraft_id="t-1",
        vehicle_type="passenger",
        home_node="a",
        status="holding",
        current_node=None,
        position=SimPosition(nb["lat"], nb["lon"], ph.PAD_ALT_M),
        rng=np.random.default_rng(0),
        telemetry_rng=np.random.default_rng(1),
    )
    ac.pad_requested = True
    ac.pad_cleared = False
    ac.hold_start_s = 0.0
    # Give the aircraft enough battery for one hover time-step plus the trip to
    # c. After the first holding() tick the budget exactly covers the alternate.
    # Subtract a tiny epsilon so the reserve comparison (<= reserve + 1e-9) wins
    # despite binary-float rounding.
    dt = 1.0
    trip_c = 0.0907520939379466  # w2 -> c
    ac.battery_soc = energy.reserve_soc + trip_c + vehicle.hover_soc_per_s * dt + 1e-9 - 1e-12
    # Leave a tiny active leg so nearest_alternate_energy has a current edge to work from.
    ac.legs = ph.build_legs(["w2", "b"], line_network, 60.0)
    ac.distance_along_edge_m = 0.0
    from uuid import uuid4

    from aerolink.sim.state import FlightPlan

    ac.active_flight = FlightPlan(  # the emergency needs the flight it is on (destination b, flown from w2)
        uuid4(), uuid4(), uuid4(), ["w2", "b"], "b", 0.0, {"w2": 0.0, "b": 100.0}, flown=["w2"]
    )

    world = WorldState(t=0.0, aircraft={"t-1": ac}, pads={}, rng=np.random.default_rng(0))
    world.pads = {
        "b": PadState("b", 1, occupants=["other"], queue=["t-1"]),
        "a": PadState("a", 1),
        "c": PadState("c", 2),  # an emergency diverts only to a site with a free pad
    }
    cfg = ControllerConfig(world=WorldToggles(energy=True), energy=energy, max_hold_s=600.0)
    pilot = Pilot("t-1", line_network, world, _StubAdvisor(line_network), cfg)

    diverted = False
    for _ in range(300):
        pilot.holding()
        world.t += cfg.dt_s
        if any(e["kind"] == "diversion" for e in world.events):
            diverted = True
            break
    assert diverted


# ------------------------------------------------------------------- charging


def test_turnaround_recharges_gradually(line_network: Network):
    energy = EnergyConfig(charge_rate_soc_per_s=0.001)
    ac = _make_ac(line_network, ["a", "w1"])
    ac.status = "turnaround"
    ac.current_node = "a"
    ac.battery_soc = 0.5
    ac.turnaround_until_s = 100.0

    world = WorldState(
        t=0.0,
        aircraft={"t-1": ac},
        pads={"a": PadState("a", 1, occupants=["t-1"])},
        rng=np.random.default_rng(0),
    )
    cfg = ControllerConfig(world=WorldToggles(energy=True), energy=energy)
    pilot = Pilot("t-1", line_network, world, _StubAdvisor(line_network), cfg)

    for _ in range(100):
        pilot.turnaround()
        world.t += cfg.dt_s

    assert ac.battery_soc > 0.5
    assert ac.battery_soc <= 1.0


# ------------------------------------------------------------------- helpers


class _StubAdvisor:
    def __init__(self, network: Network):
        self.network = network

    def request_routes(self, origin: str, destination: str, vehicle_type: str, t_s: float):
        from aerolink.vehicle import as_vehicle_type

        return self.network.shortest_paths(
            origin,
            destination,
            vehicle_type=as_vehicle_type(vehicle_type),
            k=1,
        )

    def suggest(self, ac: AircraftState, from_node: str, t_s: float):
        return None
