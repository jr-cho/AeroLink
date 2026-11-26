"""E5.3: train a small aerolink-gbm end to end and check its outputs."""

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from aerolink.forecast.features import from_track_frame, position_targets, to_track_frame
from aerolink.forecast.gbm import GBMForecaster
from aerolink.forecast.train_gbm import main
from aerolink.network import Network
from aerolink.realdata.synthetic_days import make_days

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def trained(tmp_path_factory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("gbm")
    data, scenarios, models = root / "adsblol", root / "scenarios", root / "model"
    make_days(data, days=3, flights_per_day=8)
    scenarios.mkdir()
    shutil.copy(BACKEND / "scenarios" / "i4_peak.yaml", scenarios)
    out = root / "e53.json"
    args = ["--data", str(data), "--scenarios", str(scenarios), "--train-seeds", "1", "--test-seeds", "2"]
    assert main([*args, "--model-dir", str(models), "--out", str(out)]) == 0
    return models, out


def test_track_frame_round_trip():
    for course in [0.0, 45.0, 200.0]:
        a, c = to_track_frame(30.0, -40.0, course)
        e, n = from_track_frame(a, c, course)
        assert e == pytest.approx(30.0) and n == pytest.approx(-40.0)


def test_straight_flight_has_zero_residual():
    hist = np.array([[28.0 + i * 1e-4, -82.0, 300.0] for i in range(60)])
    fut = np.array([28.0 + 89 * 1e-4, -82.0, 300.0])  # 30 s later, same speed
    along, cross, up = position_targets(hist, fut, 30)
    assert abs(along) < 1 and abs(cross) < 1 and up == 0


def test_model_files_and_manifest(trained):
    models, out = trained
    manifest = json.loads((models / "manifest.json").read_text())
    assert manifest["seed"] == 42 and manifest["sim_train_seeds"] == [1]
    assert len(manifest["sha256"]) == 64
    report = json.loads(out.read_text())
    assert report["position_real_held_out"]["aerolink-gbm"][0]["n"] > 0
    assert report["eta_sim_held_out"]


def test_gbm_forecaster_predicts_ordered_quantiles(trained, fl_network: Network):
    models, _ = trained
    f = GBMForecaster.load(fl_network, models)
    path = fl_network.shortest_paths("ev-lal", "ev-water-st", "passenger", k=1)[0]
    eta = f.predict_eta(path, 100.0, "passenger")
    assert [e.node for e in eta.etas] == path
    last = eta.etas[-1]
    assert 100.0 < last.eta_p10 <= last.eta_p50 <= last.eta_p90
    track = [(28.0 + i * 1e-4, -82.0, 300.0) for i in range(60)]
    for p in f.predict_position(track, 0.0, "passenger", [30, 120]):
        assert p.east_p10 <= p.east_p50 <= p.east_p90 and p.north_p10 <= p.north_p50 <= p.north_p90
