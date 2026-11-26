"""Train, calibrate and evaluate ``aerolink-gbm`` (PLAN E5.3).

    uv run python -m aerolink.forecast.train_gbm                 # simulated + real data
    uv run python -m aerolink.forecast.train_gbm --with-chronos  # also score Chronos-2
    uv run python -m aerolink.forecast.train_gbm --no-real       # simulated data only

Data:
* simulated: every scenario in ``backend/scenarios`` with training seeds (default 1-6)
  and held-out seeds (101, 102) that are never trained on;
* real: the E5.1 adsb.lol days, split by day exactly as in E5.2 (latest ~30% held out).

Training samples are split again by flight: 80% fit the quantile models, 20% calibrate
them (MAPIE). Results go to ``backend/eval-results/aerolink-gbm-e5.3.json`` and the model to
``backend/models/aerolink-gbm/`` with a manifest of its seeds and data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from aerolink.forecast.cv import CVForecaster
from aerolink.forecast.features import POSITION_FEATURES, position_row, position_targets
from aerolink.forecast.gbm import AXES, COVERAGE_TARGET, MODEL_DIR, GBMForecaster, fit_calibrated, predict_q
from aerolink.forecast.simdata import EtaSamples, eta_samples, position_samples, run_scenarios
from aerolink.network import Network
from aerolink.realdata.benchmark import predict_all, score
from aerolink.realdata.samples import DATA_DIR, PositionSamples, build_position_samples, list_days, split_days

BACKEND = Path(__file__).resolve().parents[3]
DEFAULT_OUT = BACKEND / "eval-results" / "aerolink-gbm-e5.3.json"
ETA_BUCKETS_MIN = [5, 10, 20, 30, 45]


def _group_split(groups: np.ndarray, frac: float, seed: int) -> np.ndarray:
    """Boolean mask: True for the ``frac`` share of groups (flights) picked at random."""
    uniq = np.unique(groups)
    rng = np.random.default_rng(seed)
    picked = set(rng.choice(uniq, max(1, round(len(uniq) * frac)), replace=False)) if len(uniq) > 1 else set()
    return np.array([g in picked for g in groups], dtype=bool)


def position_xy(
    samples: PositionSamples, graphs: list[dict[str, float]] | None, rng: np.random.Generator, mask_hour: float = 0.0
) -> dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Per horizon: (X, Y[along, cross, up], group) for samples whose future is known."""
    out = {}
    for j, h in enumerate(samples.horizons_s):
        X, Y, G = [], [], []
        for i in range(len(samples)):
            if np.isnan(samples.future[i, j, 0]):
                continue
            hour = None if rng.random() < mask_hour else float(samples.hour[i])
            X.append(
                position_row(samples.history[i], h, samples.vehicle[i] or None, hour, graphs[i] if graphs else None)
            )
            Y.append(position_targets(samples.history[i], samples.future[i, j], h))
            G.append(samples.segment_id[i])
        out[h] = (
            np.array(X, dtype=float).reshape(-1, len(POSITION_FEATURES)),
            np.array(Y, dtype=float).reshape(-1, 3),
            np.array(G, dtype=str),
        )
    return out


def _merge(parts: list[dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]]]):
    keys = parts[0].keys()
    return {h: tuple(np.concatenate([p[h][k] for p in parts]) for k in range(3)) for h in keys}


def frame_coverage(models, data) -> list[dict]:
    """p10-p90 coverage in the calibrated frame (along / cross / up), per horizon."""
    rows = []
    for h, (X, Y, _) in data.items():
        if not len(X):
            continue
        row: dict = {"horizon_s": h, "n": len(X)}
        for k, axis in enumerate(AXES):
            q = predict_q(models[(h, axis)], X)
            row[f"coverage_{axis}"] = round(float(((Y[:, k] >= q[:, 0]) & (Y[:, k] <= q[:, 2])).mean()), 3)
        rows.append(row)
    return rows


def eta_report(model, data: EtaSamples) -> list[dict]:
    """ETA error and coverage per horizon bucket (CV remaining time), CV vs aerolink-gbm."""
    q = np.exp(predict_q(model, data.X)) * data.cv_remaining_s[:, None]
    cv_half = np.maximum(30.0, 0.08 * data.cv_remaining_s)
    rows = []
    lo = 0.0
    for b in ETA_BUCKETS_MIN:
        m = (data.cv_remaining_s > lo * 60) & (data.cv_remaining_s <= b * 60)
        lo = b
        if not m.any():
            rows.append({"horizon_max_min": b, "n": 0})
            continue
        act = data.actual_remaining_s[m]
        rows.append(
            {
                "horizon_max_min": b,
                "n": int(m.sum()),
                "cv_mae_s": round(float(np.abs(act - data.cv_remaining_s[m]).mean()), 1),
                "gbm_mae_s": round(float(np.abs(act - q[m, 1]).mean()), 1),
                "cv_coverage": round(float((np.abs(act - data.cv_remaining_s[m]) <= cv_half[m]).mean()), 3),
                "gbm_coverage": round(float(((act >= q[m, 0]) & (act <= q[m, 2])).mean()), 3),
            }
        )
    return rows


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--network", type=Path, default=BACKEND.parent / "airspace" / "central-fl-v1.geojson")
    ap.add_argument("--scenarios", type=Path, default=BACKEND / "scenarios")
    ap.add_argument("--train-seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5, 6])
    ap.add_argument("--test-seeds", type=int, nargs="+", default=[101, 102])
    ap.add_argument("--data", type=Path, default=DATA_DIR)
    ap.add_argument("--no-real", action="store_true", help="train on simulated data only")
    ap.add_argument("--with-chronos", action="store_true", help="also score Chronos-2 (downloads weights once)")
    ap.add_argument("--seed", type=int, default=42, help="LightGBM and split seed")
    ap.add_argument("--max-eval-samples", type=int, default=3000)
    ap.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args(argv)
    rng = np.random.default_rng(a.seed)
    started = time.monotonic()

    network = Network.from_geojson(a.network)
    scenario_files = sorted(a.scenarios.glob("*.yaml"))
    print(
        f"simulating {len(scenario_files)} scenarios x {len(a.train_seeds) + len(a.test_seeds)} seeds ...", flush=True
    )
    sim_train = run_scenarios(network, scenario_files, a.train_seeds)
    sim_test = run_scenarios(network, scenario_files, a.test_seeds)
    sim_pos_train, sim_graph_train = position_samples(sim_train, network)
    sim_pos_test, sim_graph_test = position_samples(sim_test, network)
    eta_train = eta_samples(sim_train, network)
    eta_test = eta_samples(sim_test, network)
    print(f"simulated: {len(sim_pos_train):,} position / {len(eta_train.y_log_ratio):,} ETA training samples")

    real_train = real_test = None
    days: dict[str, list[str]] = {"train": [], "held_out": []}
    if not a.no_real:
        all_days = list_days(a.data)
        if len(all_days) < 2:
            print("fewer than 2 real days found: run E5.1 first, or pass --no-real")
            return 1
        days["train"], days["held_out"] = split_days(all_days)
        real_train = build_position_samples(days["train"], a.data)
        real_test = build_position_samples(days["held_out"], a.data)
        print(f"real: {len(real_train):,} training samples ({len(days['train'])} days)")

    # --- position head ---
    parts = [position_xy(sim_pos_train, sim_graph_train, rng, mask_hour=0.1)]
    if real_train is not None:
        parts.append(position_xy(real_train, None, rng))
    pos = _merge(parts)
    pos_models = {}
    pos_cal = {}
    for h, (X, Y, G) in pos.items():
        cal = _group_split(G, 0.2, a.seed)
        pos_cal[h] = (X[cal], Y[cal], G[cal])
        for k, axis in enumerate(AXES):
            pos_models[(h, axis)] = fit_calibrated(X[~cal], Y[~cal, k], X[cal], Y[cal, k], a.seed)
        print(f"  position head {h:>3}s: {int((~cal).sum()):,} fit / {int(cal.sum()):,} calibration", flush=True)

    # --- ETA head ---
    hour_col = 1  # ETA_FEATURES index of "hour"; hidden on 10% of rows so missing hours are learnt
    Xe = eta_train.X.copy()
    Xe[rng.random(len(Xe)) < 0.1, hour_col] = np.nan
    Xe[rng.random(len(Xe)) < 0.1, -1] = np.nan  # traffic_ahead is not always known either
    cal = _group_split(eta_train.group, 0.2, a.seed)
    eta_model = fit_calibrated(Xe[~cal], eta_train.y_log_ratio[~cal], Xe[cal], eta_train.y_log_ratio[cal], a.seed)

    a.model_dir.mkdir(parents=True, exist_ok=True)
    model_path = a.model_dir / "model.joblib"
    manifest = {
        "model": "aerolink-gbm",
        "seed": a.seed,
        "sim_train_seeds": a.train_seeds,
        "sim_test_seeds": a.test_seeds,
        "scenarios": [f.stem for f in scenario_files],
        "real_days": days,
        "coverage_target": COVERAGE_TARGET,
        "samples": {
            "position_fit_and_calibration": {str(h): int(len(v[0])) for h, v in pos.items()},
            "eta": int(len(eta_train.y_log_ratio)),
        },
        "libraries": {p: version(p) for p in ["lightgbm", "mapie", "scikit-learn", "numpy"]},
    }
    joblib.dump({"position": pos_models, "eta": eta_model, "manifest": manifest}, model_path, compress=3)
    manifest["sha256"] = _sha256(model_path)
    (a.model_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {model_path} ({model_path.stat().st_size / 1e6:.1f} MB)")

    # --- evaluation on held-out data ---
    gbm = GBMForecaster.load(network, a.model_dir)
    forecasters = {"cv": CVForecaster(network), "aerolink-gbm": gbm}
    if a.with_chronos:
        from aerolink.forecast.chronos import ChronosForecaster

        forecasters["chronos"] = ChronosForecaster(network)

    def compare(samples: PositionSamples, graphs) -> dict:
        if a.max_eval_samples and len(samples) > a.max_eval_samples:
            keep = np.zeros(len(samples), dtype=bool)
            keep[np.random.default_rng(a.seed).choice(len(samples), a.max_eval_samples, replace=False)] = True
            samples = samples.subset(keep)
            graphs = [g for g, k in zip(graphs, keep, strict=True) if k] if graphs else None
        res = {}
        for name, f in forecasters.items():
            if isinstance(f, GBMForecaster):
                preds = f.predict_position_batch(
                    list(samples.history), samples.horizons_s, list(samples.vehicle), list(samples.hour), graphs
                )
            else:
                preds = predict_all(f, samples)
            res[name] = score(samples, preds)
        return res

    report: dict[str, Any] = {
        "manifest": manifest,
        "position_sim_held_out": compare(sim_pos_test, sim_graph_test),
        "position_real_held_out": compare(real_test, None) if real_test is not None and len(real_test) else None,
        "position_calibration_coverage_sim_held_out": frame_coverage(
            pos_models, position_xy(sim_pos_test, sim_graph_test, rng)
        ),
        "position_calibration_coverage_real_held_out": (
            frame_coverage(pos_models, position_xy(real_test, None, rng)) if real_test is not None else None
        ),
        "eta_sim_held_out": eta_report(eta_model, eta_test),
        "minutes": round((time.monotonic() - started) / 60, 1),
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=2) + "\n")

    print("\nPosition head, horizontal error at each horizon (mean, m) and p10-p90 coverage in the model's frame:")
    for label, key, cov_key in [
        ("simulated", "position_sim_held_out", "position_calibration_coverage_sim_held_out"),
        ("real", "position_real_held_out", "position_calibration_coverage_real_held_out"),
    ]:
        if not report[key]:
            continue
        cov = {r["horizon_s"]: r for r in report[cov_key]}
        for h_i, h in enumerate(sim_pos_test.horizons_s):
            errs = "  ".join(
                f"{n}={rows[h_i]['fde_mean_m']:.0f}" for n, rows in report[key].items() if rows[h_i].get("n")
            )
            c = cov.get(h, {})
            cv_txt = "/".join(f"{c.get('coverage_' + ax, 0):.0%}" for ax in AXES)
            print(f"  {label:<9} {h:>3}s  {errs}   coverage along/cross/up {cv_txt}")
    print("\nETA head (simulated held-out): mean absolute error and p10-p90 coverage")
    for r in report["eta_sim_held_out"]:
        if r["n"]:
            print(
                f"  <= {r['horizon_max_min']:>2} min  n={r['n']:<5} cv {r['cv_mae_s']:>6.0f}s ({r['cv_coverage']:.0%})"
                f"   gbm {r['gbm_mae_s']:>6.0f}s ({r['gbm_coverage']:.0%})"
            )
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())