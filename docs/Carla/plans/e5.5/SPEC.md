# E5.5 Off-Route Detection: Spec

Source: `docs/Carla/M5_GUIDE.md` Part 9, `docs/PLAN.md` E5.5, ARCHITECTURE §7.4.

## Goal

Detect aircraft leaving their predicted path using learned tolerances instead of the classical fixed 300 m bound, then react: mark the flight `off_route`, recalculate the options, and spread the aircraft's possible positions into the traffic forecast.

## Stories (PLAN.md, as amended by the guide)

- Flag an aircraft when it is outside its calibrated band for 3 or more consecutive checks (p01–p99, one check every 10 s), outside a learned route tolerance, or unable to arrive inside its ETA window.
- Train a scikit-learn `IsolationForest` on residual features from clean real-data legs.
- Set the flight to `off_route` and send fresh route options from the current position ("recalculating").
- Spread the flagged aircraft's possible positions into the traffic forecast.

## Detection rule

Every check runs on a 1 Hz track, is evaluated every `CHECK_EVERY_S = 10` s, and needs `CONSECUTIVE = 3` hits. Hysteresis clears a flag after 2 consecutive misses.

| Check | Definition |
|---|---|
| `fixed` (baseline B1) | cross-track > 300 m, flagged immediately with no consecutive rule |
| `tolerance` | cross-track > LightGBM p99 tolerance (min 30 m) from features `[vehicle, speed_mps (10 s smoothed), dist_next_node_m, turn_at_next_deg, alt_m]`, trained on clean simulated flights |
| `schedule` | `t + remaining_route_m / cruise > p90 + (p90 − p50) × 1.81`, using the ETA head's arrival window predicted at departure |
| `band` | the fix is outside the 30 s position forecast made 30 s earlier, with the p10–p90 box widened to p01–p99 (×2.326/1.2816) |
| `anomaly` | `IsolationForest.decision_function < threshold`, where the threshold is the 1st percentile on clean real residuals. Features: `[abs_cross_err_m, along_err_m, speed_change_mps, vrate_dev_mps]` |

**AeroLink rule:** `ai = tolerance | schedule | (band & anomaly)`.

Rationale, which goes into ARCHITECTURE §7.4 in E5.7: a band check every second at p05–p95 gave about 23 false alarms per flight-hour. Checking every 10 s at p01–p99 gives about 1.

## Modules

### New: `offroute/detector.py`

- Constants: `LEAD_S=30`, `BAND_SCALE`, `CHECK_EVERY_S=10`, `CONSECUTIVE=3`, `FIXED_BOUND_M=300`, `MODEL_DIR=backend/models/offroute`.
- Geometry: `route_polyline(network, path)` (follows each edge's oriented geometry), `cross_track_m(lat, lon, route)`, `smoothed_speed`.
- Timing: `consecutive(flags, n=3, clear=2)`, `checked(n, idx, hits, t)`.
- Forecast residuals:
  - `TrackForecasts`
  - `forecast_track(forecaster, xyz, t, vehicle, graphs)`: batched, and passes vehicles and graphs to `aerolink-gbm`.
  - `outside_band`, `residual_features`
- Models:
  - `AnomalyModel`, `train_anomaly_model(X, seed, clean_quantile=0.01)`
  - `ToleranceModel`, `tolerance_row`, `train_tolerance_model(X, cross_m, seed)`
  - `OffRouteModels(tolerance, anomaly)` with `save`/`load` to `offroute.joblib`
- `Detection` dataclass (fixed, tolerance, schedule, band, anomaly; `.ai`).
- `detect(xyz, t, route, graphs, forecaster, models, vehicle, cruise_mps, arrival_window=None)`.
- `reachable_mass(network, lat, lon, speed_mps, t_s, horizon_s=600)`:
  - Assigns equal probability to every node reachable in a straight line within the horizon, at max(10, speed). The total is 1.0.
  - ETAs are (0.8·eta, eta, 1.3·eta + 30).

### New: `offroute/evaluate.py` (CLI)

```
uv run python -m aerolink.offroute.evaluate [--forecaster aerolink-gbm|cv] [--network F]
    [--scenarios DIR] [--scenario rogue_aircraft.yaml] [--train-seeds 1 2 3]
    [--seeds 201..220] [--data DIR] [--max-train-segments 400] [--seed 42]
    [--model-dir DIR] [--out F]
```

1. **Train.**
   - The anomaly model trains on residuals from up to 400 clean real segments from the E5.2 training days.
   - The tolerance model trains on clean simulated flights (all scenarios, training seeds, 5 m noise, every 5 s).
   - Both are saved to `models/offroute/offroute.joblib`.
2. **Ground truth.**
   - Run `rogue_aircraft` with the held-out seeds 201–220 and take every completed AeroLink flight with at least 240 s airborne.
   - With p = 0.5, inject a deviation at a random index i0 in [90, len − 120]:
     - `turn`: 25–60°
     - `drift`: 5–10°
     - `slow`: half speed
   - Add 5 m of noise.
3. **Score.**
   - A detection is the first flag onset within 300 s after i0. Every other onset is a false alarm.
   - Metrics per method: precision, recall, detections, false_alarms, latency median/p90, recall_by_kind.
   - Methods: `fixed_bounds, tolerance, schedule, band, anomaly, ai`.
4. **Output:** `backend/eval-results/offroute-e5.5.json`, with the scenario, seeds, forecaster, flight and deviation counts, model manifests and results.

### New: `routing/aerolink.py` (shared with E5.7)

- `AeroLinkConfig(demand, off_route, risk_ranking, en_route, a1)`.
- `ABLATIONS` covers A1–A5.
- `AeroLinkAdvisor(A1Advisor)`:
  - `bind(world, controller)`, `local_hour`, `planned_flights`.
  - `expected_flights(t_s)`: demand plus off-route mass. Demand is cached per 15-min bin.
  - `off_route_mass(t_s)`: the controller's background aircraft, spread with `reachable_mass`.
  - `request_routes(...)`:
    - Scores candidate paths against the live traffic forecast.
    - When the forecaster is GBM, it sets `traffic_ahead` and `local_hour` first.
    - Results are sorted by `total_predicted_s`.
  - `suggest` only runs when `en_route` is on. `mu_risk_s` is forced to 0 unless `risk_ranking` is on.
  - `recalculate(flight_id, lat, lon, destination, vehicle_type, t_s)`: sets `flight_status[flight_id] = "off_route"` and returns options from the nearest connected node.
- `AeroLinkRouteAdvisorWrapper`: the `controller.RouteAdvisor` adapter (`bind`, `request_routes`, `suggest`, `predict`).

### New: `tests/test_e55_offroute.py` (5 tests)

- `consecutive` needs 3 hits and clears after 2 misses.
- Cross-track is about 0 on the route and over 300 m about 2 km away.
- `reachable_mass` sums to 1 and includes the start node.
- `recalculate` sets `off_route` and returns a route to the destination.
- An end-to-end evaluate run with `cv` (3 fake days, train seed 1, rogue seeds 201 and 202) writes all 6 methods and the model file.

## Dependencies

- E5.2 (`samples`, `synthetic_days`), E5.3 (`simdata` helpers, `GBMForecaster`, and the trained model for the default forecaster), E5.4 (`DemandModel`, imported by `routing/aerolink.py`).
- Existing code: `routing/a1.py` (`A1Advisor`, `A1Config.mu_risk_s`, `_candidate_paths`, `_capacities`, `_score`, `suggest`), `traffic.forecast_traffic(planned, capacities, expected)`, `Edge.oriented_geometry`, `Network.route_distance_m`.

## Acceptance criteria

1. Detection precision, recall and latency are reported against the fixed-bounds baseline on `rogue_aircraft` (`offroute-e5.5.json`), with per-kind recall that shows the slow-down case.
2. `models/offroute/offroute.joblib` is committed, with manifests (seeds, training days).
3. `recalculate` marks `off_route` and returns fresh options. Off-route mass enters the traffic forecast (wired in A4+).
4. Tests pass (5), ruff and ty are clean, and results are reported as measured.
