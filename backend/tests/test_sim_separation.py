"""E3.3 T1 separation: directional levels, following, conflict probe, departure
release, holding stack, spacing metrics and the separation oracle rules.

Synthetic networks keep every scenario small and exact; the Central Florida
matrix lives in tests/test_sim_stress.py (``sep_on``).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any

import networkx as nx
import numpy as np
import pytest

from aerolink.network import Edge, Network
from aerolink.sim import pads as padlib
from aerolink.sim.clock import SimClock
from aerolink.sim.controller import Controller, ControllerConfig, ShortestPathAdvisor
from aerolink.sim.demand import DemandConfig
from aerolink.sim.fleet import pad_position
from aerolink.sim.invariants import InvariantChecker
from aerolink.sim.physics import (
    FT_TO_M,
    PAD_ALT_M,
    STACK_ALT_OFFSET_M,
    approach_alt_m,
    build_legs,
    directional_alt_m,
    interpolate_on_edge,
    segment_length_m,
)
from aerolink.sim.separation import (
    ConflictProbe,
    following_cap_mps,
    following_caps,
    leader_ahead,
    stack_hold_distance_m,
)
from aerolink.sim.spacing import SpacingMonitor, horizontal_m
from aerolink.sim.state import AircraftState, Status, WindField, WorldState
from aerolink.sim.traffic import TrafficIndex
from aerolink.sim.world_config import SeparationConfig, WorldToggles

SEP = SeparationConfig()
FLOOR_M = 2500 * FT_TO_M
CEILING_M = 3000 * FT_TO_M


# ------------------------------------------------------------------ fixtures
def site(lat: float, lon: float, pads: int = 3) -> dict:
    return {"lat": lat, "lon": lon, "types": ["evtol"], "serves": ["passenger"], "pads": pads}


def wp(lat: float, lon: float) -> dict:
    return {"lat": lat, "lon": lon, "types": ["waypoint"], "serves": []}


def make_network(nodes: dict[str, dict], pairs: list[tuple[str, str]]) -> Network:
    graph = nx.Graph()
    node_props: dict[str, dict[str, Any]] = {}
    for nid, p in nodes.items():
        node_props[nid] = p
        graph.add_node(nid)
    edge_map = {}
    for i, (u, v) in enumerate(pairs):
        a = (float(nodes[u]["lon"]), float(nodes[u]["lat"]))
        b = (float(nodes[v]["lon"]), float(nodes[v]["lat"]))
        length = segment_length_m(a, b)
        e = Edge(
            edge_id=f"e{i}",
            kind="corridor",
            source=u,
            target=v,
            length_m=length,
            band_floor_ft=2500,
            band_ceiling_ft=3000,
            highway=None,
            geometry=(a, b),
        )
        edge_map[(u, v)] = e
        edge_map[(v, u)] = e
        graph.add_edge(u, v, length_m=length, edge_id=e.edge_id, band_floor_ft=2500, band_ceiling_ft=3000)
    return Network(graph, node_props, edge_map)


def line(km: float = 11.1, pads_b: int = 3) -> Network:
    """a (south) -- w -- b (north)."""
    dlat = km / 111.2
    nodes = {"a": site(28.0, -81.5), "w": wp(28.0 + dlat / 2, -81.5), "b": site(28.0 + dlat, -81.5, pads_b)}
    return make_network(nodes, [("a", "w"), ("w", "b")])


def crossing(half_km: float = 15.0) -> Network:
    """Two corridors crossing in mid-air without a shared node: a1->b1 north
    and a2->b2 east, both ``half_km`` from the crossing, so both fly the floor
    and reach the crossing at the same moment."""
    dlat = half_km / 111.2
    dlon = dlat / np.cos(np.radians(28.0 + dlat))
    nodes = {
        "a1": site(28.0, -81.5),
        "b1": site(28.0 + 2 * dlat, -81.5),
        "a2": site(28.0 + dlat, -81.5 - dlon),
        "b2": site(28.0 + dlat, -81.5 + dlon),
    }
    return make_network(nodes, [("a1", "b1"), ("a2", "b2")])


def controller(
    net: Network,
    homes: dict[str, str],
    separation: bool = True,
    stands: dict[str, int] | None = None,
    sep: SeparationConfig = SEP,
) -> Controller:
    pads = padlib.init_pads(net, stand_overrides=stands)
    fleet = {}
    for i, (aid, home) in enumerate(sorted(homes.items())):
        padlib.occupy(pads[home], aid)
        fleet[aid] = AircraftState(
            aircraft_id=aid,
            vehicle_type="passenger",
            home_node=home,
            status="parked",
            current_node=home,
            position=pad_position(net, home),
            rng=np.random.default_rng(i),
            telemetry_rng=np.random.default_rng(100 + i),
        )
    world = WorldState(t=0.0, aircraft=fleet, pads=pads, rng=np.random.default_rng(42), wind=WindField(0, 0, 0))
    clock = SimClock(datetime(2026, 11, 20, 12, tzinfo=UTC), 1.0)
    cfg = ControllerConfig(
        dt_s=1.0,
        demand=DemandConfig(0, 0, 0),
        cruise_speed_sigma=0.0,
        destination_change_probability=0.0,
        suggestion_acceptance_rate=0.0,
        turnaround_s=30,
        separation=sep,
        world=WorldToggles(separation=separation),
    )
    return Controller(net, world, clock, ShortestPathAdvisor(net), cfg)


class Run:
    """Steps a controller with the oracle (separation rules on) and an
    independent spacing monitor, recording the closest pair approach."""

    def __init__(self, ctl: Controller):
        self.ctl = ctl
        self.checker = InvariantChecker(ctl.network, 1.0, separation=SEP)
        self.checker.check_step(ctl.world)
        self.monitor = SpacingMonitor(ctl.network, SEP)
        self.monitor_events: list[dict] = []
        self.min_h_at_los_level = float("inf")  # min horizontal while < 150 m apart vertically
        self.pending: dict[str, tuple[str, float]] = {}

    def depart(self, aid: str, dest: str, speed: float = 60.0) -> None:
        """Ask for a departure; retried every step until released, the way
        ``Pilot.parked`` retries a due trip."""
        self.pending[aid] = (dest, speed)
        self._try_departures()

    def _try_departures(self) -> None:
        for aid, (dest, speed) in sorted(self.pending.items()):
            if depart(self.ctl, aid, dest, speed):
                del self.pending[aid]

    def step(self, n: int = 1) -> None:
        w = self.ctl.world
        for _ in range(n):
            self._try_departures()
            self.ctl.step()
            self.checker.check_step(w)
            self.monitor.step(w, lambda kind, aircraft_id=None, **d: self.monitor_events.append({"kind": kind, **d}))
            air = [ac for ac in w.aircraft.values() if ac.airborne]
            for i, a in enumerate(air):
                for b in air[i + 1 :]:
                    if abs(a.position.alt_msl_m - b.position.alt_msl_m) < SEP.min_vertical_separation_m:
                        h = horizontal_m(a.position.lat, a.position.lon, b.position.lat, b.position.lon)
                        self.min_h_at_los_level = min(self.min_h_at_los_level, h)

    def until_parked(self, limit: int = 2000) -> None:
        for _ in range(limit):
            self.step()
            if not self.pending and all(ac.status == "parked" for ac in self.ctl.world.aircraft.values()):
                return
        raise AssertionError("aircraft never all parked")

    def events(self, kind: str) -> list[dict]:
        return [e for e in self.ctl.world.events if e["kind"] == kind]

    def rules(self) -> list[str]:
        return [v.rule for v in self.checker.violations]


def depart(ctl: Controller, aid: str, dest: str, speed: float = 60.0) -> bool:
    origin = ctl.world.aircraft[aid].current_node
    assert origin is not None
    n = len(ctl.network.shortest_paths(origin, dest, "passenger", k=1)[0]) - 1
    return ctl.pilots[aid].depart(dest, planned_speeds=[speed] * n)


# ---------------------------------------------------------------- unit level
def test_directional_levels_follow_the_chord_course():
    net = line()
    e = net.edge_between("a", "w")
    assert directional_alt_m(e, "a") == pytest.approx(FLOOR_M)  # north-bound
    assert directional_alt_m(e, "w") == pytest.approx(CEILING_M)  # south-bound
    assert CEILING_M - FLOOR_M == pytest.approx(152.4)
    assert CEILING_M - FLOOR_M >= SEP.min_vertical_separation_m


def test_build_legs_directional_flag():
    net = line()
    path = ["a", "w", "b"]
    off = build_legs(path, net, 60.0)
    on = build_legs(path, net, 60.0, directional=True)
    back = build_legs(list(reversed(path)), net, 60.0, directional=True)
    assert [leg.cruise_alt_m for leg in off] == pytest.approx([(FLOOR_M + CEILING_M) / 2] * 2)
    assert [leg.cruise_alt_m for leg in on] == pytest.approx([FLOOR_M] * 2)
    assert [leg.cruise_alt_m for leg in back] == pytest.approx([CEILING_M] * 2)


def test_following_cap():
    v = 50.0
    assert following_cap_mps(SEP.follow_target_distance_m, v, SEP) == pytest.approx(v)
    assert following_cap_mps(2000.0, v, SEP) > v  # may close a large gap
    assert following_cap_mps(400.0, v, SEP) < v  # opens a small one
    assert following_cap_mps(SEP.min_separation_m, v, SEP) == pytest.approx(v - 300.0 / 30.0)
    assert following_cap_mps(0.0, 5.0, SEP) == 0.0  # never negative
    gaps = [300, 400, 500, 600, 800, 1200]
    caps = [following_cap_mps(g, v, SEP) for g in gaps]
    assert caps == sorted(caps)


def test_stack_hold_distances():
    pad = padlib.PadState(node_id="b", capacity=1)
    for aid in ("x", "y", "z"):
        padlib.request(pad, aid)  # x gets the slot, y and z queue
    assert pad.queue == ["y", "z"]
    assert stack_hold_distance_m(pad, "y", SEP) == pytest.approx(padlib.HOLD_DISTANCE_M)
    assert stack_hold_distance_m(pad, "z", SEP) == pytest.approx(padlib.HOLD_DISTANCE_M + SEP.stack_distance_m)


def test_holding_stack_waits_above_the_approach_path():
    net = line()
    ctl = controller(net, {"p0": "a"})
    depart(ctl, "p0", "b")
    ac = ctl.world.aircraft["p0"]
    leg = ac.legs[-1]
    glide = PAD_ALT_M + 1000.0 * 0.08
    assert approach_alt_m(ac, leg, 1000.0) == pytest.approx(glide)
    ac.stack_hold = True
    assert approach_alt_m(ac, leg, 1000.0) == pytest.approx(glide + STACK_ALT_OFFSET_M)
    ac.pad_cleared = True  # cleared: back onto the glide path
    assert approach_alt_m(ac, leg, 1000.0) == pytest.approx(glide)


def test_reroute_may_not_turn_back_onto_the_edge_just_flown():
    net = line()
    for sep_on in (True, False):
        ctl = controller(net, {"p0": "a"}, separation=sep_on)
        depart(ctl, "p0", "b")
        plan = ctl.world.aircraft["p0"].active_flight
        assert plan is not None
        plan.flown.append("w")
        assert ctl.pilots["p0"]._u_turn(["w", "a"]) is sep_on
        assert not ctl.pilots["p0"]._u_turn(["w", "b"])


# ------------------------------------------------------------- integration
def test_head_on_traffic_is_separated_by_level():
    net = line(km=30.0)
    r = Run(controller(net, {"p0": "a", "p1": "b"}))
    r.depart("p0", "b")
    r.depart("p1", "a")
    top = {"p0": 0.0, "p1": 0.0}
    for _ in range(600):
        r.step()
        for aid, ac in r.ctl.world.aircraft.items():
            top[aid] = max(top[aid], ac.position.alt_msl_m)
    assert top["p0"] == pytest.approx(FLOOR_M)  # north-bound: band floor
    assert top["p1"] == pytest.approx(CEILING_M)  # south-bound: band ceiling
    r.until_parked()
    assert r.rules() == []
    assert not r.events("loss_of_separation")
    assert len(r.events("arrival")) == 2


def test_head_on_without_separation_is_a_loss_of_separation():
    net = line()
    r = Run(controller(net, {"p0": "a", "p1": "b"}, separation=False))
    r.depart("p0", "b")
    r.depart("p1", "a")
    r.until_parked()
    assert r.rules().count("loss_of_separation") == 1  # once per encounter
    los = [e for e in r.monitor_events if e["kind"] == "loss_of_separation"]
    end = [e for e in r.monitor_events if e["kind"] == "loss_of_separation_end"]
    assert len(los) == 1 and len(end) == 1
    assert end[0]["duration_s"] > 0 and end[0]["min_horizontal_m"] < SEP.min_separation_m


@pytest.mark.parametrize("sep_on", [True, False])
def test_crossing_traffic(sep_on: bool):
    net = crossing()
    r = Run(controller(net, {"p0": "a1", "p1": "a2"}, separation=sep_on))
    r.depart("p0", "b1")
    r.depart("p1", "b2")
    min_v = {"p0": 99.0, "p1": 99.0}
    for _ in range(1500):
        r.step()
        for aid, ac in r.ctl.world.aircraft.items():
            if ac.status == "cruise":
                min_v[aid] = min(min_v[aid], ac.velocity_mps)
        if all(ac.status == "parked" for ac in r.ctl.world.aircraft.values()):
            break
    assert max(min_v.values()) < 99.0  # both reached cruise before the crossing
    assert len(r.events("arrival")) == 2
    if sep_on:
        assert r.rules() == []
        assert r.min_h_at_los_level >= SEP.min_separation_m
        assert min(min_v.values()) < 50.0  # somebody gave way
    else:
        assert "loss_of_separation" in r.rules()


def test_departure_release_spaces_out_climb_outs():
    net = line()
    r = Run(controller(net, {"p0": "a", "p1": "a"}))
    r.depart("p0", "b")
    r.depart("p1", "b")  # same moment, same pad area
    r.until_parked()
    waits = [e for e in r.events("departure_wait") if e["aircraft_id"] == "p1"]
    assert len(waits) == 1 and waits[0]["reason"] == "traffic"
    assert len([e for e in r.events("departure_released") if e["aircraft_id"] == "p1"]) == 1
    assert [e["aircraft_id"] for e in r.events("departure")] == ["p0", "p1"]
    assert r.rules() == []
    assert len(r.events("arrival")) == 2


def test_following_traffic_keeps_spacing_and_bunching_is_logged():
    net = line(km=20.0)
    r = Run(controller(net, {"p0": "a", "p1": "a"}))
    r.depart("p0", "b", speed=40.0)
    r.step(5)
    r.depart("p1", "b", speed=60.0)  # faster follower catches up
    cruise_gaps = []
    for _ in range(3000):
        r.step()
        a, b = r.ctl.world.aircraft["p0"], r.ctl.world.aircraft["p1"]
        if a.status == "cruise" and b.status == "cruise":
            cruise_gaps.append(horizontal_m(a.position.lat, a.position.lon, b.position.lat, b.position.lon))
        if not r.pending and all(ac.status == "parked" for ac in r.ctl.world.aircraft.values()):
            break
    assert len(r.events("arrival")) == 2
    assert r.rules() == []
    assert r.min_h_at_los_level >= SEP.min_separation_m
    # the follower closes up and then settles on the target gap
    assert min(cruise_gaps) == pytest.approx(SEP.follow_target_distance_m, abs=50.0)
    bunch = [e for e in r.monitor_events if e["kind"] == "bunching" and e["where"] == "edge"]
    assert bunch and all(e["spacing_m"] < SEP.spacing_same_edge_m for e in bunch)
    assert len(bunch) <= 3  # a few encounters, not one per step


def test_holding_stack_on_a_single_pad():
    net = line(pads_b=1)
    homes = {"p0": "a", "p1": "a", "p2": "a"}
    r = Run(controller(net, homes, stands={"a": 0, "b": 3}))
    for aid in sorted(homes):
        r.depart(aid, "b")  # departure release spaces them out
    both_holding_points: set[tuple[float, float]] = set()
    for _ in range(3000):
        r.step()
        held = sorted(
            ac.hold_remaining_m
            for ac in r.ctl.world.aircraft.values()
            if ac.airborne and ac.pad_requested and not ac.pad_cleared
        )
        if len(held) == 2:
            both_holding_points.add((held[0], held[1]))
        if all(ac.status == "parked" for ac in r.ctl.world.aircraft.values()):
            break
    assert len(r.events("arrival")) == 3
    assert r.rules() == []
    assert any(b - a == pytest.approx(SEP.stack_distance_m) for a, b in both_holding_points)
    starts = [e["aircraft_id"] for e in r.events("hold_start")]
    assert len(starts) == len(set(starts))  # moving up the stack is not a new hold


# ------------------------------------------------------------- oracle rules
def cruising_controller() -> tuple[Controller, AircraftState]:
    net = line()
    ctl = controller(net, {"p0": "a", "p1": "b"}, separation=False)
    depart(ctl, "p0", "b")
    depart(ctl, "p1", "a")
    for _ in range(120):
        ctl.step()
    return ctl, ctl.world.aircraft["p0"]


def test_oracle_flags_a_stuck_aircraft_once():
    ctl, ac = cruising_controller()
    checker = InvariantChecker(ctl.network, 1.0)
    for _ in range(700):
        ctl.world.t += 1.0
        ac.velocity_mps = 0.0
        ac.status = "cruise"
        checker.check_step(ctl.world)
    assert [v.rule for v in checker.violations].count("stuck") == 1


def test_oracle_loss_of_separation_needs_separation_config():
    ctl, a = cruising_controller()
    b = ctl.world.aircraft["p1"]
    b.position = a.position  # teleport on top of each other
    with_sep = InvariantChecker(ctl.network, 1.0, separation=SEP)
    without = InvariantChecker(ctl.network, 1.0)
    for chk in (with_sep, without):
        for _ in range(3):
            chk.check_step(ctl.world)
    assert [v.rule for v in with_sep.violations].count("loss_of_separation") == 1
    assert "loss_of_separation" not in {v.rule for v in without.violations}


# ------------------------------------------------------------- conflict probe
def place(
    ctl: Controller,
    aid: str,
    dest: str,
    along_m: float,
    v: float = 60.0,
    alt: float | None = None,
    status: Status = "cruise",
    directional: bool = True,
) -> AircraftState:
    """Depart ``aid`` and put it ``along_m`` metres down its route, flying."""
    assert depart(ctl, aid, dest)
    ac = ctl.world.aircraft[aid]
    if not directional:  # same level both ways, as on overlapping corridors
        ac.legs = [dataclasses.replace(leg, cruise_alt_m=FLOOR_M) for leg in ac.legs]
    i, d = 0, along_m
    while d > ac.legs[i].length_m:
        d -= ac.legs[i].length_m
        i += 1
    ac.leg_index, ac.distance_along_edge_m = i, d
    leg = ac.legs[i]
    lat, lon, course = interpolate_on_edge(leg.edge, d, leg.start_node)
    ac.position.lat, ac.position.lon = lat, lon
    ac.position.alt_msl_m = leg.cruise_alt_m if alt is None else alt
    ac.heading_deg, ac.velocity_mps, ac.status = course, v, status
    return ac


def probe_caps(ctl: Controller) -> ConflictProbe:
    """One separation pass, as in ``Controller.step``: following caps, then the probe."""
    following_caps(ctl.world, TrafficIndex(ctl.world.aircraft), SEP)
    probe = ConflictProbe(ctl.network, SEP)
    probe.caps(ctl.world)
    return probe


def test_leader_ahead_looks_across_legs():
    net = line(km=3.0)  # two legs of 1.5 km
    ctl = controller(net, {"p0": "a", "p1": "a"})
    follower = place(ctl, "p0", "b", 1200.0)
    place(ctl, "p1", "b", 1900.0, v=45.0)  # already on the second leg
    idx = TrafficIndex(ctl.world.aircraft)
    assert leader_ahead(follower, idx, 2000.0) == ("p1", pytest.approx(700.0), 45.0)
    assert leader_ahead(follower, idx, 500.0) is None


def test_probe_leaves_unconflicted_traffic_alone():
    net = line(km=30.0)
    ctl = controller(net, {"p0": "a", "p1": "b"})
    place(ctl, "p0", "b", 5000.0)
    place(ctl, "p1", "a", 5000.0)  # 20 km apart, opposite levels
    probe_caps(ctl)
    assert all(ac.velocity_cap_mps is None for ac in ctl.world.aircraft.values())


def test_probe_head_on_same_level_brakes_and_swaps_level():
    net = line(km=30.0)
    ctl = controller(net, {"p0": "a", "p1": "b"})
    a = place(ctl, "p0", "b", 14000.0, directional=False)
    b = place(ctl, "p1", "a", 14500.0, directional=False)  # 1.5 km nose to nose
    assert a.position.alt_msl_m == pytest.approx(b.position.alt_msl_m)
    probe_caps(ctl)
    assert a.velocity_cap_mps == 0.0 and b.velocity_cap_mps == 0.0  # neither can stop short of 400 m
    swapped = [ac for ac in (a, b) if ac.level_swap_with is not None]
    assert len(swapped) == 1  # exactly one leaves the shared level
    assert swapped[0].level_swap_with == ({"p0", "p1"} - {swapped[0].aircraft_id}).pop()


def test_level_swap_ends_when_the_partner_is_gone():
    net = line(km=30.0)
    ctl = controller(net, {"p0": "a", "p1": "b"})
    a = place(ctl, "p0", "b", 5000.0)
    a.level_swap_with = "p1"  # p1 still parked
    probe_caps(ctl)
    assert a.level_swap_with is None


def test_probe_inside_but_opening_needs_no_caps():
    net = line(km=30.0)
    ctl = controller(net, {"p0": "a", "p1": "b"})
    a = place(ctl, "p0", "b", 15100.0, directional=False)
    b = place(ctl, "p1", "a", 15100.0, directional=False)  # passed each other: 200 m apart, separating
    assert horizontal_m(a.position.lat, a.position.lon, b.position.lat, b.position.lon) < SEP.min_separation_m
    probe_caps(ctl)
    assert a.velocity_cap_mps is None and b.velocity_cap_mps is None


def test_probe_inside_and_closing_stops_closing_in():
    net = line(km=30.0)
    ctl = controller(net, {"p0": "a", "p1": "a"})
    a = place(ctl, "p0", "b", 10000.0, v=60.0)
    b = place(ctl, "p1", "b", 10250.0, v=30.0)  # same level, 250 m ahead, much slower
    probe_caps(ctl)
    assert a.velocity_cap_mps is not None and a.velocity_cap_mps < a.velocity_mps
    assert b.velocity_cap_mps is None  # the leader flies on: braking it would not help
    for ac in (a, b):
        ac.velocity_cap_mps = None
    a.velocity_mps, b.velocity_mps = 45.0, 40.0  # closing slowly: a can stop closing in
    probe_caps(ctl)
    assert a.velocity_cap_mps is not None and a.velocity_cap_mps < 40.0
    assert b.velocity_cap_mps is None


def test_probe_levels_off_below_a_holding_aircraft():
    net = line(km=30.0)
    ctl = controller(net, {"p0": "a", "p1": "a"})
    climber = place(ctl, "p0", "b", 1500.0, alt=FLOOR_M - 180.0, status="climbing")
    holder = place(ctl, "p1", "b", 3000.0, v=0.0, status="holding")
    probe_caps(ctl)
    assert climber.altitude_hold  # passes under at its present altitude
    assert climber.velocity_cap_mps is None or climber.velocity_cap_mps > 0.0
    assert holder.velocity_cap_mps is None


def test_cleared_arrival_never_gives_way_to_another_arrival():
    net = line(km=30.0)
    ctl = controller(net, {"p0": "a", "p1": "a"})
    first = place(ctl, "p0", "b", 28500.0, v=40.0, status="descending")
    second = place(ctl, "p1", "b", 28000.0, v=60.0, status="descending")
    for ac in (first, second):
        ac.position.alt_msl_m = first.position.alt_msl_m
    first.pad_requested = second.pad_requested = True
    first.pad_cleared = second.pad_cleared = True  # both cleared: the closer lands first
    probe_caps(ctl)
    assert first.velocity_cap_mps is None
    assert second.velocity_cap_mps is not None
    first.pad_cleared = False  # only the farther is cleared: it has priority
    first.velocity_cap_mps = second.velocity_cap_mps = None
    probe_caps(ctl)
    # nothing beyond its ordinary following cap (500 m behind a 40 m/s leader)
    assert second.velocity_cap_mps == pytest.approx(following_cap_mps(500.0, 40.0, SEP))


def test_pad_area_traffic_is_not_a_loss_of_separation():
    """Arrivals and departures at the same site inside ``pad_area_m`` are the
    pad's business (sequenced by pad slots), not airborne separation."""
    net = line(km=30.0)
    ctl = controller(net, {"p0": "a", "p1": "b", "p2": "a"})
    leaving = place(ctl, "p1", "a", 100.0, v=10.0, alt=50.0, status="climbing")  # from b
    arriving = place(ctl, "p0", "b", 30000.0 - 120.0, v=10.0, alt=60.0, status="descending")
    assert (
        horizontal_m(arriving.position.lat, arriving.position.lon, leaving.position.lat, leaving.position.lon)
        < SEP.min_separation_m
    )
    events: list[dict] = []
    monitor = SpacingMonitor(ctl.network, SEP)
    monitor.step(ctl.world, lambda kind, aircraft_id=None, **d: events.append({"kind": kind}))
    checker = InvariantChecker(ctl.network, 1.0, separation=SEP)
    checker.check_step(ctl.world)
    assert not events and "loss_of_separation" not in {v.rule for v in checker.violations}
    # the same geometry away from any site is a loss of separation
    far = place(ctl, "p2", "b", 15000.0, v=10.0, alt=60.0)
    leaving.legs, leaving.leg_index, leaving.distance_along_edge_m = far.legs, far.leg_index, far.distance_along_edge_m
    leaving.position = dataclasses.replace(far.position, alt_msl_m=50.0)
    ctl.world.aircraft.pop("p0")
    monitor.step(ctl.world, lambda kind, aircraft_id=None, **d: events.append({"kind": kind}))
    assert "loss_of_separation" in [e["kind"] for e in events]
