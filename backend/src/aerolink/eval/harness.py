"""Run B0, B1, and A1 evaluations across all scenarios (PLAN E3.6, E4.6)."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from aerolink.forecast.cv import CVForecaster
from aerolink.navigation.b1 import B1Advisor, B1Config, B1RouteAdvisorWrapper
from aerolink.network import Network
from aerolink.routing.a1 import A1Advisor, A1Config, A1RouteAdvisorWrapper
from aerolink.sim import load_scenario, run_simulation
from aerolink.sim.controller import ShortestPathAdvisor


@dataclass(frozen=True)
class EvalMetrics:
    scenario: str
    config: str  # "B0", "B1", "A1"
    seed: int
    duration_s: float
    trips: int = 0
    completed_trips: int = 0
    no_route_count: int = 0
    total_trip_time_s: float = 0.0
    total_pad_wait_s: float = 0.0
    bunching_events: int = 0
    loss_of_separation_events: int = 0
    off_route_events: int = 0
    medical_dispatches: int = 0
    medical_wait_s: float = 0.0
    eta_error_sum_s: float = 0.0
    eta_predictions: int = 0


def _count_events(events: list[dict], kind: str) -> int:
    return sum(1 for e in events if e["kind"] == kind)


def _trip_metrics(events: list[dict]) -> dict[str, Any]:
    """Simple trip-level metrics extracted from events."""
    trips = []
    departures: dict[str, float] = {}
    for e in events:
        aid = e.get("aircraft_id")
        if e["kind"] == "departure" and aid is not None:
            departures[aid] = e["t"]
        elif e["kind"] == "arrival" and aid in departures:
            trips.append({"aircraft_id": aid, "trip_time_s": e["t"] - departures.pop(aid)})
    return {
        "trips": len(trips),
        "completed_trips": len(trips),
        "total_trip_time_s": sum(t["trip_time_s"] for t in trips),
    }


def make_advisor(config: str, network: Network) -> ShortestPathAdvisor | B1RouteAdvisorWrapper | A1RouteAdvisorWrapper:
    if config == "B0":
        return ShortestPathAdvisor(network, k=3)
    if config == "B1":
        b1 = B1Advisor(network, B1Config(), k=3)
        return B1RouteAdvisorWrapper(b1)
    if config == "A1":
        a1 = A1Advisor(network, CVForecaster(network), cfg=A1Config())
        return A1RouteAdvisorWrapper(a1)
    raise ValueError(f"unknown config {config}")


def run_eval(network_file: Path, scenario_file: Path, config: str, output_dir: Path) -> EvalMetrics:
    network = Network.from_geojson(network_file)
    scenario = load_scenario(scenario_file)
    advisor = make_advisor(config, network)
    result = run_simulation(network, scenario, advisor=advisor)

    tm = _trip_metrics(result.events)
    pad_waits = [e for e in result.events if e["kind"] == "pad_wait"]
    eta_errors = [
        abs(e.get("eta_error_s", 0.0)) for e in result.events if e["kind"] == "arrival" and "eta_error_s" in e
    ]
    return EvalMetrics(
        scenario=scenario.name,
        config=config,
        seed=scenario.seed,
        duration_s=scenario.duration_s,
        trips=tm["trips"],
        completed_trips=tm["completed_trips"],
        no_route_count=_count_events(result.events, "route_unavailable"),
        total_trip_time_s=tm["total_trip_time_s"],
        total_pad_wait_s=sum(e.get("wait_s", 0.0) for e in pad_waits),
        bunching_events=_count_events(result.events, "bunching"),
        loss_of_separation_events=_count_events(result.events, "loss_of_separation"),
        off_route_events=_count_events(result.events, "off_route"),
        medical_dispatches=_count_events(result.events, "medical_incident"),
        medical_wait_s=sum(
            e.get("response_time_s", 0.0)
            for e in result.events
            if e["kind"] == "arrival" and e.get("purpose") == "medical_dispatch" and "response_time_s" in e
        ),
        eta_error_sum_s=sum(eta_errors),
        eta_predictions=len(eta_errors),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run B0/B1/A1 evaluation across scenarios.")
    parser.add_argument("--output-dir", "-o", type=Path, default=Path("eval-results"), help="output directory")
    parser.add_argument("--network", type=Path, default=Path("airspace/central-fl-v1.geojson"), help="network GeoJSON")
    parser.add_argument("--scenarios", type=Path, default=Path("scenarios"), help="directory with scenario YAML files")
    parser.add_argument("--configs", nargs="+", default=["B0", "B1", "A1"], help="configs to run")
    args = parser.parse_args()

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    all_metrics: list[dict] = []
    scenario_files = sorted(args.scenarios.glob("*.yaml"))
    if not scenario_files:
        raise SystemExit(f"no scenario files in {args.scenarios}")
    for scenario_file in scenario_files:
        for config in args.configs:
            metrics = run_eval(args.network, scenario_file, config, output_dir)
            all_metrics.append(asdict(metrics))
            print(
                f"{metrics.scenario:22s} {metrics.config} trips={metrics.trips} "
                f"completed={metrics.completed_trips} no_route={metrics.no_route_count}"
            )
    out = output_dir / "m4-results.json"
    out.write_text(json.dumps(all_metrics, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
