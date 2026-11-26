# E5.2 Real-Data Benchmark: Spec

Source: `docs/Carla/M5_GUIDE.md` Part 6, `docs/PLAN.md` E5.2.

## Goal

Measure how well the existing forecasters (`cv`, `chronos`) predict real low-altitude flights, using a day-level train/held-out split that E5.3 and E5.5 reuse.

## Stories (PLAN.md)

- Split by day into training and held-out sets.
- Compare `cv` and `chronos` on the held-out days (position head).

## Changes

### Replaced: `forecast/chronos.py` (Chronos-2 API fix)

The current wrapper loads with the Chronos v1 `ChronosPipeline`, which can't run the Chronos-2 weights, and reports offsets from the wrong point. The replacement:

- Loads with `chronos.BaseChronosPipeline.from_pretrained(model_name, device_map="cpu")`. The default is `autogluon/chronos-2-small`, loaded lazily on the first call.
- Position head:
  - The context is the last 60 s as a 3-variate east/north/up series, in metres, relative to the **last** fix.
  - Forecasts `max(horizons)` steps with `predict_quantiles(..., quantile_levels=[0.1, 0.5, 0.9])` and reads horizon `h` at index `h-1`.
- New `predict_position_batch(tracks, horizons_s=None, batch_size=64)`, batched for CPU speed.
- `predict_position` keeps the `Forecaster` protocol and returns a fixed ±10 m box when there are fewer than 2 fixes.
- ETA head:
  - Forecasts the last 60 s of ground speed 64 steps ahead.
  - Integrates the p90/p50/p10 speed profiles to get p10/p50/p90 ETAs, then assumes cruise speed after the forecast ends.
  - Falls back to a CV ETA when the track has fewer than 3 fixes.
- `is_available(model_name) -> bool`.
- The docstring records that fine-tuning is supported (`Chronos2Pipeline.fit`) but not used.

### New: `forecast/features.py`

Shared geometry and the feature definitions that E5.3 uses. E5.2 needs:

- `to_local`, `from_local`
- `cv_state`, `cv_point`
- `to_track_frame`, `from_track_frame`
- `POSITION_HORIZONS_S = [30, 60, 120, 300]`, `HISTORY_S = 60`
- `CATEGORY_TO_VEHICLE = {"A1": "light", "A7": "rotor"}`

The full feature set (`POSITION_FEATURES`, `position_row`, `position_targets`, `ETA_FEATURES`, `eta_row`, `cv_speed`, `kinematics`) ships here too, exactly as in the guide.

### New: `realdata/samples.py`

- `PositionSamples` dataclass:
  - `day`, `segment_id`, `t`, `vehicle`, `hour` have shape (N,).
  - `history` has shape (N, 60, 3) and holds lat, lon, alt_m, oldest first.
  - `future` has shape (N, H, 3). It is NaN past the end of the segment.
  - Also has `horizons_s`, `__len__` and `subset(mask)`.
- `list_days(data_dir)`: sorted days that have a `segments-*.parquet` file.
- `split_days(days, held_out_fraction=0.3)`:
  - Chronological: the latest days are held out.
  - At least one day is held out, and at least one is kept for training.
  - Raises `ValueError` when there are fewer than 2 days.
- `build_position_samples(days, data_dir, stride_s=30, horizons_s=None)`:
  - Takes one sample every `stride_s` along each segment.
  - Fills altitude gaps by interpolation and skips segments with fewer than 2 valid altitudes.
  - The hour is local (fixed UTC−5).
- `DATA_DIR = backend/data/adsblol`, `_segments(path)`: generator over (segment_id, category, t, xyz). E5.5 uses it.

### New: `realdata/benchmark.py` (CLI)

```
uv run python -m aerolink.realdata.benchmark [--forecasters cv chronos [aerolink-gbm]]
    [--chronos-model M] [--data DIR] [--held-out-fraction 0.3] [--stride-s 30]
    [--max-samples 5000] [--seed 42] [--out F]
```

- `make_forecaster(name, chronos_model)`: `cv`, `chronos` and `aerolink-gbm` (`aerolink-gbm` is imported lazily and only exists after E5.3).
- `predict_all(forecaster, samples)`:
  - Uses `predict_position_batch` when the forecaster has one.
  - For `aerolink-gbm` it also passes `vehicles` and `hours`.
- `score(samples, preds)` returns one row per horizon with these fields:
  - `n`
  - `fde_mean_m`, `fde_median_m`, `fde_p90_m`: horizontal p50 error.
  - `ade_mean_m`: mean error over all horizons up to this one.
  - `coverage_east`, `coverage_north`, `coverage_up`, `coverage_horizontal`: the share of truths inside p10–p90.
- Scores only held-out samples. When there are more than `--max-samples`, it scores a seeded random subset.
- Output `backend/eval-results/realdata-e5.2.json` contains:
  - `attribution` (ODbL)
  - `split` (train / held-out days)
  - `samples` (total, scored, stride, seed)
  - `chronos_model`, `ms_per_sample`
  - `results[forecaster]`: the rows above.

### New: `realdata/synthetic_days.py`

`make_days(out, days=5, flights_per_day=25, seed=0)` writes made-up days in the exact E5.1 format (source `synthetic-test-data`), for tests and dry runs. CLI: `python -m aerolink.realdata.synthetic_days /tmp/fake-adsblol`. It must never write into `backend/data/adsblol`.

### New: `tests/test_e52_benchmark.py` (5 tests, no downloads)

- The split is chronological, and a single day raises.
- Samples have shapes (60, 3) and (4, 3), the 30 s future is always finite, and vehicles come from {rotor, light}.
- The benchmark CLI with `cv` writes 4 horizons, the error grows with the horizon, and the ODbL attribution is present.
- The Chronos batch with a fake pipeline gives ordered quantiles, with offsets relative to the last fix.
- `_time_to_cover` uses the speed profile, then cruise speed.

## Acceptance criteria

1. The forecast error per horizon is committed for both `cv` and `chronos` in `eval-results/realdata-e5.2.json`.
2. The split is by day, and the held-out days never appear in training (they are listed in the JSON).
3. `pytest tests/test_e52_benchmark.py` passes (5 tests), and ruff and ty are clean.
4. Results are reported as measured. No code is tuned to make one forecaster win.
