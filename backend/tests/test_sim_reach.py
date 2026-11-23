"""Range rules (hardening phase C, findings H2/H3/M3): every accepted plan fits the
battery, and medical aircraft never end up where they cannot get home from."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest
from conftest import make_network, site, wp
from test_sim_controller import make_controller, run_until

from aerolink.network import Network
from aerolink.sim import events as evlib
from aerolink.sim import pads as padlib
from aerolink.sim.controller import Controller, ShortestPathAdvisor
from aerolink.sim.demand import find_nearest_idle_medical_aircraft
from aerolink.sim.fleet import choose_destination
from aerolink.sim.invariants import InvariantChecker
from aerolink.sim.reach import (
    ROUTE_RETRY_S,
    can_get_home,
    can_serve_incident,
    fits,
    in_service_area,
    range_left_m,
)
from aerolink.vehicle import vehicle_cfg

KM = 1000.0
RANGE_M = vehicle_cfg("medical").range_m


def always_change(ctl: Controller) -> Controller:
    ctl.cfg = dataclasses.replace(ctl.cfg, destination_change_probability=1.0)
    return ctl


def kinds(ctl: Controller, kind: str) -> list[dict]:
    return [e for e in ctl.world.events if e["kind"] == kind]


def checked(ctl: Controller) -> InvariantChecker:
    chk = InvariantChecker(ctl.network, ctl.cfg.dt_s)
    chk.check_step(ctl.world)
    return chk


def step_checked(ctl: Controller, chk: InvariantChecker, n: int) -> None:
    for _ in range(n):
        ctl.step()
        chk.check_step(ctl.world)


@pytest.fixture
def far_network() -> Network:
    """h1 --155km-- w1 --10km-- s ;  w1 --70km-- x --120km-- h2 (all medical).

    From s, home h1 is 165 km away (beyond one battery) while incident x is 80 km.
    """
    nodes = {
        "h1": site(28.0, -81.5, ["medical"], kind="helipad"),
        "w1": wp(28.5, -81.5),
        "s": site(28.6, -81.5, ["medical"], kind="helipad"),
        "x": site(29.0, -81.5, ["medical"], kind="helipad"),
        "h2": site(30.0, -81.5, ["medical"], kind="helipad"),
    }
    edges = [("h1", "w1", 155 * KM), ("w1", "s", 10 * KM), ("w1", "x", 70 * KM), ("x", "h2", 120 * KM)]
    return make_network(nodes, edges)


# ------------------------------------------------------------------ helpers
def test_range_left_and_fits(line_network: Network):
    ctl = make_controller(line_network, {"p1": ("passenger", "a")})
    ac = ctl.world.aircraft["p1"]
    ac.battery_soc = 0.5
    assert range_left_m(ac) == pytest.approx(0.5 * RANGE_M)
    assert fits(ac, 0.5 * RANGE_M)
    assert not fits(ac, 0.5 * RANGE_M + 1)


def test_route_distance_cached_and_bounded(far_network: Network):
    assert far_network.route_distance_m("s", "x", "medical") == pytest.approx(80 * KM)
    assert far_network.route_distance_m("s", "h1", "medical") is None  # 165 km > range
    assert far_network.route_distance_m("x", "x", "medical") == 0.0
    assert ("s", "h1", "medical") in far_network._distance_cache


def test_medical_home_and_service_rules(far_network: Network):
    ctl = make_controller(far_network, {"m1": ("medical", "s"), "m2": ("medical", "h2")})
    m1, m2 = ctl.world.aircraft["m1"], ctl.world.aircraft["m2"]
    m1.home_node = "h1"
    assert not can_get_home(far_network, m1, "x")  # x -> h1 is 225 km
    assert not can_serve_incident(far_network, m1, "x")
    assert can_serve_incident(far_network, m2, "x")
    # m1 is closer (80 km vs 120 km) but could not get home afterwards.
    assert find_nearest_idle_medical_aircraft(ctl.world, "x", far_network) == "m2"
    assert in_service_area(far_network, ctl.world.aircraft, "x")
    del ctl.world.aircraft["m2"]
    assert not in_service_area(far_network, ctl.world.aircraft, "x")
    assert find_nearest_idle_medical_aircraft(ctl.world, "x", far_network) is None


def test_passengers_have_no_home_constraint(far_network: Network, line_network: Network):
    ctl = make_controller(line_network, {"p1": ("passenger", "a")})
    assert can_get_home(far_network, ctl.world.aircraft["p1"], "x")


# ------------------------------------------------------------------ dispatch
def test_dispatch_outside_service_area_dropped(far_network: Network):
    ctl = make_controller(far_network, {"m1": ("medical", "h1")})
    ctl.dispatch_medical("x")  # h1 -> x is 225 km
    assert ctl.pending_dispatches == []
    assert [e["node"] for e in kinds(ctl, evlib.DISPATCH_UNSERVICEABLE)] == ["x"]
    for _ in range(50):
        ctl.step()
    assert ctl.world.aircraft["m1"].status == "parked"


def test_dispatch_waits_for_an_aircraft_that_can_get_home(far_network: Network):
    ctl = make_controller(far_network, {"m1": ("medical", "s"), "m2": ("medical", "h2")})
    ctl.world.aircraft["m1"].home_node = "h1"
    ctl.world.aircraft["m1"].retry_at_s = 1e9  # keep it parked at s for the test
    ctl.dispatch_medical("x")
    ctl.step()
    assert ctl.world.aircraft["m1"].status == "parked"
    assert ctl.world.aircraft["m2"].status == "climbing"


@pytest.mark.parametrize("timeout", [None, 100.0])
def test_dispatch_timeout(line_network: Network, timeout: float | None):
    ctl = make_controller(line_network, {"m1": ("medical", "h")})
    ctl.cfg = dataclasses.replace(
        ctl.cfg, demand=dataclasses.replace(ctl.cfg.demand, medical_dispatch_timeout_s=timeout)
    )
    ctl.world.aircraft["m1"].status = "turnaround"  # busy: nobody can take the call
    ctl.world.aircraft["m1"].turnaround_until_s = 1e9
    ctl.dispatch_medical("h2")
    for _ in range(150):
        ctl.step()
    expired = kinds(ctl, evlib.DISPATCH_EXPIRED)
    if timeout is None:
        assert expired == [] and len(ctl.pending_dispatches) == 1
    else:
        assert [e["node"] for e in expired] == ["h2"] and ctl.pending_dispatches == []
        assert expired[0]["waited_s"] == pytest.approx(timeout + 1.0)


def test_dispatch_timeout_validated():
    from aerolink.sim.demand import DemandConfig

    with pytest.raises(ValueError, match="medical_dispatch_timeout_s"):
        DemandConfig(medical_dispatch_timeout_s=0)


# ------------------------------------------------------------------ departures
@pytest.mark.parametrize(("soc", "ok"), [(0.1, False), (0.2, True)])
def test_departure_needs_range(line_network: Network, soc: float, ok: bool):
    ctl = make_controller(line_network, {"p1": ("passenger", "a")})
    ac = ctl.world.aircraft["p1"]
    ac.battery_soc = soc  # a -> b is ~16.7 km; 0.1 leaves 16 km
    assert ctl.request_trip("p1", "b") is ok
    assert bool(kinds(ctl, evlib.ROUTE_UNAVAILABLE)) is not ok


def test_choose_destination_only_reachable():
    nodes = {
        "a": site(28.0, -81.5, ["passenger"]),
        "b": site(28.1, -81.5, ["passenger"]),
        "z": site(29.0, -81.5, ["passenger"]),  # isolated
    }
    net = make_network(nodes, [("a", "b", 11 * KM)])
    net.graph.add_node("z")
    ctl = make_controller(net, {"p1": ("passenger", "a")})
    ac = ctl.world.aircraft["p1"]
    ac.home_node = "z"  # unreachable home: the return-home draw falls through
    rng = np.random.default_rng(0)
    assert {choose_destination(net, ac, rng, return_home_probability=1.0) for _ in range(50)} == {"b"}
    ac.current_node = "z"
    assert choose_destination(net, ac, rng) is None


def test_stranded_medical_return_logged_once_with_backoff(far_network: Network):
    ctl = make_controller(far_network, {"m1": ("medical", "s")})
    m1 = ctl.world.aircraft["m1"]
    m1.home_node = "h1"  # 165 km away: no route
    for _ in range(int(3 * ROUTE_RETRY_S)):
        ctl.step()
    assert len(kinds(ctl, evlib.ROUTE_UNAVAILABLE)) == 1
    assert m1.status == "parked" and m1.retry_at_s is not None
    assert m1.retry_at_s > ctl.world.t


# ------------------------------------------------------------------ in flight
def _hold_at_b(line_network: Network) -> tuple[Controller, InvariantChecker]:
    ctl = make_controller(line_network, {"p1": ("passenger", "a")}, max_hold_s=10)
    padlib.occupy(ctl.world.pads["b"], "ghost")  # b's only pad is taken
    chk = checked(ctl)
    assert ctl.request_trip("p1", "b")
    run_until(ctl, lambda c: c.world.aircraft["p1"].status == "holding", check=lambda c: chk.check_step(c.world))
    return ctl, chk


def test_divert_out_of_range_keeps_holding_and_logs_once(line_network: Network):
    ctl, chk = _hold_at_b(line_network)
    ac = ctl.world.aircraft["p1"]
    # 600 m of battery: enough for the 500 m to b, not for the ~10 km to c.
    ac.battery_soc = (ac.remaining_route_m() + 100) / RANGE_M
    step_checked(ctl, chk, int(2 * ROUTE_RETRY_S))
    assert ac.status == "holding"
    assert len(kinds(ctl, evlib.DIVERSION_UNAVAILABLE)) == 1
    assert kinds(ctl, evlib.DIVERSION) == []
    padlib.release(ctl.world.pads["b"], "ghost")
    run_until(ctl, lambda c: c.world.aircraft["p1"].status == "parked", check=lambda c: chk.check_step(c.world))
    assert ac.current_node == "b" and ac.retry_at_s is None
    assert chk.ok, chk.violations


def test_divert_in_range_goes_to_c(line_network: Network):
    ctl, chk = _hold_at_b(line_network)
    run_until(ctl, lambda c: c.world.aircraft["p1"].status == "parked", check=lambda c: chk.check_step(c.world))
    assert ctl.world.aircraft["p1"].current_node == "c"
    assert chk.ok, chk.violations


def test_divert_from_first_leg_returns_to_origin():
    """Holding on a leg that starts at a site: turning back reaches that site, and
    sites are never transited, so the only diversion is to land there."""
    nodes = {
        "a": site(28.00, -81.50, ["passenger"]),
        "b": site(28.03, -81.50, ["passenger"]),
        "c": site(28.00, -81.45, ["passenger"], pads=2),
    }
    from aerolink.sim.physics import segment_length_m

    def d(u, v):
        return segment_length_m((nodes[u]["lon"], nodes[u]["lat"]), (nodes[v]["lon"], nodes[v]["lat"]))

    net = make_network(nodes, [("a", "b", d("a", "b")), ("a", "c", d("a", "c"))])
    ctl = make_controller(net, {"p1": ("passenger", "a")}, max_hold_s=10)
    padlib.occupy(ctl.world.pads["b"], "ghost")
    chk = checked(ctl)
    assert ctl.request_trip("p1", "b")
    run_until(ctl, lambda c: bool(kinds(c, evlib.ARRIVAL)), check=lambda c: chk.check_step(c.world))
    (div,) = kinds(ctl, evlib.DIVERSION)
    assert div["to_node"] == "a"
    assert ctl.world.aircraft["p1"].current_node == "a"
    assert chk.ok, chk.violations


def test_destination_change_happens_with_range(line_network: Network):
    ctl = always_change(make_controller(line_network, {"p1": ("passenger", "a")}))
    chk = checked(ctl)
    assert ctl.request_trip("p1", "c")
    run_until(ctl, lambda c: bool(kinds(c, evlib.ARRIVAL)), check=lambda c: chk.check_step(c.world))
    (change,) = kinds(ctl, evlib.DESTINATION_CHANGE)
    assert change["to_node"] == "b" and ctl.world.aircraft["p1"].current_node == "b"
    assert chk.ok, chk.violations


def test_destination_change_only_within_range(line_network: Network):
    ctl = always_change(make_controller(line_network, {"p1": ("passenger", "a")}))
    ac = ctl.world.aircraft["p1"]
    chk = checked(ctl)
    assert ctl.request_trip("p1", "c")
    plan = ac.active_flight
    assert plan is not None and plan.destination_change_at is not None
    # Exactly enough battery for a -> c; the only alternative, b, is farther.
    ac.battery_soc = (ac.remaining_route_m() + 1) / RANGE_M
    run_until(ctl, lambda c: bool(kinds(c, evlib.ARRIVAL)), check=lambda c: chk.check_step(c.world))
    assert kinds(ctl, evlib.DESTINATION_CHANGE) == []
    assert ac.current_node == "c"
    assert chk.ok, chk.violations


# ------------------------------------------------------------------ remaining branches
def test_due_trip_rejected_when_nothing_fits(line_network: Network):
    ctl = make_controller(line_network, {"p1": ("passenger", "a")})
    ac = ctl.world.aircraft["p1"]
    ac.battery_soc = 0.01  # 1.6 km: no site is that close
    ac.next_trip_s = 0.0
    ctl.step()
    (rej,) = kinds(ctl, evlib.TRIP_REJECTED)
    assert rej["destination"] in {"b", "c"} and ac.status == "parked"
    ctl.step()
    assert len(kinds(ctl, evlib.TRIP_REJECTED)) == 1  # the trip is consumed, not retried every step


def test_due_trip_rejected_without_destination():
    net = make_network({"a": site(28.0, -81.5, ["passenger"])}, [])
    ctl = make_controller(net, {"p1": ("passenger", "a")})
    ctl.world.aircraft["p1"].next_trip_s = 0.0
    ctl.step()
    assert [e["destination"] for e in kinds(ctl, evlib.TRIP_REJECTED)] == [None]


class SitesOnlyAdvisor(ShortestPathAdvisor):
    """Offers routes only when asked from a landing site, so en-route
    destination changes get no route."""

    def request_routes(self, origin, destination, vehicle_type, t_s):
        if "waypoint" in self.network.nodes[origin].get("types", []):
            return []
        return super().request_routes(origin, destination, vehicle_type, t_s)


def test_destination_change_rejected_without_route(line_network: Network):
    ctl = always_change(
        make_controller(line_network, {"p1": ("passenger", "a")}, advisor=SitesOnlyAdvisor(line_network))
    )
    chk = checked(ctl)
    assert ctl.request_trip("p1", "c")
    run_until(ctl, lambda c: bool(kinds(c, evlib.ARRIVAL)), check=lambda c: chk.check_step(c.world))
    (rej,) = kinds(ctl, evlib.DESTINATION_CHANGE_REJECTED)
    assert rej["from_node"] == "c" and rej["to_node"] == "b"
    assert kinds(ctl, evlib.DESTINATION_CHANGE) == []
    assert ctl.world.aircraft["p1"].current_node == "c"
    assert chk.ok, chk.violations


def test_advisor_suggest_edge_cases(line_network: Network, monkeypatch: pytest.MonkeyPatch):
    ctl = make_controller(line_network, {"p1": ("passenger", "a")})
    adv = ShortestPathAdvisor(line_network)
    ac = ctl.world.aircraft["p1"]
    assert adv.suggest(ac, "a", 0.0) is None  # no active flight
    assert ctl.request_trip("p1", "b")
    assert adv.suggest(ac, "a", 0.0) is None  # already on the best route
    monkeypatch.setattr(line_network, "shortest_paths", lambda *a, **k: [])
    assert adv.suggest(ac, "a", 0.0) is None  # no route at all


def test_incident_needs_a_located_aircraft(far_network: Network):
    ctl = make_controller(far_network, {"m1": ("medical", "x")})
    m1 = ctl.world.aircraft["m1"]
    m1.current_node = None
    assert not can_serve_incident(far_network, m1, "x")


class LongestFirstAdvisor(ShortestPathAdvisor):
    """Real network routes, but the longest option first, so the real
    ``ShortestPathAdvisor.suggest`` has something better to offer en route."""

    def request_routes(self, origin, destination, vehicle_type, t_s):
        return list(reversed(super().request_routes(origin, destination, vehicle_type, t_s)))


def test_real_network_suggestion_fires_and_is_flown(fl_network: Network):
    """Finding L5: Tampa -> Lakeland is offered the Brandon/Bartow route (101.7 km); at
    the first waypoint the real advisor suggests I-4 (66.7 km) and the pilot takes it."""
    ctl = make_controller(fl_network, {"p1": ("passenger", "ev-water-st")}, advisor=LongestFirstAdvisor(fl_network))
    chk = checked(ctl)
    assert ctl.request_trip("p1", "ev-lal")
    plan = ctl.world.aircraft["p1"].active_flight
    assert plan is not None and "wp-brandon" in plan.path
    run_until(ctl, lambda c: bool(kinds(c, evlib.ARRIVAL)), check=lambda c: chk.check_step(c.world))
    (sug,) = kinds(ctl, evlib.SUGGESTION)
    assert sug["node"] == "wp-downtown-tampa" and sug["accepted"] is True
    assert sug["path"] == ["wp-downtown-tampa", "wp-i4-i75", "wp-plant-city", "wp-lakeland", "ev-lal"]
    (arr,) = kinds(ctl, evlib.ARRIVAL)
    assert arr["accepted_suggestion"] is True and arr["node"] == "ev-lal"
    assert chk.ok, chk.violations
