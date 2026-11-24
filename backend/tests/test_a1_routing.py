"""Tests for A1 AI route engine and traffic forecast (PLAN E4)."""

from __future__ import annotations

import uuid

import numpy as np

from aerolink.forecast.cv import CVForecaster
from aerolink.network import Network
from aerolink.routing.a1 import A1Advisor, A1Config, A1RouteAdvisorWrapper
from aerolink.sim.state import AircraftState, FlightPlan, SimPosition
from aerolink.traffic import forecast_traffic, node_capacity


def test_a1_returns_options(fl_a1: A1Advisor) -> None:
    options = fl_a1.request_routes("ev-lal", "ev-orl", "passenger", 0.0)
    assert len(options) >= 1
    best = options[0]
    assert best.path[0] == "ev-lal"
    assert best.path[-1] == "ev-orl"
    assert best.eta_s >= 0.0
    assert best.reason
    assert best.total_predicted_s >= 0


def test_a1_sorts_by_cost(fl_a1: A1Advisor) -> None:
    options = fl_a1.request_routes("ev-lal", "ev-orl", "passenger", 0.0)
    costs = [o.total_predicted_s for o in options]
    assert costs == sorted(costs)


def test_a1_wrapper_matches_controller_protocol(fl_a1: A1Advisor) -> None:
    wrapper = A1RouteAdvisorWrapper(fl_a1)
    paths = wrapper.request_routes("ev-lal", "ev-orl", "passenger", 0.0)
    assert all(isinstance(p, list) for p in paths)
    assert paths[0][0] == "ev-lal"
    assert paths[0][-1] == "ev-orl"


def test_traffic_forecast_empty_planned(fl_network: Network) -> None:
    caps = {
        nid: node_capacity(props) for nid, props in fl_network.nodes.items() if "waypoint" not in props.get("types", [])
    }
    forecast = forecast_traffic([], caps)
    assert all(v == 0.0 for nt in forecast.bins.values() for v in nt.p50_arrivals.values())
    assert all(forecast.level_at(node, 0.0) == "low" for node in forecast.bins)


def test_a1_medical_priority_lowers_wait(fl_network: Network) -> None:
    a1 = A1Advisor(fl_network, CVForecaster(fl_network), cfg=A1Config())
    med = a1.request_routes("hp-60fd", "hp-61fl", "medical", 0.0)
    pax = a1.request_routes("ev-lal", "ev-orl", "passenger", 0.0)
    assert med and pax
    assert med[0].total_predicted_s <= pax[0].total_predicted_s + 1e-6


def test_a1_suggest_requires_improvement(fl_a1: A1Advisor) -> None:
    plan = FlightPlan(
        flight_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        route_id=uuid.uuid4(),
        path=["ev-lal", "wp-lakeland", "ev-orl"],
        destination_node="ev-orl",
        departure_time=0.0,
        eta_by_node={"ev-lal": 0.0, "wp-lakeland": 100.0, "ev-orl": 200.0},
        purpose="passenger",
    )
    rng = np.random.default_rng(0)
    ac = AircraftState(
        aircraft_id="sim-p00",
        vehicle_type="passenger",
        home_node="ev-lal",
        position=SimPosition(lat=0.0, lon=0.0, alt_msl_m=0.0),
        rng=rng,
        telemetry_rng=rng,
        active_flight=plan,
        legs=[],
        current_node="ev-lal",
        status="parked",
    )
    opt = fl_a1.suggest(ac, "ev-lal", 0.0)
    assert opt is None
