"""Run B0, B1, and A1 evaluations across all scenarios (PLAN E3.6, E4.6)."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

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
    door_to_door_mean_s: float | None = None
    door_to_door_p95_s: float | None = None
    ground_delay_mean_s: float | None = None
    ground_delay_p95_s: float | None = None
    eta_error_mean_s: float | None = None
    eta_error_p90_s: float | None = None
    eta_error_cruise_mean_s: float | None = None
    eta_error_cruise_p90_s: float | None = None
    eta_coverage: float | None = None
    suggestions_offered: int = 0
    suggestions_accepted: int = 0
    suggestion_predicted_saving_s: float = 0.0
    schedule_conflicts: int = 0


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


def _mean(xs: list[float]) -> float | None:
    return float(np.mean(xs)) if xs else None


def _quantile(xs: list[float], q: float) -> float | None:
    return float(np.quantile(xs, q)) if xs else None


def flight_metrics(events: list[dict]) -> dict[str, Any]:
    """Per-flight metrics (ARCHITECTURE section 9.4) from the event log, matched on flight id.

    * ``trip_time``: departure to arrival (the flight).
    * ``door_to_door``: trip request to arrival. A pad slot that is not free turns into a ground
      hold before departure, which ``trip_time`` does not see; ``ground_delay`` (departure minus
      request) and ``door_to_door`` do. Passenger: the trip became due; medical dispatch and
      scheduled legs: the dispatch / schedule time.
    * ``eta_error_cruise``: the same against the ETA alone (cruise time, no predicted waits).
    * ``eta_error``: |actual arrival - predicted arrival| at departure, for flights that landed at
      the planned destination. The prediction is the advisor's ETA plus the waits it predicts;
      ``coverage`` is the share that landed inside its predicted window (B1: fixed +-60 s; A: p10-p90).
      B0 predicts nothing and has no ETA metrics.
    * suggestions: offered / accepted, and the advisor's own predicted saving of the accepted ones
      (not a measured counterfactual).
    * ``medical_response``: dispatch to landing of medical-dispatch flights only.
    """
    deps = {e["flight_id"]: e for e in events if e["kind"] == "departure" and "flight_id" in e}
    trip: list[float] = []
    d2d: list[float] = []
    delay: list[float] = []
    err: list[float] = []
    err_cruise: list[float] = []
    medical: list[float] = []
    covered = windowed = 0
    for e in events:
        if e["kind"] != "arrival" or e.get("flight_id") not in deps:
            continue
        dep = deps[e["flight_id"]]
        trip.append(e["t"] - dep["t"])
        request_t = dep.get("request_t", dep["t"])
        d2d.append(e["t"] - request_t)
        delay.append(dep["t"] - request_t)
        if e.get("purpose") == "medical_dispatch" and "response_time_s" in e:
            medical.append(e["response_time_s"])
        if "predicted_arrival_s" in dep and not e.get("diverted") and not e.get("destination_changed"):
            err.append(abs(e["t"] - dep["predicted_arrival_s"]))
            err_cruise.append(abs(e["t"] - dep.get("predicted_cruise_arrival_s", dep["predicted_arrival_s"])))
            lo, hi = dep["predicted_window_s"]
            windowed += 1
            covered += lo <= e["t"] <= hi
    offered = [e for e in events if e["kind"] == "suggestion"]
    accepted = [e for e in offered if e.get("accepted")]
    gains = [e["predicted_gain_s"] for e in accepted if "predicted_gain_s" in e]
    return {
        "trips": len(trip),
        "trip_time_mean_s": _mean(trip),
        "trip_time_p95_s": _quantile(trip, 0.95),
        "door_to_door_mean_s": _mean(d2d),
        "door_to_door_p95_s": _quantile(d2d, 0.95),
        "ground_delay_mean_s": _mean(delay),
        "ground_delay_p95_s": _quantile(delay, 0.95),
        "eta_error_mean_s": _mean(err),
        "eta_error_p90_s": _quantile(err, 0.90),
        "eta_error_cruise_mean_s": _mean(err_cruise),
        "eta_error_cruise_p90_s": _quantile(err_cruise, 0.90),
        "eta_flights": windowed,
        "eta_covered": covered,
        "eta_coverage": covered / windowed if windowed else None,
        "suggestions_offered": len(offered),
        "suggestions_accepted": len(accepted),
        "suggestion_predicted_saving_s": float(sum(gains)),
        "medical_response_mean_s": _mean(medical),
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
    fm = flight_metrics(result.events)
    # time spent holding for a pad: the simulator logs it as hold_end (held_s); a "pad_wait" event never existed,
    # so this column was always 0
    pad_waits = [e for e in result.events if e["kind"] == "hold_end"]
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
        total_pad_wait_s=sum(e.get("held_s", 0.0) for e in pad_waits),
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
        door_to_door_mean_s=fm["door_to_door_mean_s"],
        door_to_door_p95_s=fm["door_to_door_p95_s"],
        ground_delay_mean_s=fm["ground_delay_mean_s"],
        ground_delay_p95_s=fm["ground_delay_p95_s"],
        eta_error_mean_s=fm["eta_error_mean_s"],
        eta_error_p90_s=fm["eta_error_p90_s"],
        eta_error_cruise_mean_s=fm["eta_error_cruise_mean_s"],
        eta_error_cruise_p90_s=fm["eta_error_cruise_p90_s"],
        eta_coverage=fm["eta_coverage"],
        suggestions_offered=fm["suggestions_offered"],
        suggestions_accepted=fm["suggestions_accepted"],
        suggestion_predicted_saving_s=fm["suggestion_predicted_saving_s"],
        schedule_conflicts=_count_events(result.events, "schedule_conflict"),
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
