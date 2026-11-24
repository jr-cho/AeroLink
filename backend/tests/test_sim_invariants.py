"""The invariant checker (hardening plan §4): clean runs pass, injected faults are caught.

Each fault test runs the real simulator to a state with an aircraft in a given
phase, corrupts one thing, and asserts the checker names the right rule. A
checker that misses these would let the stress matrix pass vacuously.
"""

from __future__ import annotations

import dataclasses

import pytest

from aerolink.network import Network
from aerolink.sim import events as evlib
from aerolink.sim import pads as padlib
from aerolink.sim import run_simulation
from aerolink.sim.clock import SimClock
from aerolink.sim.controller import Controller, ControllerConfig, ShortestPathAdvisor
from aerolink.sim.invariants import InvariantChecker
from aerolink.sim.physics import MAX_ACCEL_MPS2, MAX_GROUND_SPEED_MPS
from aerolink.sim.runner import build_world
from aerolink.sim.scenarios import Scenario
from aerolink.sim.state import AircraftState, PadState


class Harness:
    def __init__(self, network: Network, seed: int = 3):
        sc = Scenario()
        self.world = build_world(network, sc, seed)
        clock = SimClock.from_iso(sc.start_time, dt_s=sc.dt_s)
        cfg = ControllerConfig(dt_s=sc.dt_s, start_local_hour=clock.local_hour(), demand=sc.demand)
        self.ctl = Controller(network, self.world, clock, ShortestPathAdvisor(network), cfg)
        self.checker = InvariantChecker(network, sc.dt_s)
        self.checker.check_step(self.world)

    def step(self) -> None:
        self.ctl.step()
        self.checker.check_step(self.world)

    def run_until(self, pred, limit: int = 7200) -> AircraftState:
        for _ in range(limit):
            self.step()
            for aid in sorted(self.world.aircraft):
                ac = self.world.aircraft[aid]
                if pred(ac):
                    return ac
        raise AssertionError("state never reached")

    def rules(self) -> set[str]:
        return {v.rule for v in self.checker.violations}

    def recheck(self) -> set[str]:
        self.checker.check_step(self.world)
        return self.rules()


def cruising(ac: AircraftState) -> bool:
    return ac.status == "cruise" and ac.velocity_mps > 20


@pytest.fixture
def h(fl_network: Network) -> Harness:
    return Harness(fl_network)


# ---------------------------------------------------------------- clean runs
@pytest.mark.parametrize("seed", [0, 1, 42])
def test_clean_hour_has_no_violations(fl_network: Network, seed: int):
    r = run_simulation(fl_network, Scenario(duration_s=3600), seed=seed, check_invariants=True)
    assert r.violations == [], "\n".join(map(str, r.violations[:20]))


def test_checker_does_not_change_output(fl_network: Network):
    sc = Scenario(duration_s=900)
    assert run_simulation(fl_network, sc, check_invariants=True).digest() == run_simulation(fl_network, sc).digest()


def test_harness_baseline_clean(h: Harness):
    h.run_until(cruising)
    assert h.rules() == set()


# ---------------------------------------------------------------- kinematics
def test_detects_teleport(h: Harness):
    ac = h.run_until(cruising)
    ac.position = dataclasses.replace(ac.position, lat=ac.position.lat + 0.05)
    assert "displacement" in h.recheck()


def test_detects_overspeed(h: Harness):
    ac = h.run_until(cruising)
    ac.velocity_mps = MAX_GROUND_SPEED_MPS + 1
    assert {"ground_speed_range", "accel_limit"} <= h.recheck()


def test_detects_hard_acceleration(h: Harness):
    ac = h.run_until(cruising)
    ac.velocity_mps += MAX_ACCEL_MPS2 * 2
    assert "accel_limit" in h.recheck()


def test_detects_vertical_jump(h: Harness):
    ac = h.run_until(cruising)
    ac.position = dataclasses.replace(ac.position, alt_msl_m=ac.position.alt_msl_m - 50)
    assert "vertical_rate" in h.recheck()


def test_detects_below_pad_alt(h: Harness):
    ac = h.run_until(cruising)
    h.checker._prev.pop(ac.aircraft_id)  # isolate the rule from rate checks
    ac.position = dataclasses.replace(ac.position, alt_msl_m=5.0)
    assert "below_pad_alt" in h.recheck()


def test_detects_climb_above_band(h: Harness):
    ac = h.run_until(cruising)
    h.checker._prev.pop(ac.aircraft_id)
    h.checker.check_step(h.world)
    ac.position = dataclasses.replace(ac.position, alt_msl_m=ac.position.alt_msl_m + 4.0)
    leg = ac.leg
    assert leg is not None
    ac.legs[ac.leg_index] = dataclasses.replace(leg, cruise_alt_m=0.0)
    edge = dataclasses.replace(leg.edge, band_ceiling_ft=0.0)
    ac.legs[ac.leg_index] = dataclasses.replace(ac.legs[ac.leg_index], edge=edge)
    assert "above_band" in h.recheck()


# ---------------------------------------------------------------- state machine
def test_detects_illegal_transition(h: Harness):
    ac = h.run_until(cruising)
    ac.status = "turnaround"
    assert "status_transition" in h.recheck()


def test_detects_airborne_without_plan(h: Harness):
    ac = h.run_until(cruising)
    ac.active_flight = None
    assert "airborne_without_plan" in h.recheck()


def test_detects_broken_leg_chain(h: Harness):
    ac = h.run_until(lambda a: cruising(a) and len(a.legs) - a.leg_index >= 2)
    i = ac.leg_index + 1
    ac.legs[i] = dataclasses.replace(ac.legs[i], start_node="nowhere")
    assert "legs_contiguous" in h.recheck()


def test_detects_wrong_destination(h: Harness):
    ac = h.run_until(cruising)
    plan = ac.active_flight
    assert plan is not None
    ac.active_flight = dataclasses.replace(plan, destination_node="elsewhere")
    assert "legs_end_at_destination" in h.recheck()


def test_detects_site_transit(h: Harness, fl_network: Network):
    ac = h.run_until(lambda a: cruising(a) and len(a.legs) - a.leg_index >= 2)
    site = next(n for n in sorted(h.world.pads) if n != ac.active_flight.destination_node)  # ty: ignore[missing-union-attribute]
    i = ac.leg_index
    ac.legs[i] = dataclasses.replace(ac.legs[i], end_node=site)
    ac.legs[i + 1] = dataclasses.replace(ac.legs[i + 1], start_node=site)
    assert "site_transit" in h.recheck()


# ---------------------------------------------------------------- energy
def test_detects_soc_out_of_range(h: Harness):
    ac = h.run_until(cruising)
    ac.battery_soc = 1.5
    assert "soc_range" in h.recheck()


def test_detects_depleted_battery_airborne(h: Harness):
    ac = h.run_until(cruising)
    ac.battery_soc = 0.0
    assert {"battery_depleted", "range_guard"} <= h.recheck()


def test_detects_route_beyond_range(h: Harness):
    ac = h.run_until(cruising)
    ac.battery_soc = 0.01  # 1.6 km left
    assert ac.remaining_route_m() > 1600
    assert "range_guard" in h.recheck()


# ---------------------------------------------------------------- pads
def _parked(h: Harness) -> AircraftState:
    return next(a for a in (h.world.aircraft[k] for k in sorted(h.world.aircraft)) if a.status == "parked")


def test_detects_pad_over_capacity(h: Harness):
    ac = _parked(h)
    pad = _site(h, ac)
    pad.occupants.extend(["ghost"] * (pad.capacity + 1))
    assert "pad_capacity" in h.recheck()


def test_detects_leaked_slot(h: Harness):
    ac = h.run_until(cruising)
    other = next(n for n in sorted(h.world.pads) if n != ac.active_flight.destination_node)  # ty: ignore[missing-union-attribute]
    h.world.pads[other].occupants.append(ac.aircraft_id)
    assert "leaked_slot" in h.recheck()


def _site(h: Harness, ac: AircraftState) -> PadState:
    assert ac.current_node is not None
    return h.world.pads[ac.current_node]


def _on_pad(h: Harness) -> AircraftState:
    """A parked aircraft moved from its stand onto a pad slot."""
    ac = _parked(h)
    site = _site(h, ac)
    padlib.leave_stand(site, ac.aircraft_id)
    padlib.occupy(site, ac.aircraft_id)
    ac.on_stand = False
    assert h.recheck() == set()
    return ac


def test_fleet_starts_on_stands(h: Harness):
    assert all(a.on_stand for a in h.world.aircraft.values())


def test_detects_parked_without_slot(h: Harness):
    ac = _on_pad(h)
    _site(h, ac).occupants.remove(ac.aircraft_id)
    assert "on_pad_not_occupant" in h.recheck()


def test_detects_on_stand_without_stand(h: Harness):
    ac = _parked(h)
    _site(h, ac).stand_occupants.remove(ac.aircraft_id)
    assert "on_pad_not_occupant" in h.recheck()


def test_detects_stale_queue_entry(h: Harness):
    ac = _on_pad(h)
    _site(h, ac).queue.append(ac.aircraft_id)
    assert {"stale_queue_entry", "queued_and_occupying"} <= h.recheck()


def test_detects_stand_and_pad(h: Harness):
    ac = _parked(h)
    _site(h, ac).occupants.append(ac.aircraft_id)
    assert {"stand_and_pad", "leaked_slot"} <= h.recheck()


def test_detects_stand_over_capacity(h: Harness):
    ac = _parked(h)
    site = _site(h, ac)
    site.stand_occupants.extend(f"ghost{i}" for i in range(site.stands + 1))
    assert "stand_capacity" in h.recheck()


def test_detects_stale_stand(h: Harness):
    ac = h.run_until(cruising)
    h.world.pads[sorted(h.world.pads)[0]].stand_occupants.append(ac.aircraft_id)
    assert "stale_stand" in h.recheck()


def test_detects_airborne_on_stand(h: Harness):
    ac = h.run_until(cruising)
    ac.on_stand = True
    assert "airborne_on_stand" in h.recheck()


def test_detects_waiting_off_stand(h: Harness):
    ac = _on_pad(h)
    ac.departure_waiting = True
    assert "waiting_off_stand" in h.recheck()


def test_detects_parked_off_pad(h: Harness):
    ac = _parked(h)
    ac.position = dataclasses.replace(ac.position, lat=ac.position.lat + 1e-4)
    assert "on_pad_position" in h.recheck()


def test_detects_medical_stranded(fl_network: Network):
    h = Harness(fl_network)
    med = next(a for a in (h.world.aircraft[k] for k in sorted(h.world.aircraft)) if a.vehicle_type == "medical")
    assert med.current_node is not None
    med.home_node = "no-such-node"  # parked away from a home it cannot reach
    h.checker._home_reachable[(med.current_node, "no-such-node")] = False
    assert "medical_stranded" in h.recheck()


# ---------------------------------------------------------------- events
def _ev(t: float, kind: str, aid: str | None = "sim-p00") -> dict:
    return {"t": t, "kind": kind, "aircraft_id": aid}


def _checked(h: Harness, events: list[dict]) -> set[str]:
    h.checker.check_events(events, h.world)
    return h.rules()


def test_events_clean(h: Harness):
    assert _checked(h, [_ev(1, evlib.DEPARTURE), _ev(2, evlib.ARRIVAL)]) == set()


def test_detects_unknown_event_kind(h: Harness):
    assert "event_kind" in _checked(h, [_ev(1, "teleported")])


def test_detects_events_out_of_order(h: Harness):
    assert "event_order" in _checked(h, [_ev(5, evlib.READY), _ev(4, evlib.READY)])


def test_detects_unknown_aircraft(h: Harness):
    assert "event_unknown_aircraft" in _checked(h, [_ev(1, evlib.READY, "sim-x99")])


def test_detects_event_spam(h: Harness):
    spam = [_ev(t, evlib.ROUTE_UNAVAILABLE) for t in range(10)]
    rules = _checked(h, spam)
    assert "event_spam" in rules
    assert sum(v.rule == "event_spam" for v in h.checker.violations) == 1  # reported once


def test_spam_window_slides(h: Harness):
    sparse = [_ev(t * 30.0, evlib.ROUTE_UNAVAILABLE) for t in range(20)]
    assert "event_spam" not in _checked(h, sparse)


def test_detects_unbalanced_flights(h: Harness):
    assert "flight_balance" in _checked(h, [_ev(1, evlib.ARRIVAL)])


def test_violation_cap(h: Harness):
    h.checker.max_violations = 3
    h.checker.check_events([_ev(1, "bogus")] * 10, h.world)
    assert len(h.checker.violations) == 3


def test_violation_str_is_readable(h: Harness):
    h.checker.check_events([_ev(1.5, "bogus")], h.world)
    assert str(h.checker.violations[0]) == "t=1.5 sim-p00 [event_kind] bogus"
