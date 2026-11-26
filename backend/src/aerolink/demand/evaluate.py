"""Demand forecaster accuracy on SYNTHETIC data (PLAN E5.4).

    uv run python -m aerolink.demand.evaluate               # Chronos-2 + baselines
    uv run python -m aerolink.demand.evaluate --no-chronos  # baselines only (no download)

The last ``--test-days`` days of the synthetic history are held out. Each held-out day is
forecast one day ahead from everything before it (96 bins of 15 min per site). Reported per
forecaster and kind: mean absolute error per site-bin, daily total bias, and p10-p90
coverage; for medical also the rate calibration (predicted / observed dispatches).
Destinations: LightGBM vs a uniform choice, on held-out trips (top-1 accuracy, log loss).

Results: ``backend/eval-results/demand-e5.4.json`` (labelled synthetic).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from aerolink.demand.forecaster import COUNT_FORECASTERS, ChronosCounts, DestinationModel
from aerolink.demand.synthetic import (
    BINS_PER_DAY,
    DEFAULT_DIR,
    LABEL,
    NETWORK,
    SyntheticDemandConfig,
    count_matrix,
    generate,
    load,
    write,
)
from aerolink.network import Network

BACKEND = Path(__file__).resolve().parents[3]
DEFAULT_OUT = BACKEND / "eval-results" / "demand-e5.4.json"


def evaluate_counts(m: np.ndarray, test_days: int, forecasters: dict) -> dict:
    n_days = m.shape[1] // BINS_PER_DAY
    res = {}
    for name, f in forecasters.items():
        err, bias, cov, pred_tot, obs_tot = [], [], [], 0.0, 0.0
        for d in range(n_days - test_days, n_days):
            hist = m[:, : d * BINS_PER_DAY]
            truth = m[:, d * BINS_PER_DAY : (d + 1) * BINS_PER_DAY]
            q = f(hist, BINS_PER_DAY)
            err.append(np.abs(q[..., 1] - truth).mean())
            bias.append(float(q[..., 1].sum() - truth.sum()))
            cov.append(((truth >= np.floor(q[..., 0])) & (truth <= np.ceil(q[..., 2]))).mean())
            pred_tot += float(q[..., 1].sum())
            obs_tot += float(truth.sum())
        res[name] = {
            "mae_per_site_bin": round(float(np.mean(err)), 4),
            "daily_total_bias": round(float(np.mean(bias)), 2),
            "coverage_p10_p90": round(float(np.mean(cov)), 3),
            "rate_calibration": round(pred_tot / max(1e-9, obs_tot), 3),
        }
    return res


def evaluate_destinations(trips: dict, sites: list[str], test_days: list[str], seed: int) -> dict:
    test = np.array([d in set(test_days) for d in trips["day"]])
    origins = np.array(trips["origin"])
    dests = np.array(trips["destination"])
    hours = np.array(trips["t_of_day_s"]) / 3600.0
    weekdays = np.array(trips["weekday"], dtype=float)
    model = DestinationModel(sites, seed).fit(list(origins[~test]), hours[~test], weekdays[~test], list(dests[~test]))
    truth = np.array([model.index[d] for d in dests[test]])
    out = {"held_out_trips": int(test.sum())}
    for name, p in [
        ("lightgbm", model.proba(list(origins[test]), hours[test], weekdays[test])),
        ("uniform", model.uniform(list(origins[test]))),
    ]:
        pt = p[np.arange(len(truth)), truth]
        out[name] = {
            "top1_accuracy": round(float((p.argmax(axis=1) == truth).mean()), 3),
            "log_loss": round(float(-np.log(np.maximum(pt, 1e-12)).mean()), 3),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--test-days", type=int, default=7)
    ap.add_argument("--no-chronos", action="store_true")
    ap.add_argument("--chronos-model", default="autogluon/chronos-2-small")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args(argv)

    if not (a.data / "counts.parquet").exists():
        print(f"no synthetic history in {a.data}: generating it")
        write(a.data, *generate(Network.from_geojson(NETWORK), SyntheticDemandConfig()))
    counts, trips_table = load(a.data)

    forecasters = dict(COUNT_FORECASTERS)
    if not a.no_chronos:
        forecasters["chronos"] = ChronosCounts(a.chronos_model)

    report: dict = {"label": LABEL, "test_days": a.test_days, "counts": {}}
    for kind in ["passenger", "medical"]:
        days, sites, m = count_matrix(counts, kind)
        print(f"{kind}: {len(sites)} sites, {len(days)} days, {int(m.sum()):,} flights", flush=True)
        report["counts"][kind] = evaluate_counts(m, a.test_days, forecasters)
        if kind == "passenger":
            report["destinations"] = evaluate_destinations(trips_table.to_pydict(), sites, days[-a.test_days :], a.seed)

    for kind, res in report["counts"].items():
        print(f"\n{kind} (one day ahead, {a.test_days} held-out days, SYNTHETIC)")
        print(f"  {'forecaster':<15}{'MAE/bin':>9}{'bias/day':>10}{'cover':>8}{'calib':>8}")
        for name, r in sorted(res.items(), key=lambda kv: kv[1]["mae_per_site_bin"]):
            print(
                f"  {name:<15}{r['mae_per_site_bin']:>9.3f}{r['daily_total_bias']:>10.1f}"
                f"{r['coverage_p10_p90']:>8.0%}{r['rate_calibration']:>8.2f}"
            )
    d = report["destinations"]
    print(f"\ndestinations ({d['held_out_trips']} held-out trips):")
    for name in ["lightgbm", "uniform"]:
        print(f"  {name:<10} top-1 {d[name]['top1_accuracy']:.0%}   log loss {d[name]['log_loss']:.3f}")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
