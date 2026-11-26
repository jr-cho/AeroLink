"""E5.5: off-route checks, end-to-end evaluation, recalculation and reachable mass."""

import json
from pathlib import Path

import numpy as np
import pytest

from aerolink.forecast.cv import CVForecaster
from aerolink.network import Network
from aerolink.offroute.detector import consecutive, cross_track_m, reachable_mass, route_polyline
from aerolink.offroute.evaluate import main
from aerolink.realdata.synthetic_days import make_days
from aerolink.routing.aerolink import ABLATIONS, AeroLinkAdvisor


def test_consecutive_needs_three_and_clears_slowly():
    f = np.array([1, 1, 0, 1, 1, 1, 0, 1, 0, 0, 0], dtype=bool)
    assert consecutive(f).tolist() == [0, 0, 0, 0, 0, 1, 1, 1, 1, 0, 0]


def test_cross_track_follows_corridor_shape(fl_network: Network):
    path = fl_network.shortest_paths("ev-lal", "ev-water-st", "passenger", k=1)[0]
    route = route_polyline(fl_network, path)
    lat, lon = route[len(route) // 2]
    assert cross_track_m(lat, lon, route) < 1.0
    assert cross_track_m(lat + 0.02, lon, route) > 300  # ~2 km north of a point on the route


def test_reachable_mass_is_one_aircraft(fl_network: Network):
    n = fl_network.nodes["ev-lal"]
    mass = reachable_mass(fl_network, n["lat"], n["lon"], 50.0, 100.0, horizon_s=900)
    assert mass and sum(p for p, *_ in mass) == pytest.approx(1.0)
    assert any(node == "ev-lal" for _, node, *_ in mass)


def test_recalculate_marks_off_route(fl_network: Network):
    adv = AeroLinkAdvisor(fl_network, CVForecaster(fl_network), ABLATIONS["A4"])
    n = fl_network.nodes["ev-lal"]
    opts = adv.recalculate("flight-1", n["lat"] + 0.001, n["lon"], "ev-water-st", "passenger", 0.0)
    assert adv.flight_status["flight-1"] == "off_route"
    assert opts and opts[0].path[-1] == "ev-water-st"


def test_evaluate_end_to_end(tmp_path: Path):
    data = tmp_path / "adsblol"
    make_days(data, days=3, flights_per_day=4)
    out = tmp_path / "e55.json"
    args = ["--data", str(data), "--forecaster", "cv", "--train-seeds", "1", "--seeds", "201", "202"]
    assert main([*args, "--model-dir", str(tmp_path / "m"), "--out", str(out)]) == 0
    r = json.loads(out.read_text())
    assert set(r["results"]) == {"fixed_bounds", "tolerance", "schedule", "band", "anomaly", "ai"}
    assert (tmp_path / "m" / "offroute.joblib").exists()
