"""Train the off-route models and measure detection on ``rogue_aircraft`` (PLAN E5.5).

    uv run python -m aerolink.offroute.evaluate

1. **Train** (saved to ``backend/models/offroute/offroute.joblib``):
   * the IsolationForest, on residual features of clean real flights (E5.1 training days,
     same day split as E5.2);
   * the route tolerance, on clean simulated flights (all scenarios, training seeds).
2. **Labelled ground truth:** run ``rogue_aircraft`` with held-out seeds and take every
   completed AeroLink flight. Half of them get a deviation injected at a random moment
   (the label): a ``turn`` away from the route (25-60 deg), a slow ``drift`` (5-10 deg),
   or a ``slow``-down to half speed. 5 m position noise is added to every track.
3. **Score** each method: a deviation is detected if the method raises a flag within
   300 s after it starts; every other new flag is a false alarm.

    precision = detections / (detections + false alarms)
    recall    = detections / injected deviations
    latency   = seconds from deviation start to the first flag (median, p90)

Results: ``backend/eval-results/offroute-e5.5.json``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from aerolink.forecast.features import to_local
from aerolink.forecast.simdata import (
    AIRBORNE,
    AT_SITE,
    _path_at,
    _path_history,
    _tracks,
    _vehicle,
    add_noise,
    graph_context,
    run_scenarios,
)
from aerolink.network import Network
from aerolink.offroute.detector import (
    MODEL_DIR,
    OffRouteModels,
    cross_track_m,
    detect,
    forecast_track,
    residual_features,
    route_polyline,
    smoothed_speed,
    tolerance_row,
    train_anomaly_model,
    train_tolerance_model,
)
from aerolink.realdata.samples import DATA_DIR, _segments, list_days, split_days
from aerolink.sim import load_scenario, run_simulation
from aerolink.sim.runner import SimulationResult
from aerolink.vehicle import vehicle_cfg

BACKEND = Path(__file__).resolve().parents[3]
DEFAULT_OUT = BACKEND / "eval-results" / "offroute-e5.5.json"
WINDOW_S = 300
METHODS = ["fixed_bounds", "tolerance", "schedule", "band", "anomaly", "ai"]


def load_forecaster(name: str, network: Network):
    if name == "aerolink-gbm":
        from aerolink.forecast.gbm import GBMForecaster

        return GBMForecaster.load(network)
    from aerolink.forecast.cv import CVForecaster

    return CVForecaster(network)


def flights(res: SimulationResult, network: Network, min_s: int = 240) -> Iterator[dict]:
    """Every completed AeroLink flight: its airborne 1 Hz track (until it reaches the site) and route."""
    tracks, paths = _tracks(res), _path_history(res)
    deps = {e["flight_id"]: e for e in res.events if e["kind"] == "departure"}
    for arr in (e for e in res.events if e["kind"] == "arrival" and e.get("flight_id") in deps):
        dep, aid = deps[arr["flight_id"]], arr["aircraft_id"]
        tr = tracks[aid]
        m = (tr["t"] > dep["t"]) & (tr["t"] <= arr["t"]) & np.isin(tr["status"], list(AIRBORNE - AT_SITE))
        idx = np.flatnonzero(m)
        if len(idx) < min_s:
            continue
        yield {
            "aircraft_id": aid,
            "vehicle": _vehicle(res, aid),
            "dep_t": float(dep["t"]),
            "t": tr["t"][idx],
            "xyz": tr["xyz"][idx],
            "path": _path_at(paths[aid], float(tr["t"][idx[0]])) or list(dep["path"]),
        }


def clean_real_features(days: list[str], data_dir: Path, forecaster, max_segments: int, seed: int) -> np.ndarray:
    segs = [s for d in days for s in _segments(data_dir / f"segments-{d}.parquet")]
    if max_segments and len(segs) > max_segments:
        pick = np.random.default_rng(seed).choice(len(segs), max_segments, replace=False)
        segs = [segs[i] for i in sorted(pick)]
    feats = []
    for _, category, t, xyz in segs:
        xyz = xyz.copy()
        if not np.isfinite(xyz[:, 2]).any():
            continue
        xyz[:, 2] = np.nan_to_num(xyz[:, 2], nan=float(np.nanmedian(xyz[:, 2])))
        fc = forecast_track(forecaster, xyz, t, {"A7": "rotor", "A1": "light"}.get(category))
        if len(fc.t):
            feats.append(residual_features(xyz, t, fc)[1])
    return np.concatenate(feats) if feats else np.zeros((0, 4))


def clean_sim_tolerance_data(network: Network, scenarios: list[Path], seeds: list[int], rng) -> tuple:
    X, y = [], []
    for res in run_scenarios(network, scenarios, seeds):
        for f in flights(res, network):
            xyz = add_noise(f["xyz"], rng, 5.0)
            route = route_polyline(network, f["path"])
            for i in range(10, len(xyz), 5):
                g, _ = graph_context(network, f["path"], float(xyz[i, 0]), float(xyz[i, 1]))
                X.append(tolerance_row(f["vehicle"], smoothed_speed(xyz, i), g, float(xyz[i, 2])))
                y.append(cross_track_m(float(xyz[i, 0]), float(xyz[i, 1]), route))
    return np.array(X, dtype=float), np.array(y, dtype=float)


def inject(xyz: np.ndarray, i0: int, kind: str, rng: np.random.Generator) -> np.ndarray:
    """Modify a 1 Hz track from index ``i0`` on (the labelled deviation)."""
    out = xyz.copy()
    if kind == "slow":
        for i in range(i0, len(xyz)):
            src = i0 + 0.5 * (i - i0)
            a = int(src)
            w = src - a
            out[i] = xyz[a] * (1 - w) + xyz[min(a + 1, len(xyz) - 1)] * w
        return out
    angle = math.radians(rng.uniform(25, 60) if kind == "turn" else rng.uniform(5, 10)) * rng.choice([-1, 1])
    lat0, lon0 = xyz[i0, 0], xyz[i0, 1]
    e, n = to_local(xyz[i0:, 0], xyz[i0:, 1], lat0, lon0)
    c, s = math.cos(angle), math.sin(angle)
    k = math.cos(math.radians(lat0))
    out[i0:, 0] = lat0 + np.degrees((e * s + n * c) / 6_371_008.8)
    out[i0:, 1] = lon0 + np.degrees((e * c - n * s) / (6_371_008.8 * k))
    return out


def _onsets(flags: np.ndarray) -> np.ndarray:
    return np.flatnonzero(flags & ~np.r_[False, flags[:-1]])


def score_flights(results: list[dict]) -> dict:
    out = {}
    n_dev = sum(1 for f in results if f["i0"] is not None)
    for m in METHODS:
        tp = fp = 0
        latency: list[float] = []
        by_kind: dict[str, list[int]] = {}
        for f in results:
            on = _onsets(f["flags"][m])
            if f["i0"] is None:
                fp += len(on)
                continue
            fp += int(np.sum(on < f["i0"]))
            hit = [i for i in on if f["i0"] <= i <= f["i0"] + WINDOW_S]
            if hit:
                tp += 1
                latency.append(float(hit[0] - f["i0"]))
            by_kind.setdefault(f["kind"], []).append(1 if hit else 0)
        out[m] = {
            "precision": round(tp / max(1, tp + fp), 3),
            "recall": round(tp / max(1, n_dev), 3),
            "detections": tp,
            "false_alarms": fp,
            "latency_median_s": float(np.median(latency)) if latency else None,
            "latency_p90_s": float(np.quantile(latency, 0.9)) if latency else None,
            "recall_by_kind": {k: round(float(np.mean(v)), 3) for k, v in sorted(by_kind.items())},
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--network", type=Path, default=BACKEND.parent / "airspace" / "central-fl-v1.geojson")
    ap.add_argument("--scenarios", type=Path, default=BACKEND / "scenarios")
    ap.add_argument("--scenario", type=Path, default=BACKEND / "scenarios" / "rogue_aircraft.yaml")
    ap.add_argument("--train-seeds", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--seeds", type=int, nargs="+", default=list(range(201, 221)), help="held-out rogue seeds")
    ap.add_argument("--forecaster", choices=["aerolink-gbm", "cv"], default="aerolink-gbm")
    ap.add_argument("--data", type=Path, default=DATA_DIR)
    ap.add_argument("--max-train-segments", type=int, default=400)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args(argv)
    rng = np.random.default_rng(a.seed)
    network = Network.from_geojson(a.network)
    forecaster = load_forecaster(a.forecaster, network)

    days = list_days(a.data)
    if len(days) < 2:
        print("fewer than 2 real days: run E5.1 first")
        return 1
    train_days, _ = split_days(days)
    X_real = clean_real_features(train_days, a.data, forecaster, a.max_train_segments, a.seed)
    anomaly = train_anomaly_model(X_real, a.seed)
    anomaly.manifest.update({"forecaster": a.forecaster, "train_days": train_days})
    print(f"anomaly model: {len(X_real):,} clean real samples", flush=True)
    X_tol, y_tol = clean_sim_tolerance_data(network, sorted(a.scenarios.glob("*.yaml")), a.train_seeds, rng)
    tolerance = train_tolerance_model(X_tol, y_tol, a.seed)
    tolerance.manifest.update({"sim_train_seeds": a.train_seeds})
    print(f"route tolerance: {len(y_tol):,} clean simulated samples (median {np.median(y_tol):.0f} m off route)")
    models = OffRouteModels(tolerance, anomaly)
    print(f"wrote {models.save(a.model_dir)}", flush=True)

    scenario = load_scenario(a.scenario)
    results: list[dict] = []
    for seed in a.seeds:
        for f in flights(run_simulation(network, scenario, seed=seed), network):
            t, xyz = f["t"], f["xyz"]
            kind, i0 = "none", None
            if rng.random() < 0.5:
                kind = str(rng.choice(["turn", "drift", "slow"]))
                i0 = int(rng.integers(90, len(t) - 120))
                xyz = inject(xyz, i0, kind, rng)
            xyz = add_noise(xyz, rng, 5.0)
            graphs = [graph_context(network, f["path"], float(p[0]), float(p[1]))[0] for p in xyz]
            window = None
            if hasattr(forecaster, "eta") and forecaster.eta is not None:
                eta = forecaster.predict_eta(f["path"], f["dep_t"], f["vehicle"]).etas[-1]
                window = (eta.eta_p50, eta.eta_p90)
            d = detect(
                xyz,
                t,
                route_polyline(network, f["path"]),
                graphs,
                forecaster,
                models,
                f["vehicle"],
                vehicle_cfg(f["vehicle"]).cruise_speed_mps,
                window,
            )
            flags = {
                "fixed_bounds": d.fixed,
                "tolerance": d.tolerance,
                "schedule": d.schedule,
                "band": d.band,
                "anomaly": d.anomaly,
                "ai": d.ai,
            }
            results.append({"kind": kind, "i0": i0, "flags": flags})
        print(f"  seed {seed}: {len(results)} flights so far", flush=True)

    n_dev = sum(1 for f in results if f["i0"] is not None)
    report = {
        "scenario": scenario.name,
        "held_out_seeds": a.seeds,
        "forecaster": a.forecaster,
        "flights": len(results),
        "injected_deviations": n_dev,
        "deviation_kinds": {k: sum(1 for f in results if f["kind"] == k) for k in ["turn", "drift", "slow"]},
        "detection_window_s": WINDOW_S,
        "models": {"anomaly": anomaly.manifest, "tolerance": tolerance.manifest},
        "results": score_flights(results),
    }
    print(f"\n{len(results)} flights, {n_dev} injected deviations")
    print(f"{'method':<14}{'precision':>10}{'recall':>8}{'latency':>9}{'p90':>7}   recall turn/drift/slow")
    for m, r in report["results"].items():
        k = r["recall_by_kind"]
        lat = "-" if r["latency_median_s"] is None else f"{r['latency_median_s']:.0f}s"
        p90 = "-" if r["latency_p90_s"] is None else f"{r['latency_p90_s']:.0f}s"
        print(
            f"{m:<14}{r['precision']:>10.0%}{r['recall']:>8.0%}{lat:>9}{p90:>7}   "
            f"{k.get('turn', 0):.0%}/{k.get('drift', 0):.0%}/{k.get('slow', 0):.0%}"
        )
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
