"""Full ablations: what each AI component adds (PLAN E5.7, ARCHITECTURE §9.3).

    uv run python -m aerolink.eval.ablations                       # all configs, all scenarios
    uv run python -m aerolink.eval.ablations --update-docs         # also refresh ARCHITECTURE §9.3

Runs B0, B1, A1-A5 and "A5@50%" (A5 with only half of the en-route suggestions accepted)
across every scenario and seed, in the closed-loop simulator. Needs the trained
``aerolink-gbm`` (E5.3) and the synthetic demand history (E5.4).

Results: ``backend/eval-results/m5-ablations.json`` (every run) and
``backend/eval-results/m5-ablations.md`` (the summary table, also written into
ARCHITECTURE §9.3 with ``--update-docs``).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np

from aerolink.demand.forecaster import ChronosCounts, DemandModel, DestinationModel, tod_mean
from aerolink.demand.synthetic import DEFAULT_DIR as DEMAND_DIR
from aerolink.demand.synthetic import count_matrix, load
from aerolink.eval.harness import _count_events, make_advisor
from aerolink.forecast.cv import CVForecaster
from aerolink.forecast.gbm import GBMForecaster
from aerolink.network import Network
from aerolink.routing.aerolink import ABLATIONS, AeroLinkAdvisor, AeroLinkRouteAdvisorWrapper
from aerolink.sim import load_scenario, run_simulation

BACKEND = Path(__file__).resolve().parents[3]
DOCS = BACKEND.parent / "docs" / "ARCHITECTURE.md"
CONFIGS = ["B0", "B1", "A1", "A2", "A3", "A4", "A5", "A5@50%"]
DESCRIPTIONS = {
    "B0": "shortest path, no traffic information",
    "B1": "**classical navigation** (§6), no AI",
    "A1": "B1 + AI ETA windows (`cv` ETAs in simulation, see PLAN E4.2)",
    "A2": "B1 + AI ETA windows (`aerolink-gbm`)",
    "A3": "A2 + demand forecaster",
    "A4": "A3 + AI off-route detection",
    "A5": "**full AeroLink** (A4 + risk ranking + en-route suggestions)",
    "A5@50%": "A5, half of the suggestions accepted (sensitivity)",
}
START, END = "<!-- ablation-table:start -->", "<!-- ablation-table:end -->"


def build_demand(network: Network, scenario_start: str, use_chronos: bool) -> DemandModel:
    counts, trips = load(DEMAND_DIR)
    histories = {}
    for kind in ["passenger", "medical"]:
        _, sites, m = count_matrix(counts, kind)
        histories[kind] = (sites, m)
    t = trips.to_pydict()
    dest = DestinationModel(histories["passenger"][0]).fit(
        t["origin"], np.array(t["t_of_day_s"]) / 3600.0, np.array(t["weekday"], dtype=float), t["destination"]
    )
    forecaster = tod_mean
    if use_chronos:
        try:
            chronos = ChronosCounts()
            chronos._load()
            forecaster = chronos
        except Exception as e:  # noqa: BLE001
            print(f"Chronos-2 unavailable ({e.__class__.__name__}); demand uses the time-of-day mean")
    weekday = datetime.fromisoformat(scenario_start).weekday()
    return DemandModel.from_history(histories, dest, weekday, forecaster)


def advisor_for(config: str, network: Network, gbm: GBMForecaster, demand: DemandModel):
    if config in ("B0", "B1"):
        return make_advisor(config, network)
    name = config.split("@")[0]
    forecaster = CVForecaster(network) if name == "A1" else gbm
    return AeroLinkRouteAdvisorWrapper(AeroLinkAdvisor(network, forecaster, ABLATIONS[name], demand=demand))


def run_one(network: Network, scenario_file: Path, config: str, seed: int, gbm, demand) -> dict:
    scenario = load_scenario(scenario_file)
    if config.endswith("@50%"):
        scenario = replace(scenario, suggestion_acceptance_rate=0.5)
    result = run_simulation(network, scenario, seed=seed, advisor=advisor_for(config, network, gbm, demand))
    ev = result.events
    deps = {e["flight_id"]: e for e in ev if e["kind"] == "departure"}
    trips = [e["t"] - deps[e["flight_id"]]["t"] for e in ev if e["kind"] == "arrival" and e.get("flight_id") in deps]
    medical = [e["response_time_s"] for e in ev if e["kind"] == "arrival" and "response_time_s" in e]
    holding_s = sum(scenario.dt_s for r in result.truth if r["status"] == "holding")
    return {
        "scenario": scenario.name,
        "config": config,
        "seed": seed,
        "trips": len(trips),
        "trip_time_mean_s": float(np.mean(trips)) if trips else None,
        "trip_time_p95_s": float(np.quantile(trips, 0.95)) if trips else None,
        "holding_s_total": holding_s,
        "holding_s_per_trip": holding_s / max(1, len(trips)),
        "no_route": _count_events(ev, "route_unavailable"),
        "suggestions_accepted": sum(1 for e in ev if e["kind"] == "suggestion" and e.get("accepted")),
        "bunching_events": _count_events(ev, "bunching"),
        "loss_of_separation": _count_events(ev, "loss_of_separation"),
        "medical_response_mean_s": float(np.mean(medical)) if medical else None,
    }


def summarize(rows: list[dict]) -> list[dict]:
    out = []
    for c in CONFIGS:
        rs = [r for r in rows if r["config"] == c]
        if not rs:
            continue
        trips = sum(r["trips"] for r in rs)

        def wmean(key: str, rs=rs) -> float | None:
            vals = [(r[key], r["trips"]) for r in rs if r[key] is not None and r["trips"]]
            return sum(v * w for v, w in vals) / sum(w for _, w in vals) if vals else None

        med = [r["medical_response_mean_s"] for r in rs if r["medical_response_mean_s"] is not None]
        out.append(
            {
                "config": c,
                "runs": len(rs),
                "trips": trips,
                "trip_time_mean_s": wmean("trip_time_mean_s"),
                "trip_time_p95_s": wmean("trip_time_p95_s"),
                "holding_s_per_trip": sum(r["holding_s_total"] for r in rs) / max(1, trips),
                "no_route": sum(r["no_route"] for r in rs),
                "suggestions_accepted": sum(r["suggestions_accepted"] for r in rs),
                "bunching_events": sum(r["bunching_events"] for r in rs),
                "loss_of_separation": sum(r["loss_of_separation"] for r in rs),
                "medical_response_mean_s": float(np.mean(med)) if med else None,
            }
        )
    return out


def markdown(summary: list[dict], seeds: list[int] | None, n_scenarios: int) -> str:
    def f(v, d=0):
        return "–" if v is None else f"{v:,.{d}f}"

    seed_text = "each scenario's own seed" if not seeds else f"seeds {', '.join(map(str, seeds))}"
    lines = [
        f"Measured over {n_scenarios} scenarios, {seed_text} (`python -m aerolink.eval.ablations`, "
        "`backend/eval-results/m5-ablations.json`). Times in seconds; trips = completed flights.",
        "",
        "| # | Configuration | Trips | Mean trip | p95 trip | Holding / trip | No route | Suggestions | "
        "Bunching | Medical response |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summary:
        lines.append(
            f"| {s['config']} | {DESCRIPTIONS[s['config']]} | {s['trips']} | {f(s['trip_time_mean_s'])} | "
            f"{f(s['trip_time_p95_s'])} | {f(s['holding_s_per_trip'], 1)} | {s['no_route']} | "
            f"{s['suggestions_accepted']} | {s['bunching_events']} | {f(s['medical_response_mean_s'])} |"
        )
    b1 = next((s for s in summary if s["config"] == "B1"), None)
    a5 = next((s for s in summary if s["config"] == "A5"), None)
    if b1 and a5 and b1["trip_time_mean_s"] and a5["trip_time_mean_s"]:
        d = a5["trip_time_mean_s"] - b1["trip_time_mean_s"]
        h = a5["holding_s_per_trip"] - b1["holding_s_per_trip"]
        lines += [
            "",
            f"**B1 vs A5 (as measured):** mean trip time {d:+,.0f} s ({d / b1['trip_time_mean_s']:+.1%}), "
            f"holding per trip {h:+,.1f} s, bunching events {a5['bunching_events'] - b1['bunching_events']:+d}.",
        ]
    return "\n".join(lines)


def update_docs(table: str, path: Path = DOCS) -> bool:
    text = path.read_text()
    if START not in text or END not in text:
        return False
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    path.write_text(f"{head}{START}\n{table}\n{END}{tail}")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--network", type=Path, default=BACKEND.parent / "airspace" / "central-fl-v1.geojson")
    ap.add_argument("--scenarios", type=Path, default=BACKEND / "scenarios")
    ap.add_argument("--configs", nargs="+", default=CONFIGS)
    ap.add_argument("--seeds", type=int, nargs="*", default=[], help="default: each scenario's own seed")
    ap.add_argument("--no-chronos", action="store_true", help="demand counts from the time-of-day mean")
    ap.add_argument("--out", type=Path, default=BACKEND / "eval-results" / "m5-ablations.json")
    ap.add_argument("--update-docs", action="store_true")
    a = ap.parse_args(argv)

    network = Network.from_geojson(a.network)
    gbm = GBMForecaster.load(network)
    files = sorted(a.scenarios.glob("*.yaml"))
    demand = build_demand(network, load_scenario(files[0]).start_time, not a.no_chronos)
    rows = []
    for f in files:
        seeds = a.seeds or [load_scenario(f).seed]
        for seed in seeds:
            for c in a.configs:
                r = run_one(network, f, c, seed, gbm, demand)
                rows.append(r)
                print(
                    f"{r['scenario']:<22}{c:<8} seed {seed:<4} trips {r['trips']:<4} "
                    f"mean {r['trip_time_mean_s'] or 0:>7.0f}s holding/trip {r['holding_s_per_trip']:>6.1f}s",
                    flush=True,
                )
    summary = summarize(rows)
    table = markdown(summary, a.seeds, len(files))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({"summary": summary, "runs": rows}, indent=2) + "\n")
    a.out.with_suffix(".md").write_text(table + "\n")
    print("\n" + table)
    print(f"\nwrote {a.out} and {a.out.with_suffix('.md')}")
    if a.update_docs:
        print("updated ARCHITECTURE §9.3" if update_docs(table) else f"markers {START} / {END} not found in {DOCS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
