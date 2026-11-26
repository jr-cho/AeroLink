"""E5.2: day split, real-data samples, benchmark CLI, Chronos-2 wrapper (no downloads)."""

import json
from pathlib import Path

import numpy as np
import pytest
import torch

from aerolink.forecast.chronos import ChronosForecaster
from aerolink.realdata.benchmark import main
from aerolink.realdata.samples import build_position_samples, list_days, split_days
from aerolink.realdata.synthetic_days import make_days


@pytest.fixture(scope="module")
def fake_days(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("adsblol")
    make_days(out, days=4, flights_per_day=6)
    return out


def test_split_is_by_day_and_chronological():
    train, test = split_days(["2026-01-03", "2026-01-01", "2026-01-02", "2026-01-04"])
    assert train == ["2026-01-01", "2026-01-02", "2026-01-03"] and test == ["2026-01-04"]
    with pytest.raises(ValueError):
        split_days(["2026-01-01"])


def test_samples_have_history_and_future(fake_days: Path):
    days = list_days(fake_days)
    s = build_position_samples(days[:1], fake_days)
    assert len(s) > 0
    assert s.history.shape[1:] == (60, 3)
    assert s.future.shape[1:] == (4, 3)
    assert set(s.vehicle) <= {"rotor", "light"}
    assert np.isfinite(s.future[:, 0, 0]).all()  # 30 s ahead always exists


def test_benchmark_cv_writes_results(fake_days: Path, tmp_path: Path):
    out = tmp_path / "e52.json"
    assert main(["--data", str(fake_days), "--forecasters", "cv", "--out", str(out)]) == 0
    report = json.loads(out.read_text())
    rows = report["results"]["cv"]
    assert [r["horizon_s"] for r in rows] == [30, 60, 120, 300]
    assert rows[0]["fde_mean_m"] < rows[-1]["fde_mean_m"]  # error grows with horizon
    assert "ODbL" in report["attribution"]


class FakeChronos2:
    """Stands in for Chronos2Pipeline.predict_quantiles: straight-line forecast, +-10% spread."""

    def predict_quantiles(self, context, prediction_length, quantile_levels):
        out = []
        for series in context:  # (V, T)
            step = series[:, -1] - series[:, -2]
            h = torch.arange(1, prediction_length + 1, dtype=torch.float32)
            mid = series[:, -1:] + step[:, None] * h
            q = torch.stack([mid - 0.1 * mid.abs(), mid, mid + 0.1 * mid.abs()], dim=-1)
            out.append(q)
        return out, [o[..., 1] for o in out]


def test_chronos_position_batch_uses_quantiles():
    f = ChronosForecaster(None)
    f._pipeline = FakeChronos2()
    track = np.array([[28.0 + i * 1e-4, -82.0, 300.0] for i in range(60)])  # flying north ~11 m/s
    preds = f.predict_position_batch([track, track], [30, 300])
    assert len(preds) == 2 and [p.horizon_s for p in preds[0]] == [30, 300]
    p = preds[0][0]
    assert p.north_p10 <= p.north_p50 <= p.north_p90
    assert 300 < p.north_p50 < 360 and abs(p.east_p50) < 1  # ~11 m/s x 30 s, relative to the LAST fix


def test_time_to_cover_uses_profile_then_cruise():
    profile = np.full(10, 20.0)
    assert ChronosForecaster._time_to_cover(profile, 100.0, 50.0) == pytest.approx(5.0)
    assert ChronosForecaster._time_to_cover(profile, 300.0, 50.0) == pytest.approx(12.0)
