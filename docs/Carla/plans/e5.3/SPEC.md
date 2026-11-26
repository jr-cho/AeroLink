# E5.3 Trained Forecaster (`aerolink-gbm`): Spec

Source: `docs/Carla/M5_GUIDE.md` Part 7, `docs/PLAN.md` E5.3, ARCHITECTURE §7.1.

## Goal

AeroLink's own forecaster, trained on simulated flights plus the real training days, with intervals calibrated so that p10–p90 really covers about 80%.

## Stories (PLAN.md)

- Build features: kinematics, vehicle type, route context, traffic ahead, time of day.
- Train both heads on simulated data plus the real training days.
- Calibrate the intervals with MAPIE `ConformalizedQuantileRegressor`, and report the measured p10–p90 coverage against the 80% target.
- Version the model files with their training seeds.

## Design

- **Residual over CV.** Both heads predict a correction to the constant-velocity estimate:
  - The position head predicts (along, cross, up) in the aircraft's track frame.
  - The ETA head predicts `log(actual / cv)` remaining time.
- **LightGBM quantile models.** Each (horizon, axis) pair gets p10, p90 and p50 models, in MAPIE's order, with fixed hyperparameters: 300 trees, lr 0.05, 31 leaves, subsample 0.8, colsample 0.9, `random_state=seed`.
- **Calibration.** `ConformalizedQuantileRegressor(models, confidence_level=0.80, prefit=True).conformalize(X_cal, y_cal)`. The predicted quantiles are sorted so that p10 ≤ p50 ≤ p90.
- **Mixed data.** Real flights have NaN graph features, which LightGBM handles natively. The hour is masked on 10% of simulated rows, and the hour and traffic_ahead on 10% of ETA rows, so the model learns what to do when they're missing.

## Modules

### New: `forecast/simdata.py`

- `run_scenarios(network, files, seeds)`
- `position_samples(results, network, stride_s=30, noise_m=5.0, seed=0) -> (PositionSamples, graphs)`:
  - Built from the airborne stretches of `SimulationResult.truth`, with 5 m GPS-like noise added.
  - Each sample carries graph context (`dist_next_node_m`, `turn_at_next_deg`, `remaining_route_m`).
- `eta_samples(results, network, stride_s=30) -> EtaSamples`:
  - Covers completed flights that weren't diverted.
  - The target is the time to reach the site, meaning the first holding or landing status. Pad waits are excluded.
  - Features come from `eta_row`. `traffic_ahead` counts concurrently active flights that share a remaining node.
- Helpers that E5.5 reuses:
  - `AIRBORNE`, `AT_SITE`
  - `graph_context`, `is_junction`
  - `_path_history`, `_path_at`, `_tracks`, `_vehicle`
  - `add_noise`
- `EtaSamples` (`X`, `y_log_ratio`, `cv_remaining_s`, `actual_remaining_s`, `group`; `subset`, `concat`).
- `position_samples` asserts that the scenario uses `dt_s == 1.0`.

### New: `forecast/gbm.py`

- `MODEL_DIR = backend/models/aerolink-gbm`, `AXES = ("along","cross","up")`, `COVERAGE_TARGET = 0.80`.
- `make_quantile_model(alpha, seed)`, `fit_calibrated(X_tr, y_tr, X_cal, y_cal, seed)`, `predict_q(model, X) -> (N,3)`.
- A log filter hides MAPIE's repeated "ill-sorted" INFO line.
- `GBMForecaster(Forecaster)`:
  - Named `"aerolink-gbm"`.
  - `load(network, model_dir)` raises `FileNotFoundError` with the training command when the model is missing.
  - `predict_position_batch(tracks, horizons_s, vehicles, hours, graphs)`:
    - Rotates the along/cross p50 back to east/north.
    - p10/p90 are the bounding box of the rotated quantile rectangle.
    - Up is used as is.
  - `predict_position(...)` follows the protocol and gives a fixed small box when there are fewer than 2 fixes.
  - `predict_eta(path, origin_t_s, vehicle_type, ...)`:
    - Each node's ETA is the CV time to that node scaled by exp(p10/p50/p90 ratio).
    - Uses the `traffic_ahead` and `local_hour` attributes when the route engine sets them (E5.7).

### New: `forecast/train_gbm.py` (CLI)

```
uv run python -m aerolink.forecast.train_gbm [--with-chronos] [--no-real]
    [--network F] [--scenarios DIR] [--train-seeds 1..6] [--test-seeds 101 102]
    [--data DIR] [--seed 42] [--max-eval-samples 3000] [--model-dir DIR] [--out F]
```

- **Simulated data:** every `scenarios/*.yaml`. Training seeds are 1–6 and held-out seeds 101–102.
- **Real data:** the E5.2 `split_days`. With `--no-real` it trains on simulated data only. It returns 1 when there are fewer than 2 real days unless `--no-real` is set.
- **Calibration split:** 20% of groups (flights or segments) are held back, using `_group_split` with the seed.
- **Model count:** 4 horizons × 3 axes position models plus 1 ETA model, each made of 3 LightGBM quantile models, so 39 LightGBM models in all.
- **Saved files:**
  - `model.joblib` (compress=3) holding `{"position", "eta", "manifest"}`.
  - `manifest.json` with:
    - model, seed, sim train/test seeds, scenarios
    - real_days {train, held_out}
    - coverage_target, sample counts
    - library versions (lightgbm, mapie, scikit-learn, numpy)
    - the `sha256` of `model.joblib`
- **Evaluation:** `eval-results/aerolink-gbm-e5.3.json` contains:
  - `position_sim_held_out`, `position_real_held_out`: E5.2 `score` rows per forecaster (cv, aerolink-gbm, and chronos with `--with-chronos`).
  - `position_calibration_coverage_{sim,real}_held_out`: per-horizon p10–p90 coverage in the along/cross/up frame.
  - `eta_sim_held_out`: per bucket (≤5/10/20/30/45 min), CV against GBM MAE and coverage. CV uses a fixed ±max(30 s, 8%) window.
  - `manifest`, `minutes`.

### New: `tests/test_e53_gbm.py` (4 tests)

- The track frame round-trips.
- A straight flight has zero residual.
- A tiny end-to-end training run (3 fake days, `i4_peak`, seeds 1 and 2) writes the manifest with seed 42 and a 64-character sha256, and the report has real held-out rows and ETA rows.
- The loaded model predicts ordered ETAs along a real path and ordered position quantiles.

## Dependencies

- **`mapie` is not in `pyproject.toml` yet.** Run `uv add mapie`. The guide's Part 4 added it together with pyarrow, but only pyarrow landed.
- `lightgbm`, `scikit-learn` and `joblib` are already installed.
- E5.2: `features.py`, `samples.py`, `benchmark.predict_all`/`score`, `synthetic_days`.

## Acceptance criteria

1. Results against `cv` and `chronos` on both simulated and real held-out data are committed (`aerolink-gbm-e5.3.json`, with `--with-chronos`).
2. Measured p10–p90 coverage is reported against the 80% target, per horizon and axis, on simulated and real held-out data.
3. The model files are committed with a manifest that records seeds, data, library versions and a sha256.
4. `realdata-e5.2.json` is regenerated with `aerolink-gbm` alongside `cv` and `chronos`, on the same samples and seed.
5. Tests pass (4), ruff and ty are clean, and results are reported as measured.
