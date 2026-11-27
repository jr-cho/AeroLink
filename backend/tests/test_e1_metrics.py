"""E1 metrics (ARCHITECTURE 9.4): door-to-door, ground delay, ETA error and coverage, suggestions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aerolink.eval.harness import flight_metrics, run_eval
from aerolink.network import Network
from aerolink.sim import load_scenario, run_simulation
from aerolink.sim.scenarios import DEFAULT_NETWORK

BACKEND = Path(__file__).resolve().parents[1]


def dep(
    fid: str, t: float, request_t: float, predicted: float | None = None, window: tuple[float, float] | None = None
) -> dict:
    e = {"t": t, "kind": "departure", "flight_id": fid, "request_t": request_t, "ground_delay_s": t - request_t}
    if predicted is not None:
        assert window is not None
        e["predicted_arrival_s"] = predicted
        e["predicted_window_s"] = list(window)
    return e


def arr(fid: str, t: float, **kw) -> dict:
    return {"t": t, "kind": "arrival", "flight_id": fid, "purpose": "passenger", **kw}


def test_door_to_door_includes_the_ground_delay_that_trip_time_hides():
    ev = [dep("f1", t=100, request_t=40), arr("f1", 400), dep("f2", t=200, request_t=200), arr("f2", 500)]
    m = flight_metrics(ev)
    assert m["trip_time_mean_s"] == pytest.approx(300.0)  # both flights took 300 s in the air
    assert m["ground_delay_mean_s"] == pytest.approx(30.0)  # one waited 60 s on the ground
    assert m["door_to_door_mean_s"] == pytest.approx(330.0)


def test_eta_error_and_coverage_use_the_prediction_made_at_departure():
    ev = [
        dep("in", 0, 0, predicted=300, window=(240, 360)),
        arr("in", 330),  # 30 s late, inside the window
        dep("out", 0, 0, predicted=300, window=(240, 360)),
        arr("out", 400),  # 100 s late, outside
        dep("none", 0, 0),  # B0: predicts nothing
        arr("none", 500),
        dep("div", 0, 0, predicted=300, window=(240, 360)),
        arr("div", 900, diverted=True),  # flew to another site: the prediction no longer applies
    ]
    m = flight_metrics(ev)
    assert m["eta_flights"] == 2 and m["eta_covered"] == 1 and m["eta_coverage"] == pytest.approx(0.5)
    assert m["eta_error_mean_s"] == pytest.approx(65.0)
    assert m["trips"] == 4


def test_no_predictions_means_no_eta_metrics_not_zero():
    m = flight_metrics([dep("f", 0, 0), arr("f", 100)])
    assert m["eta_error_mean_s"] is None and m["eta_coverage"] is None


def test_suggestions_offered_accepted_and_predicted_saving():
    ev = [
        {"t": 1, "kind": "suggestion", "accepted": True, "predicted_gain_s": 40.0},
        {"t": 2, "kind": "suggestion", "accepted": False, "predicted_gain_s": 90.0},
        {"t": 3, "kind": "suggestion", "accepted": True},
    ]
    m = flight_metrics(ev)
    assert (m["suggestions_offered"], m["suggestions_accepted"]) == (3, 2)
    assert m["suggestion_predicted_saving_s"] == 40.0  # only accepted ones, only where the advisor gave a number


def test_medical_response_counts_only_dispatches():
    ev = [
        dep("m", 10, 0),
        arr("m", 200, purpose="medical_dispatch", response_time_s=200.0),
        dep("s", 10, 0),
        arr("s", 300, purpose="passenger", response_time_s=300.0),  # a scheduled leg also carries dispatch_t
    ]
    assert flight_metrics(ev)["medical_response_mean_s"] == 200.0


def test_departure_events_carry_request_time_and_the_prediction(fl_network: Network):
    from aerolink.eval.harness import make_advisor

    sc = load_scenario(BACKEND / "scenarios" / "nominal_day.yaml").with_overrides(duration_s=1800)
    res = run_simulation(fl_network, sc, advisor=make_advisor("B1", fl_network), record_truth=False)
    deps = [e for e in res.events if e["kind"] == "departure"]
    assert deps
    for d in deps:
        assert d["request_t"] <= d["t"] and d["ground_delay_s"] == pytest.approx(d["t"] - d["request_t"], abs=1e-4)
        lo, hi = d["predicted_window_s"]
        assert hi - lo == pytest.approx(120.0)  # B1's fixed +-60 s window
        assert lo < d["predicted_arrival_s"] < hi


def test_run_eval_writes_the_new_columns(tmp_path: Path):
    sc = BACKEND / "scenarios" / "nominal_day.yaml"
    m = run_eval(DEFAULT_NETWORK, sc, "B1", tmp_path)
    row = json.loads(json.dumps(m.__dict__))
    for key in (
        "door_to_door_mean_s",
        "ground_delay_mean_s",
        "eta_error_mean_s",
        "eta_coverage",
        "suggestions_offered",
    ):
        assert key in row
    assert row["door_to_door_mean_s"] >= 0


@pytest.mark.parametrize("name", ["orlando_event_egress", "i4_corridor_rush", "fdot_high_peak"])
def test_congestion_scenarios_run_clean_and_are_deterministic(fl_network: Network, name: str):
    sc = load_scenario(BACKEND / "scenarios" / f"{name}.yaml").with_overrides(duration_s=1800)
    a = run_simulation(fl_network, sc, check_invariants=True)
    b = run_simulation(fl_network, sc)
    assert a.violations == []
    assert a.digest() == b.digest()
    assert sum(e["kind"] == "departure" for e in a.events) >= 5  # the scenario really generates traffic


def test_total_pad_wait_counts_holds(tmp_path: Path):
    """``total_pad_wait_s`` sums hold_end ``held_s``; it used to read an event that is never logged."""
    from aerolink.sim.scenarios import save_scenario, scenario_from_dict

    leg = {"aircraft": "passenger", "origin": "ev-suntrax", "destination": "ev-lal"}
    sc = scenario_from_dict(
        {
            "name": "one_pad",
            "duration_s": 1200,
            "fleet": {"passenger": 3, "medical": 0},
            "fleet_staging": {"ev-suntrax": 3},
            "pad_turnaround_time_s": 60,
            "pad_capacity": {"ev-lal": 1},  # three aircraft, one pad: the last one holds
            "world": {"separation": True, "schedules": True, "energy": True, "pad_booking": True},
            "demand": {"peak_rate_per_hour": 0.0, "off_peak_rate_per_hour": 0.0, "medical_dispatch_mean_interval_s": 0},
            "schedule": [{**leg, "departure_local": f"07:01:{s:02d}"} for s in (0, 10, 20)],
        }
    )
    path = tmp_path / "one_pad.yaml"
    save_scenario(sc, path)
    m = run_eval(DEFAULT_NETWORK, path, "B0", tmp_path)
    res = run_simulation(Network.from_geojson(DEFAULT_NETWORK), sc)
    held = sum(e["held_s"] for e in res.events if e["kind"] == "hold_end")
    assert held > 0 and m.total_pad_wait_s == pytest.approx(held)


def test_cruise_only_eta_error_scores_the_eta_without_predicted_waits():
    d = dep("f", 0, 0, predicted=400, window=(340, 460))  # predicted 400 = cruise ETA 300 + 100 s of predicted waits
    d["predicted_cruise_arrival_s"] = 300
    m = flight_metrics([d, arr("f", 410)])
    assert m["eta_error_mean_s"] == pytest.approx(10.0)  # primary: against the arrival with waits
    assert m["eta_error_cruise_mean_s"] == pytest.approx(110.0)  # cruise-only: the waits show up as error
