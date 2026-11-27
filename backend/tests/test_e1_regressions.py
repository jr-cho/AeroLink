"""E1 regression tests: one focused test per simulator/route-engine fix made at the M5/M6 hand-off
(.work/PLAN.md bugs 1-8) and per fix made while building the E1 metrics and congestion scenarios."""

from __future__ import annotations

from uuid import uuid4

import pytest
from conftest import make_network, site, wp
from test_sim_controller import make_controller
from test_sim_reach import far_network, kinds  # noqa: F401  (far_network is a fixture)

from aerolink.forecast.cv import CVForecaster
from aerolink.navigation.b1 import B1Advisor, B1RouteAdvisorWrapper
from aerolink.network import Network
from aerolink.routing.a1 import A1Advisor, A1RouteAdvisorWrapper
from aerolink.routing.aerolink import ABLATIONS, AeroLinkAdvisor, AeroLinkRouteAdvisorWrapper
from aerolink.sim import events as evlib
from aerolink.sim import run_simulation
from aerolink.sim.controller import ShortestPathAdvisor
from aerolink.sim.energy import trip_energy_estimate
from aerolink.sim.fleet import choose_destination
from aerolink.sim.physics import segment_length_m
from aerolink.sim.reach import path_fits
from aerolink.sim.scenarios import scenario_from_dict
from aerolink.sim.state import FlightPlan
from aerolink.sim.world_config import EnergyConfig, WorldToggles

ENERGY = WorldToggles(energy=True)
RESERVE = EnergyConfig().reserve_soc


def plan(path: list[str], etas: dict[str, float], purpose: str = "passenger") -> FlightPlan:
    return FlightPlan(
        flight_id=uuid4(),
        request_id=uuid4(),
        route_id=uuid4(),
        path=path,
        destination_node=path[-1],
        departure_time=min(etas.values()),
        eta_by_node=etas,
        purpose=purpose,  # ty: ignore[invalid-argument-type]
    )


# ---------------------------------------------------------------- 1. one reachability rule
def test_dispatch_only_assigns_trips_the_pilot_accepts(line_network: Network):
    """A dispatch is assigned exactly when the pilot's own rule (``path_fits``, reserve included) passes."""
    path = line_network.shortest_paths("h", "h2", vehicle_type="medical", k=1)[0]
    need = trip_energy_estimate(line_network, path, "medical", EnergyConfig()) + RESERVE
    for soc, assigned in ((need - 0.002, False), (need + 0.002, True)):
        ctl = make_controller(line_network, {"m1": ("medical", "h")}, world=ENERGY)
        ac = ctl.world.aircraft["m1"]
        ac.battery_soc = soc
        assert path_fits(line_network, ac, path, EnergyConfig()) is assigned
        ctl.dispatch_medical("h2")
        ctl.step()
        assert bool(kinds(ctl, evlib.DEPARTURE)) is assigned
        assert kinds(ctl, evlib.ROUTE_UNAVAILABLE) == []  # never assigned-then-refused


# ---------------------------------------------------------------- 2. one route_unavailable per stuck dispatch
class NoRoutes(ShortestPathAdvisor):
    def request_routes(self, origin, destination, vehicle_type, t_s):
        return []


def test_stuck_dispatch_logs_route_unavailable_once(line_network: Network):
    ctl = make_controller(line_network, {"m1": ("medical", "h")}, advisor=NoRoutes(line_network))
    ctl.dispatch_medical("h2")
    for _ in range(40):
        ctl.step()
    assert len(kinds(ctl, evlib.ROUTE_UNAVAILABLE)) == 1
    assert len(ctl.pending_dispatches) == 1  # still waiting, just quiet


# ---------------------------------------------------------------- 3. destinations reachable on a full battery
def test_destinations_are_reachable_on_a_full_battery(far_network: Network):  # noqa: F811
    import numpy as np

    ctl = make_controller(far_network, {"m1": ("medical", "s")}, world=ENERGY)
    ac = ctl.world.aircraft["m1"]
    ac.battery_soc = 0.05  # a flat battery does not matter: the aircraft charges first
    rng = np.random.default_rng(0)
    energy = EnergyConfig()
    # from s only x (80 km) fits one battery; h1 (165 km) and h2 (200 km) never do
    assert {choose_destination(far_network, ac, rng, 0.0, energy) for _ in range(20)} == {"x"}
    ac.current_node = "h1"
    ac.home_node = "h1"
    assert choose_destination(far_network, ac, rng, 0.0, energy) is None  # nothing in range of a full battery
    assert choose_destination(far_network, ac, rng, 0.0, None) is None  # same without the energy model


# ---------------------------------------------------------------- 4. parked aircraft keep charging; due trip waits
def test_parked_aircraft_keep_charging(line_network: Network):
    ctl = make_controller(line_network, {"p1": ("passenger", "a")}, world=ENERGY)
    ac = ctl.world.aircraft["p1"]
    ac.battery_soc = 0.5
    for _ in range(100):
        ctl.step()
    assert ac.battery_soc == pytest.approx(0.5 + 100 * EnergyConfig().charge_rate_soc_per_s)


def test_due_trip_waits_to_charge_then_departs(line_network: Network):
    ctl = make_controller(line_network, {"p1": ("passenger", "a")}, world=ENERGY)
    ac = ctl.world.aircraft["p1"]
    ac.battery_soc = 0.05
    ac.next_trip_s = 0.0
    ctl.step()
    (wait,) = kinds(ctl, evlib.CHARGE_WAIT)
    assert wait["destination"] in {"b", "c"} and kinds(ctl, evlib.TRIP_REJECTED) == []
    for _ in range(700):  # about 320 s of charging at 0.001 per second
        ctl.step()
    assert len(kinds(ctl, evlib.CHARGE_WAIT)) == 1  # one event per wait, not per step
    (dep,) = kinds(ctl, evlib.DEPARTURE)
    assert dep["destination"] == wait["destination"]  # the trip that was due, not a new draw
    assert dep["ground_delay_s"] > 0  # the wait shows up as ground delay, not hidden


def test_trip_that_fits_no_battery_is_rejected(line_network: Network):
    """A trip no full battery can fly is still rejected (and not retried every step)."""
    nodes = {"a": site(28.0, -81.5, ["passenger"]), "z": site(31.0, -81.5, ["passenger"])}  # ~330 km apart
    net = make_network(nodes, [("a", "z", 330_000.0)])
    ctl = make_controller(net, {"p1": ("passenger", "a")}, world=ENERGY)
    ctl.world.aircraft["p1"].next_trip_s = 0.0
    for _ in range(3):
        ctl.step()
    (rej,) = kinds(ctl, evlib.TRIP_REJECTED)
    assert rej["destination"] is None and kinds(ctl, evlib.CHARGE_WAIT) == []


# ---------------------------------------------------------------- 5-6. B1 traffic picture
def test_b1_pad_wait_when_a_planned_flight_lands_first(line_network: Network):
    b1 = B1Advisor(line_network)
    eta_free = b1.request_routes("c", "b", "passenger", 0.0)[0].eta_s  # 'b' has one pad
    before = plan(["a", "w1", "w2", "b"], {"b": eta_free - 30.0})
    alone = b1.request_routes("c", "b", "passenger", 0.0, [])[0]
    behind = b1.request_routes("c", "b", "passenger", 0.0, [before])[0]
    assert alone.predicted_pad_wait_s == 0.0
    assert behind.predicted_pad_wait_s == pytest.approx(b1.cfg.pad_service_s - 30.0)
    assert behind.total_predicted_s == pytest.approx(alone.total_predicted_s + behind.predicted_pad_wait_s)
    after = plan(["a", "w1", "w2", "b"], {"b": eta_free + 30.0})  # lands later: we are first
    assert b1.request_routes("c", "b", "passenger", 0.0, [after])[0].predicted_pad_wait_s == 0.0


def test_b1_medical_ignores_non_medical_traffic_ahead(line_network: Network):
    b1 = B1Advisor(line_network)
    eta = b1.request_routes("h", "h2", "medical", 0.0)[0].eta_s
    passenger_ahead = plan(["c", "w2", "h2"], {"h2": eta - 20.0}, purpose="passenger")
    medical_ahead = plan(["h", "w1", "w2", "h2"], {"h2": eta - 20.0}, purpose="medical_dispatch")
    assert b1.request_routes("h", "h2", "medical", 0.0, [passenger_ahead])[0].predicted_pad_wait_s == 0.0
    assert b1.request_routes("h", "h2", "medical", 0.0, [medical_ahead])[0].predicted_pad_wait_s > 0.0


def test_b1_makes_no_en_route_suggestions(line_network: Network):
    ctl = make_controller(line_network, {"p1": ("passenger", "a")})
    b1 = B1Advisor(line_network)
    assert b1.suggest(ctl.world.aircraft["p1"], "w1", 10.0) is None
    assert B1RouteAdvisorWrapper(b1).suggest(ctl.world.aircraft["p1"], "w1", 10.0) is None


# ---------------------------------------------------------------- 7. wrappers see the planned flights
@pytest.mark.parametrize("kind", ["b1", "a1", "aerolink"])
def test_advisors_see_flights_already_planned(line_network: Network, kind: str):
    if kind == "b1":
        advisor = B1RouteAdvisorWrapper(B1Advisor(line_network))
    elif kind == "a1":
        advisor = A1RouteAdvisorWrapper(A1Advisor(line_network, CVForecaster(line_network)))
    else:
        advisor = AeroLinkRouteAdvisorWrapper(
            AeroLinkAdvisor(line_network, CVForecaster(line_network), ABLATIONS["A1"])
        )
    ctl = make_controller(line_network, {"p1": ("passenger", "a"), "p2": ("passenger", "c")}, advisor=advisor)
    advisor.bind(ctl.world, ctl)
    assert ctl.request_trip("p1", "b")
    seen = advisor.advisor.planned_flights() if isinstance(advisor, AeroLinkRouteAdvisorWrapper) else advisor.planned()
    assert [p.destination_node for p in seen] == ["b"]
    # p2 would land after p1 at the one-pad site: the engine knows, because it sees p1's plan
    (best, *_) = advisor.predict("c", "b", "passenger", 200.0)
    assert best.predicted_pad_wait_s > 0.0


# ---------------------------------------------------------------- 8. suggestions compare like with like
def diamond() -> Network:
    """a --- m1 --- b (short)  and  a --- m2 --- b (about 8% longer)."""
    nodes = {
        "a": site(28.00, -81.50, ["passenger"]),
        "m1": wp(28.10, -81.50),
        "m2": wp(28.10, -81.45),
        "b": site(28.20, -81.50, ["passenger"]),
    }
    pairs = [("a", "m1"), ("m1", "b"), ("a", "m2"), ("m2", "b")]
    edges = [
        (u, v, segment_length_m((nodes[u]["lon"], nodes[u]["lat"]), (nodes[v]["lon"], nodes[v]["lat"])))
        for u, v in pairs
    ]
    return make_network(nodes, edges)


def test_a5_suggestion_uses_one_traffic_picture_and_fires_when_clearly_better():
    net = diamond()
    adv = AeroLinkAdvisor(net, CVForecaster(net), ABLATIONS["A5"])
    wrapper = AeroLinkRouteAdvisorWrapper(adv)
    ctl = make_controller(net, {"p1": ("passenger", "a")}, advisor=wrapper)
    wrapper.bind(ctl.world, ctl)
    assert ctl.request_trip("p1", "b")
    ac = ctl.world.aircraft["p1"]
    assert ac.active_flight is not None and ac.active_flight.path == ["a", "m1", "b"]
    eta_m1 = ac.active_flight.eta_by_node["m1"]
    # six other flights pass m1 at the same time as p1 (they head elsewhere), none pass m2
    others = [plan(["x", "m1", "y"], {"m1": eta_m1 + i}) for i in range(6)]

    quiet = adv.suggest(ac, "a", 0.0, [])
    assert quiet is None  # no traffic: the current, shorter route is already best
    busy = adv.suggest(ac, "a", 0.0, others)
    assert busy is not None and busy.path == ["a", "m2", "b"]
    assert adv.last_gain_s is not None and adv.last_gain_s > 0.0
    # option cost and current-route cost come from the same picture: re-scoring the chosen path agrees
    again = adv._cost_from_eta(
        busy.path, CVForecaster(net).predict_eta(busy.path, 0.0, "passenger"), "passenger", 0.0, others
    )
    assert again == pytest.approx(busy.total_predicted_s)
    # the plan being flown never counts against itself (the wrapper leaves it out)
    assert wrapper.suggest(ac, "a", 0.0) is None
    # without en-route suggestions (A4) nothing is offered
    a4 = AeroLinkAdvisor(net, CVForecaster(net), ABLATIONS["A4"])
    assert a4.suggest(ac, "a", 0.0, others) is None


# ---------------------------------------------------------------- E1 sim fixes found building the scenarios
def _scenario(**kw) -> dict:
    base = {
        "duration_s": 1500,
        "start_time": "2026-11-20T07:00:00-05:00",
        "fleet": {"passenger": 2, "medical": 0},
        "world": {"separation": True, "schedules": True},
        "demand": {"peak_rate_per_hour": 0.0, "off_peak_rate_per_hour": 0.0, "medical_dispatch_mean_interval_s": 0},
        "pad_turnaround_time_s": 60,
    }
    base.update(kw)
    return base


def test_shuttle_departs_at_each_repeat_time_not_all_at_once(fl_network: Network):
    leg = {"aircraft": "passenger", "origin": "ev-sef", "destination": "ev-lal", "departure_local": "07:01:00"}
    sc = scenario_from_dict(_scenario(schedule=[{**leg, "repeat_every_s": 600}]))
    ev = run_simulation(fl_network, sc, record_truth=False).events
    fired = [e["t"] for e in ev if e["kind"] in (evlib.SCHEDULED_DEPARTURE, evlib.SCHEDULE_CONFLICT)]
    assert fired == [60.0, 660.0, 1260.0]  # was: every repeat fired at the first time


def test_legs_scheduled_for_the_same_minute_from_different_sites_both_depart(fl_network: Network):
    sc = scenario_from_dict(
        _scenario(
            fleet_staging={"ev-sef": 1, "ev-lal": 1},
            schedule=[
                {
                    "aircraft": "passenger",
                    "origin": "ev-sef",
                    "destination": "ev-suntrax",
                    "departure_local": "07:01:00",
                },
                {
                    "aircraft": "passenger",
                    "origin": "ev-lal",
                    "destination": "ev-suntrax",
                    "departure_local": "07:01:00",
                },
            ],
        )
    )
    ev = run_simulation(fl_network, sc, record_truth=False).events
    assert sorted(e["origin"] for e in ev if e["kind"] == evlib.SCHEDULED_DEPARTURE) == ["ev-lal", "ev-sef"]


def test_scheduled_leg_waiting_for_a_free_pad_departs_late_instead_of_being_dropped(fl_network: Network):
    """Three legs 10 s apart from one site: takeoffs queue, and the legs wait rather than become conflicts."""
    leg = {"aircraft": "passenger", "origin": "ev-suntrax", "destination": "ev-lal"}
    sc = scenario_from_dict(
        _scenario(
            fleet={"passenger": 3, "medical": 0},
            fleet_staging={"ev-suntrax": 3},
            world={"separation": True, "schedules": True, "energy": True, "pad_booking": True},
            schedule=[{**leg, "departure_local": f"07:01:{s:02d}"} for s in (0, 10, 20)],
        )
    )
    res = run_simulation(fl_network, sc, check_invariants=True, record_truth=False)
    ev = res.events
    assert res.violations == []
    assert [e for e in ev if e["kind"] == evlib.SCHEDULE_CONFLICT] == []  # was: legs 2 and 3 dropped
    deps = [e for e in ev if e["kind"] == evlib.DEPARTURE]
    assert [d["request_t"] for d in deps] == [60.0, 70.0, 80.0]  # the request time is the schedule time
    assert deps[0]["ground_delay_s"] == 0.0 and all(d["ground_delay_s"] > 0.0 for d in deps[1:])


def test_fleet_staging_places_aircraft_and_empty_staging_changes_nothing(fl_network: Network):
    staged = scenario_from_dict(_scenario(fleet={"passenger": 3, "medical": 0}, fleet_staging={"ev-orl": 2}))
    res = run_simulation(fl_network, staged.with_overrides(duration_s=5), record_truth=False)
    homes = [a.home_node for a in res.final_world.aircraft.values()]
    assert homes.count("ev-orl") >= 2
    plain = scenario_from_dict(_scenario(fleet={"passenger": 3, "medical": 0}))
    assert plain.fleet_staging == {}
    with pytest.raises(ValueError, match="staging"):
        scenario_from_dict(_scenario(fleet_staging={"ev-orl": 5}))


def test_scripted_medical_incident_fires_once_at_its_local_time(fl_network: Network):
    sc = scenario_from_dict(
        _scenario(
            fleet={"passenger": 0, "medical": 2},
            medical_incidents=[{"at_local": "07:05:00", "node": "hp-7fd7"}],
        )
    )
    ev = [e for e in run_simulation(fl_network, sc, record_truth=False).events if e["kind"] == "medical_incident"]
    assert [(e["t"], e["node"], e["scripted"]) for e in ev] == [(300.0, "hp-7fd7", True)]
    with pytest.raises(ValueError, match="medical_incidents"):
        scenario_from_dict(_scenario(medical_incidents=[{"node": "hp-7fd7"}]))


def test_gbm_features_are_set_for_the_current_route_when_scoring_a_suggestion():
    """The current route used to be scored with the traffic_ahead/local_hour left from the last option."""
    from aerolink.forecast.gbm import GBMForecaster

    net = diamond()
    cv = CVForecaster(net)
    seen: list[tuple[tuple[str, ...], float, float | None]] = []

    class Recording(GBMForecaster):
        def __init__(self):  # no trained model needed: only the inputs are of interest
            self.traffic_ahead = -1.0
            self.local_hour = -1.0

        def predict_eta(self, path, origin_t_s, vehicle_type, track=None, current_speed_mps=None):
            seen.append((tuple(path), self.traffic_ahead, self.local_hour))
            return cv.predict_eta(path, origin_t_s, vehicle_type)

    adv = AeroLinkAdvisor(net, Recording(), ABLATIONS["A5"])
    wrapper = AeroLinkRouteAdvisorWrapper(adv)
    ctl = make_controller(net, {"p1": ("passenger", "a")}, advisor=wrapper)
    wrapper.bind(ctl.world, ctl)
    assert ctl.request_trip("p1", "b")
    others = [plan(["x", "m1", "y"], {"m1": 100.0 + i}) for i in range(3)]  # three flights through m1 only
    seen.clear()
    adv.suggest(ctl.world.aircraft["p1"], "a", 0.0, others)
    by_path = {p: ahead for p, ahead, _ in seen}
    assert by_path[("a", "m1", "b")] == 3.0  # the current route meets all three
    assert by_path[("a", "m2", "b")] == 0.0  # the alternative meets none: not the other's leftover
    assert all(hour == adv.local_hour(0.0) for _, _, hour in seen)


def fork() -> Network:
    """a --- x, then x --- m1 --- b (short) or x --- m2 --- b (~8% longer); six-pad site c reaches m1 from the side."""
    nodes = {
        "a": site(28.00, -81.50, ["passenger"]),
        "x": wp(28.10, -81.50),
        "m1": wp(28.20, -81.50),
        "m2": wp(28.20, -81.45),
        "b": site(28.30, -81.50, ["passenger"]),
        "c": site(28.20, -81.30, ["passenger"], pads=6),  # ~20 km east of m1
        "d": site(28.25, -81.50, ["passenger"], pads=6),  # not used as a destination below
    }
    pairs = [("a", "x"), ("x", "m1"), ("m1", "b"), ("x", "m2"), ("m2", "b"), ("c", "m1")]
    edges = [
        (u, v, segment_length_m((nodes[u]["lon"], nodes[u]["lat"]), (nodes[v]["lon"], nodes[v]["lat"])))
        for u, v in pairs
    ]
    return make_network(nodes, edges)


def test_a5_suggestion_fires_in_the_simulator_when_traffic_builds_up_after_departure():
    """End to end through the controller: p1 leaves with no traffic ahead (so takes the short route), six
    aircraft then converge on m1 at the same time, and at the fork p1 is offered, and accepts, the other route."""
    net = fork()
    adv = AeroLinkAdvisor(net, CVForecaster(net), ABLATIONS["A5"])
    wrapper = AeroLinkRouteAdvisorWrapper(adv)
    placements = {"p1": ("passenger", "a")} | {f"q{i}": ("passenger", "c") for i in range(6)}
    ctl = make_controller(net, placements, advisor=wrapper)
    wrapper.bind(ctl.world, ctl)
    assert ctl.request_trip("p1", "b")
    p1 = ctl.world.aircraft["p1"]
    plan1 = p1.active_flight
    assert plan1 is not None and plan1.path == ["a", "x", "m1", "b"]
    d_x_m1 = net.edges[("x", "m1")].length_m
    d_c_m1 = net.edges[("c", "m1")].length_m
    launch_t = plan1.eta_by_node["x"] + (d_x_m1 - d_c_m1) / 60.0  # q's reach m1 when p1 would
    launched = False
    for _ in range(4000):
        ctl.step()
        if not launched and ctl.world.t >= launch_t:
            for i in range(6):  # all six head for b through m1 (their only way)
                assert ctl.request_trip(f"q{i}", "b")
            launched = True
        if kinds(ctl, evlib.SUGGESTION):
            break
    (sug, *_) = kinds(ctl, evlib.SUGGESTION)
    assert sug["aircraft_id"] == "p1" and sug["node"] == "x" and sug["path"] == ["x", "m2", "b"]
    assert sug["accepted"] is True and sug["predicted_gain_s"] > 0.0


def test_in_flight_queries_pass_the_current_speed_and_departures_do_not():
    """GBM treats a query without an observed speed as a from-rest departure (``from_rest`` = 1). Suggestions
    and off-route recalculation are in-flight queries and must pass the aircraft's speed."""
    from aerolink.forecast.gbm import GBMForecaster

    net = diamond()
    cv = CVForecaster(net)
    speeds: list[float | None] = []

    class Recording(GBMForecaster):
        def __init__(self):
            self.traffic_ahead = 0.0
            self.local_hour = 0.0

        def predict_eta(self, path, origin_t_s, vehicle_type, track=None, current_speed_mps=None):
            speeds.append(current_speed_mps)
            return cv.predict_eta(path, origin_t_s, vehicle_type)

    adv = AeroLinkAdvisor(net, Recording(), ABLATIONS["A5"])
    wrapper = AeroLinkRouteAdvisorWrapper(adv)
    ctl = make_controller(net, {"p1": ("passenger", "a")}, advisor=wrapper)
    wrapper.bind(ctl.world, ctl)
    assert ctl.request_trip("p1", "b")  # a departure request
    assert speeds and all(v is None for v in speeds)  # no observed speed: from_rest
    ac = ctl.world.aircraft["p1"]
    ac.velocity_mps = 57.0
    speeds.clear()
    adv.suggest(ac, "a", 0.0, [])  # in flight
    assert speeds and all(v == 57.0 for v in speeds)  # options and the current route alike
    speeds.clear()
    adv.recalculate("f1", 28.0, -81.5, "b", "passenger", 0.0, current_speed_mps=48.0)
    assert speeds and all(v == 48.0 for v in speeds)
    speeds.clear()
    adv.recalculate("f1", 28.0, -81.5, "b", "passenger", 0.0)  # old call shape still works, as a departure query
    assert speeds and all(v is None for v in speeds)
