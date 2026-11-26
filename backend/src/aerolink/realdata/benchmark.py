"""Forecasters on real flights: position-head error per horizon (PLAN E5.2).

    uv run python -m aerolink.realdata.benchmark                          # cv + chronos
    uv run python -m aerolink.realdata.benchmark --forecasters cv chronos aerolink-gbm

Days are split chronologically (``samples.split_days``): the latest ~30% are held out and
never used for training. Only held-out samples are scored here. For every forecaster and
horizon (30/60/120/300 s) it reports:

* ``fde_*``: horizontal distance between the p50 prediction and the true position at that
  horizon (mean, median, p90), in metres
* ``ade_mean_m``: the same error averaged over all horizons up to this one
* ``coverage_*``: how often the true offset falls inside the p10-p90 interval
  (east, north, up separately, and east+north together); the target for a calibrated
  model is 80% per axis

Results: ``backend/eval-results/realdata-e5.2.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, cast

import numpy as np

from aerolink.forecast.features import to_local
from aerolink.forecast.interface import PositionPrediction
from aerolink.realdata.fetch import ATTRIBUTION
from aerolink.realdata.samples import DATA_DIR, PositionSamples, build_position_samples, list_days, split_days

BACKEND = Path(__file__).resolve().parents[3]
DEFAULT_OUT = BACKEND / "eval-results" / "realdata-e5.2.json"


def make_forecaster(name: str, chronos_model: str):
    if name == "cv":
        from aerolink.forecast.cv import CVForecaster

        return CVForecaster(cast(Any, None))  # the position head needs no network
    if name == "chronos":
        from aerolink.forecast.chronos import ChronosForecaster

        return ChronosForecaster(None, chronos_model)
    if name == "aerolink-gbm":
        from aerolink.forecast.gbm import GBMForecaster

        return GBMForecaster.load(None)
    raise ValueError(f"unknown forecaster {name!r}")


def predict_all(forecaster, samples: PositionSamples) -> list[list[PositionPrediction]]:
    """Predictions for every sample, batched when the forecaster supports it."""
    tracks = [samples.history[i] for i in range(len(samples))]
    if hasattr(forecaster, "predict_position_batch"):
        kwargs = {}
        if getattr(forecaster, "name", "") == "aerolink-gbm":
            kwargs = {"vehicles": list(samples.vehicle), "hours": list(samples.hour)}
        return forecaster.predict_position_batch(tracks, samples.horizons_s, **kwargs)
    return [forecaster.predict_position([tuple(p) for p in tr], 0.0, "passenger", samples.horizons_s) for tr in tracks]


def score(samples: PositionSamples, preds: list[list[PositionPrediction]]) -> list[dict]:
    """Per-horizon error and interval coverage."""
    n, h = len(samples), len(samples.horizons_s)
    err = np.full((n, h), np.nan)
    inside = np.full((n, h, 3), np.nan)
    for i in range(n):
        lat0, lon0 = samples.history[i, -1, 0], samples.history[i, -1, 1]
        alt0 = samples.history[i, -1, 2]
        for j in range(h):
            fut = samples.future[i, j]
            if np.isnan(fut[0]):
                continue
            te, tn = to_local(np.array([fut[0]]), np.array([fut[1]]), lat0, lon0)
            truth = (float(te[0]), float(tn[0]), float(fut[2] - alt0))
            p = preds[i][j]
            err[i, j] = float(np.hypot(p.east_p50 - truth[0], p.north_p50 - truth[1]))
            inside[i, j, 0] = p.east_p10 <= truth[0] <= p.east_p90
            inside[i, j, 1] = p.north_p10 <= truth[1] <= p.north_p90
            inside[i, j, 2] = p.up_p10 <= truth[2] <= p.up_p90
    rows = []
    for j, horizon in enumerate(samples.horizons_s):
        ok = ~np.isnan(err[:, j])
        if not ok.any():
            rows.append({"horizon_s": horizon, "n": 0})
            continue
        e = err[ok, j]
        upto = err[:, : j + 1]
        full = ~np.isnan(upto).any(axis=1)
        cov = inside[ok, j]
        rows.append(
            {
                "horizon_s": horizon,
                "n": int(ok.sum()),
                "fde_mean_m": round(float(e.mean()), 1),
                "fde_median_m": round(float(np.median(e)), 1),
                "fde_p90_m": round(float(np.quantile(e, 0.9)), 1),
                "ade_mean_m": round(float(upto[full].mean()), 1) if full.any() else None,
                "coverage_east": round(float(cov[:, 0].mean()), 3),
                "coverage_north": round(float(cov[:, 1].mean()), 3),
                "coverage_up": round(float(cov[:, 2].mean()), 3),
                "coverage_horizontal": round(float((cov[:, 0] * cov[:, 1]).mean()), 3),
            }
        )
    return rows


def print_table(results: dict) -> None:
    print(f"\n{'forecaster':<14}{'horizon':>8}{'n':>8}{'FDE mean':>10}{'median':>9}{'p90':>9}{'ADE':>9}{'cov E/N':>12}")
    for name, rows in results.items():
        for r in rows:
            if not r["n"]:
                continue
            cov = f"{r['coverage_east']:.0%}/{r['coverage_north']:.0%}"
            print(
                f"{name:<14}{r['horizon_s']:>7}s{r['n']:>8}{r['fde_mean_m']:>9.0f}m{r['fde_median_m']:>8.0f}m"
                f"{r['fde_p90_m']:>8.0f}m{r['ade_mean_m'] or 0:>8.0f}m{cov:>12}"
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--forecasters", nargs="+", default=["cv", "chronos"])
    ap.add_argument("--chronos-model", default="autogluon/chronos-2-small")
    ap.add_argument("--data", type=Path, default=DATA_DIR)
    ap.add_argument("--held-out-fraction", type=float, default=0.3)
    ap.add_argument("--stride-s", type=int, default=30, help="seconds between samples along a flight")
    ap.add_argument("--max-samples", type=int, default=5000, help="random subset of held-out samples (0 = all)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args(argv)

    days = list_days(a.data)
    train_days, test_days = split_days(days, a.held_out_fraction)
    print(f"{len(days)} days: training {train_days[0]}..{train_days[-1]}, held out {test_days[0]}..{test_days[-1]}")
    samples = build_position_samples(test_days, a.data, stride_s=a.stride_s)
    total = len(samples)
    if a.max_samples and total > a.max_samples:
        keep = np.zeros(total, dtype=bool)
        keep[np.random.default_rng(a.seed).choice(total, a.max_samples, replace=False)] = True
        samples = samples.subset(keep)
    print(f"held-out samples: {total:,} (scoring {len(samples):,})")
    if not len(samples):
        print("No samples: collect more days first (E5.1).")
        return 1

    results: dict[str, list[dict]] = {}
    timing: dict[str, float] = {}
    for name in a.forecasters:
        print(f"running {name} ...", flush=True)
        started = time.monotonic()
        preds = predict_all(make_forecaster(name, a.chronos_model), samples)
        timing[name] = round((time.monotonic() - started) / len(samples) * 1000, 2)
        results[name] = score(samples, preds)
    print_table(results)
    report = {
        "attribution": ATTRIBUTION,
        "split": {"train_days": train_days, "held_out_days": test_days},
        "samples": {"held_out_total": total, "scored": len(samples), "stride_s": a.stride_s, "seed": a.seed},
        "chronos_model": a.chronos_model if "chronos" in a.forecasters else None,
        "ms_per_sample": timing,
        "results": results,
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
