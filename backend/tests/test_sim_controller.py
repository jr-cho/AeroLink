"""Behavioural tests for the controller on small synthetic networks."""

from datetime import UTC, datetime

import numpy as np
import pytest
from conftest import make_network, site, wp

from aerolink.network import Network
from aerolink.sim import pads as padlib
from aerolink.sim.clock import SimClock
from aerolink.sim.controller import Controller, ControllerConfig, ShortestPathAdvisor
from aerolink.sim.demand import DemandConfig
from aerolink.sim.fleet import pad_position
from aerolink.sim.state import AircraftState, WindField, WorldState

NO_DEMAND = DemandConfig(peak_rate_per_hour=0.0, off_peak_rate_per_hour=0.0, medical_dispatch_mean_interval_s=0)


def make_controller(
    net: Network,
    placements: dict[str, tuple[str, str]],
    advisor=None,
    pad_capacity: dict[str, int] | None = None,
    **cfg,
) -> Controller:
    pads = padlib.init_pads(net, pad_capacity)
    fleet = {}
    for i, (aid, (vtype, node)) in enumerate(sorted(placements.items())):
        padlib.occupy(pads[node], aid)
        fleet[aid] = AircraftState(
            aircraft_id=aid,
            vehicle_type=vtype,  # ty: ignore[invalid-argument-type]
            home_node=node,
            status="parked",
            current_node=node,
            position=pad_position(net, node),
            rng=np.random.default_rng(i),
            telemetry_rng=np.random.default_rng(100 + i),
        )
    world = WorldState(t=0.0, aircraft=fleet, pads=pads, rng=np.random.default_rng(7), wind=WindField(0, 0, 0))
    clock = SimClock(datetime(2026, 11, 20, 12, tzinfo=UTC), cfg.pop("dt_s", 1.0))
    config = ControllerConfig(
        dt_s=clock.dt_s,
        demand=cfg.pop("demand", NO_DEMAND),
        cruise_speed_sigma=0.0,
        destination_change_probability=0.0,
        **cfg,
    )
    return Controller(net, world, clock, advisor or ShortestPathAdvisor(net), config)


def run_until(ctl: Controller, pred, max_steps=20_000, check=None):
    for _ in range(max_steps):
        ctl.step()
        if check:
            check(ctl)
        if pred(ctl):
            return
    raise AssertionError("condition never met")


def kinds(ctl, aid=None):
    return [e["kind"] for e in ctl.world.events if aid is None or e.get("aircraft_id") == aid]


def pad_invariant(ctl: Controller):
    for p in ctl.world.pads.values():
        assert len(p.occupants) <= p.capacity, p
        assert len(set(p.occupants)) == len(p.occupants)
        assert not set(p.occupants) & set(p.queue)
    for ac in ctl.world.aircraft.values():
        held = [n for n, p in ctl.world.pads.items() if ac.aircraft_id in p.occupants]
        if ac.status in ("parked", "turnaround", "landing"):
            assert len(held) == 1
        assert len(held) <= 1


# ------------------------------------------------------------------ basics
def test_simple_trip_lifecycle(line_network: Network):
    ctl = make_controller(line_network, {"p1": ("passenger", "a")}, turnaround_s=60)
    assert ctl.request_trip("p1", "b")
    ac = ctl.world.aircraft["p1"]
    assert "p1" not in ctl.world.pads["a"].occupants
    assert ac.status == "climbing" and ac.current_node is None
    run_until(ctl, lambda c: ac.status == "parked", check=pad_invariant)
    assert ac.current_node == "b" and ac.velocity_mps == 0
    assert ctl.world.pads["b"].occupants == ["p1"]
    assert kinds(ctl, "p1") == ["departure", "pad_requested", "pad_cleared", "landing", "arrival", "ready"]
    arrival = next(e for e in ctl.world.events if e["kind"] == "arrival")
    landing = next(e for e in ctl.world.events if e["kind"] == "landing")
    ready = next(e for e in ctl.world.events if e["kind"] == "ready")
    assert arrival["t"] - landing["t"] == pytest.approx(padlib.LANDING_DURATION_S)
    assert ready["t"] - arrival["t"] == pytest.approx(60)
    assert ac.battery_soc == 1.0
    plan = ctl.world.planned[0]
    assert plan.path == ["a", "w1", "w2", "b"] and plan.flown == ["a", "w1", "w2"]
    assert set(plan.eta_by_node) == {"w1", "w2", "b"}


def test_request_trip_rejects_invalid(line_network: Network):
    ctl = make_controller(line_network, {"p1": ("passenger", "a")})
    assert not ctl.request_trip("p1", "a")  # already there
    assert not ctl.request_trip("p1", "h")  # helipad: not served for passenger
    assert "route_unavailable" in kinds(ctl)
    assert ctl.world.aircraft["p1"].status == "parked"
    assert ctl.world.pads["a"].occupants == ["p1"]
    ctl.request_trip("p1", "b")
    assert not ctl.request_trip("p1", "c")  # already airborne


def test_flight_ids_are_seeded_and_unique(line_network: Network):
    def ids():
        ctl = make_controller(line_network, {"p1": ("passenger", "a"), "p2": ("passenger", "c")})
        ctl.request_trip("p1", "b")
        ctl.request_trip("p2", "a")
        return [str(p.flight_id) for p in ctl.world.planned]

    a, b = ids(), ids()
    assert a == b and len(set(a)) == 2


# ------------------------------------------------------------- contention
def test_holds_when_pad_full_then_lands_after_it_frees(line_network: Network):
    ctl = make_controller(
        line_network, {"p1": ("passenger", "a"), "p2": ("passenger", "b")}, turnaround_s=30, max_hold_s=10_000
    )
    ctl.request_trip("p1", "b")
    p1 = ctl.world.aircraft["p1"]
    run_until(ctl, lambda c: p1.status == "holding", check=pad_invariant)
    assert p1.remaining_route_m() == pytest.approx(padlib.HOLD_DISTANCE_M, abs=1e-6)
    assert ctl.world.pads["b"].queue == ["p1"]
    for _ in range(30):
        ctl.step()
        assert p1.status == "holding" and p1.velocity_mps == 0
    ctl.request_trip("p2", "c")
    run_until(ctl, lambda c: p1.status == "parked", check=pad_invariant)
    assert p1.current_node == "b"
    ev = [e for e in ctl.world.events if e.get("aircraft_id") == "p1"]
    assert [e["kind"] for e in ev] == [
        "departure",
        "pad_requested",
        "hold_start",
        "pad_cleared",
        "hold_end",
        "landing",
        "arrival",
        "ready",
    ]
    assert next(e for e in ev if e["kind"] == "hold_end")["held_s"] >= 30


def test_queue_is_first_come_first_served(line_network: Network):
    """Two aircraft wait for the single slot at b; the earlier requester lands first."""
    ctl = make_controller(
        line_network,
        {"p1": ("passenger", "a"), "p2": ("passenger", "c"), "p3": ("passenger", "b")},
        max_hold_s=10_000,
        pad_capacity={"c": 2},
    )
    ctl.request_trip("p1", "b")  # long way: a -> b
    ctl.request_trip("p2", "b")  # short way: c -> w2 -> b; requests pad first
    run_until(ctl, lambda c: c.world.aircraft["p1"].status == "holding" and c.world.aircraft["p2"].status == "holding")
    assert ctl.world.pads["b"].queue == ["p2", "p1"]
    ctl.request_trip("p3", "a")
    run_until(ctl, lambda c: any(e["kind"] == "arrival" for e in c.world.events), check=pad_invariant)
    assert next(e for e in ctl.world.events if e["kind"] == "arrival")["aircraft_id"] == "p2"
    assert ctl.world.aircraft["p1"].status == "holding"


def test_diversion_after_max_hold(line_network: Network):
    ctl = make_controller(line_network, {"p1": ("passenger", "a"), "p2": ("passenger", "b")}, max_hold_s=60)
    ctl.request_trip("p1", "b")
    p1 = ctl.world.aircraft["p1"]
    run_until(ctl, lambda c: p1.status == "parked", check=pad_invariant)
    assert p1.current_node == "c"
    div = next(e for e in ctl.world.events if e["kind"] == "diversion")
    hold = next(e for e in ctl.world.events if e["kind"] == "hold_start")
    assert div["from_node"] == "b" and div["to_node"] == "c"
    assert div["t"] - hold["t"] == pytest.approx(60)
    assert "p1" not in ctl.world.pads["b"].queue
    arrival = next(e for e in ctl.world.events if e["kind"] == "arrival")
    assert arrival["diverted"] is True and arrival["node"] == "c"
    assert ctl.world.planned[0].path[-1] == "c"


def test_diversion_unavailable_keeps_holding(line_network: Network):
    ctl = make_controller(
        line_network,
        {"p1": ("passenger", "a"), "p2": ("passenger", "b"), "p3": ("passenger", "c"), "p4": ("passenger", "c")},
        max_hold_s=30,
    )
    ctl.request_trip("p1", "b")
    padlib.occupy(ctl.world.pads["a"], "ground-vehicle")  # origin pad taken behind it
    p1 = ctl.world.aircraft["p1"]
    run_until(ctl, lambda c: "diversion_unavailable" in kinds(c))
    assert p1.status == "holding"
    assert p1.active_flight is not None
    assert p1.active_flight.destination_node == "b" and "p1" in ctl.world.pads["b"].queue
    # A slot at the origin frees: the next attempt diverts back there.
    padlib.release(ctl.world.pads["a"], "ground-vehicle")
    run_until(ctl, lambda c: "diversion" in kinds(c), check=pad_invariant)
    run_until(ctl, lambda c: p1.status == "parked", check=pad_invariant)
    assert p1.current_node == "a"


def test_slow_aircraft_never_overflows_pads_under_stress(line_network: Network):
    """Everybody flies to the one-slot pad at b; capacity is never exceeded."""
    placements = {"p1": ("passenger", "a"), "p2": ("passenger", "c"), "p3": ("passenger", "c")}
    ctl = make_controller(line_network, placements, max_hold_s=10_000, turnaround_s=20)
    for aid in ("p1", "p2", "p3"):
        ctl.request_trip(aid, "b")
    cleared: list[str] = []

    def chain(c):
        pad_invariant(c)
        for aid in ("p1", "p2", "p3"):
            ac = c.world.aircraft[aid]
            if ac.status == "parked" and ac.current_node == "b" and aid not in cleared:
                cleared.append(aid)
                c.request_trip(aid, "a")

    run_until(ctl, lambda c: len(cleared) == 3, check=chain)


# ---------------------------------------------------------------- medical
def test_medical_dispatch_full_cycle():
    nodes = {
        "h1": site(28.00, -81.50, ["medical"], kind="helipad"),
        "w": wp(28.05, -81.50),
        "h2": site(28.10, -81.50, ["medical"], kind="helipad"),
        "h3": site(28.30, -81.50, ["medical"], kind="helipad"),
        "w3": wp(28.25, -81.50),
    }
    from aerolink.sim.physics import segment_length_m

    def L(u, v):
        return segment_length_m((nodes[u]["lon"], nodes[u]["lat"]), (nodes[v]["lon"], nodes[v]["lat"]))

    net = make_network(nodes, [(u, v, L(u, v)) for u, v in [("h1", "w"), ("w", "h2"), ("w", "w3"), ("w3", "h3")]])
    ctl = make_controller(net, {"m1": ("medical", "h1"), "m3": ("medical", "h3")}, turnaround_s=30)
    ctl.dispatch_medical("h2")
    ctl.step()
    m1 = ctl.world.aircraft["m1"]
    assert m1.active_flight is not None and m1.active_flight.destination_node == "h2"  # nearest
    assert ctl.world.aircraft["m3"].status == "parked"
    run_until(ctl, lambda c: m1.status == "parked" and m1.current_node == "h1", check=pad_invariant)
    arrivals = [e for e in ctl.world.events if e["kind"] == "arrival"]
    assert [a["purpose"] for a in arrivals] == ["medical_dispatch", "medical_return"]
    assert arrivals[0]["response_time_s"] > 0
    assert arrivals[0]["response_time_s"] == pytest.approx(arrivals[0]["flight_time_s"] + 1, abs=1.5)


def test_pending_dispatch_waits_for_idle_aircraft():
    nodes = {
        "h1": site(28.00, -81.50, ["medical"], kind="helipad"),
        "w": wp(28.05, -81.50),
        "h2": site(28.10, -81.50, ["medical"], kind="helipad"),
    }
    net = make_network(nodes, [("h1", "w", 5560), ("w", "h2", 5560)])
    ctl = make_controller(net, {"m1": ("medical", "h1")}, turnaround_s=30)
    m1 = ctl.world.aircraft["m1"]
    m1.status = "turnaround"
    m1.turnaround_until_s = 50
    ctl.dispatch_medical("h2")
    for _ in range(40):
        ctl.step()
        assert m1.active_flight is None
    assert len(ctl.pending_dispatches) == 1
    run_until(ctl, lambda c: m1.active_flight is not None)
    # Turnaround ends during step t=50; the dispatch is served on the next step.
    assert ctl.pending_dispatches == [] and ctl.world.t == pytest.approx(51)


def test_passenger_never_dispatched_to_medical(line_network: Network):
    ctl = make_controller(line_network, {"p1": ("passenger", "a")})
    ctl.dispatch_medical("h")
    for _ in range(10):
        ctl.step()
    assert ctl.world.aircraft["p1"].status == "parked"
    # No medical aircraft at all: the incident is outside the service area.
    assert ctl.pending_dispatches == []
    assert [e["kind"] for e in ctl.world.events] == ["medical_incident", "dispatch_unserviceable"]


# ------------------------------------------------------------ pilot agent
def test_destination_change(line_network: Network):
    ctl = make_controller(line_network, {"p1": ("passenger", "a")})
    ctl.cfg = ControllerConfig(demand=NO_DEMAND, destination_change_probability=1.0, cruise_speed_sigma=0.0)
    ctl.request_trip("p1", "b")
    plan = ctl.world.aircraft["p1"].active_flight
    assert plan is not None
    assert plan.destination_change_at in (1, 2)
    run_until(ctl, lambda c: c.world.aircraft["p1"].status == "parked", check=pad_invariant)
    ev = next(e for e in ctl.world.events if e["kind"] == "destination_change")
    assert ev["from_node"] == "b" and ev["to_node"] == "c"
    assert ctl.world.aircraft["p1"].current_node == "c"
    assert plan.destination_changed and plan.path[0] == "a" and plan.path[-1] == "c"
    assert next(e for e in ctl.world.events if e["kind"] == "arrival")["destination_changed"] is True


def test_destination_change_after_pad_request_releases_slot():
    """Change fires at the last waypoint, after the pad at the old destination was granted."""
    nodes = {
        "a": site(28.00, -81.50, ["passenger"]),
        "w": wp(28.05, -81.50),
        "b": site(28.06, -81.50, ["passenger"]),
        "c": site(28.05, -81.45, ["passenger"]),
    }
    net = make_network(nodes, [("a", "w", 5560), ("w", "b", 1112), ("w", "c", 4900)])
    ctl = make_controller(net, {"p1": ("passenger", "a")})
    ctl.cfg = ControllerConfig(demand=NO_DEMAND, destination_change_probability=1.0, cruise_speed_sigma=0.0)
    ctl.request_trip("p1", "b")
    run_until(ctl, lambda c: "p1" in c.world.pads["b"].occupants)
    run_until(ctl, lambda c: "destination_change" in kinds(c))
    assert "p1" not in ctl.world.pads["b"].occupants
    run_until(ctl, lambda c: c.world.aircraft["p1"].status == "parked", check=pad_invariant)
    assert ctl.world.aircraft["p1"].current_node == "c"


class LongFirstAdvisor(ShortestPathAdvisor):
    """Offers the long route first so a suggestion to the short one is useful."""

    def request_routes(self, origin, destination, vehicle_type, t_s):
        return list(reversed(super().request_routes(origin, destination, vehicle_type, t_s)))


def diamond() -> Network:
    nodes = {
        "a": site(28.00, -81.50, ["passenger"]),
        "w0": wp(28.02, -81.50),
        "wn": wp(28.06, -81.48),  # short branch
        "wl": wp(28.06, -81.60),  # long branch
        "w1": wp(28.10, -81.50),
        "b": site(28.12, -81.50, ["passenger"]),
    }
    edges = [
        ("a", "w0", 2224),
        ("w0", "wn", 4700),
        ("wn", "w1", 4700),
        ("w0", "wl", 11000),
        ("wl", "w1", 11000),
        ("w1", "b", 2224),
    ]
    return make_network(nodes, edges)


@pytest.mark.parametrize("rate,expect_short", [(1.0, True), (0.0, False)])
def test_reroute_suggestion_acceptance(rate, expect_short):
    net = diamond()
    ctl = make_controller(
        net, {"p1": ("passenger", "a")}, advisor=LongFirstAdvisor(net), suggestion_acceptance_rate=rate
    )
    ctl.request_trip("p1", "b")
    plan = ctl.world.aircraft["p1"].active_flight
    assert plan is not None
    assert plan.path == ["a", "w0", "wl", "w1", "b"]
    run_until(ctl, lambda c: c.world.aircraft["p1"].status == "parked")
    sug = [e for e in ctl.world.events if e["kind"] == "suggestion"]
    assert sug and sug[0]["node"] == "w0" and sug[0]["accepted"] is expect_short
    if expect_short:
        assert plan.path == ["a", "w0", "wn", "w1", "b"] and plan.accepted_suggestion and plan.revision == 1
        assert plan.flown == ["a", "w0", "wn", "w1"]
    else:
        assert plan.path == ["a", "w0", "wl", "w1", "b"] and not plan.accepted_suggestion


def test_no_suggestion_when_on_best_route():
    net = diamond()
    ctl = make_controller(net, {"p1": ("passenger", "a")})
    ctl.request_trip("p1", "b")
    run_until(ctl, lambda c: c.world.aircraft["p1"].status == "parked")
    assert "suggestion" not in kinds(ctl)


# ----------------------------------------------------------------- demand
def test_passenger_demand_drives_trips(line_network: Network):
    busy = DemandConfig(peak_rate_per_hour=20.0, off_peak_rate_per_hour=20.0, medical_dispatch_mean_interval_s=0)
    ctl = make_controller(
        line_network, {"p1": ("passenger", "a"), "p2": ("passenger", "c")}, demand=busy, turnaround_s=60
    )
    for _ in range(3600):
        ctl.step()
        pad_invariant(ctl)
    assert kinds(ctl).count("departure") >= 4
    for e in ctl.world.events:
        if e["kind"] == "departure":
            assert e["origin"] != e["destination"]


def test_random_medical_dispatches(line_network: Network):
    demand = DemandConfig(peak_rate_per_hour=0, off_peak_rate_per_hour=0, medical_dispatch_mean_interval_s=300)
    ctl = make_controller(line_network, {"m1": ("medical", "h")}, demand=demand, turnaround_s=60)
    for _ in range(3600):
        ctl.step()
        pad_invariant(ctl)
    incidents = [e for e in ctl.world.events if e["kind"] == "medical_incident"]
    assert len(incidents) >= 5 and {e["node"] for e in incidents} <= {"h", "h2"}
    assert any(e["kind"] == "arrival" and e["purpose"] == "medical_dispatch" for e in ctl.world.events)


def test_events_are_time_ordered(line_network: Network):
    busy = DemandConfig(peak_rate_per_hour=20.0, off_peak_rate_per_hour=20.0, medical_dispatch_mean_interval_s=200)
    ctl = make_controller(
        line_network, {"p1": ("passenger", "a"), "p2": ("passenger", "c"), "m1": ("medical", "h")}, demand=busy
    )
    for _ in range(2000):
        ctl.step()
    ts = [e["t"] for e in ctl.world.events]
    assert ts == sorted(ts)
