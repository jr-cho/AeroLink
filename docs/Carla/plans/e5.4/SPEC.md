# E5.4 Demand Forecaster: Spec

Source: `docs/Carla/M5_GUIDE.md` Part 8, `docs/PLAN.md` E5.4, ARCHITECTURE §7.2–7.3.

## Goal

Forecast flights before they are planned, so the traffic forecast includes expected but unbooked flights (used by config A3+ in E5.7).

## Stories (PLAN.md)

- Generate synthetic demand (diurnal Poisson) scaled from the FDOT AAM Business Plan's 2027 passenger-trip forecast, and label it synthetic.
- Passenger: departures per `evtol` site per 15 min (Chronos-2), and destinations (LightGBM).
- Medical: dispatches per `helipad` per 15 min (Chronos-2).
- Add expected flights to the traffic forecast, weighted by probability.

## Data: synthetic, labelled everywhere

`demand/synthetic.py`, with `SyntheticDemandConfig` defaults:

- **Passenger volume:** 220,000 trips/yr (FDOT 2027, low end) × 0.27 Central Florida share (an assumption based on population) / 365 ≈ **163 departures/day**. The volume is shared across `network.nodes_for("passenger")` by pad count.
- **Passenger timing:**
  - Diurnal shape from `aerolink.sim.demand.diurnal_rate_per_hour` (`DemandConfig` defaults), with weekends at ×0.7.
  - Drawn as Poisson counts per site per 15-min bin (96 bins/day).
- **Destinations:** a gravity model, pads × exp(−distance/90 km), times a fixed lognormal twist per (period, origin, destination). There are 4 periods: night, morning 6–10, midday 10–16, evening 16–21. The diagonal is zero.
- **Medical:** 48/day across helipads (nodes whose `types` include `helipad`), with a Dirichlet(1.5) share per site and a small evening rise.
- **Run settings:** 28 days from 2027-01-04 (a Monday), seed 2027. Output is deterministic.
- **Outputs** in `backend/data/synthetic-demand/`:
  - `counts.parquet`: synthetic, day, weekday, bin, site, kind, count.
  - `trips.parquet`: synthetic, day, weekday, t_of_day_s, origin, destination.
  - Both carry the schema metadata `label = LABEL` and `config`.
- **API:** `generate(network, cfg)`, `write`, `load`, `count_matrix(counts, kind) -> (days, sites, m[site, day*96+bin])`, `period_of`, `destination_table`, `BIN_S=900`, `BINS_PER_DAY=96`, `LABEL`.

## Forecasters (`demand/forecaster.py`)

Each count forecaster maps `(history[S, T], horizon H)` to `(S, H, 3)` p10/p50/p90. The baselines use a Poisson normal-approximation band (±1.2816·√rate, floored at 0).

| Name | Rule |
|---|---|
| `mean_rate` | per-site mean over the history |
| `last_schedule` | same bin one day earlier |
| `constant_rate` | one global mean |
| `tod_mean` | per-site mean for that time of day (stronger reference, not in PLAN) |
| `chronos` (`ChronosCounts`) | Chronos-2 zero-shot, 14-day context, with `tod_sin`/`tod_cos` as past and future covariates; quantiles sorted and clipped at ≥0 |

- `COUNT_FORECASTERS` holds the four baselines. Chronos is added at runtime.
- `DestinationModel(sites, seed)`:
  - A LightGBM multiclass model on (origin index as a categorical, hour, weekday).
  - `proba` never returns the origin and renormalizes.
  - `uniform` is the baseline.
- `DemandModel(forecast, destinations, weekday)`:
  - `from_history(histories, destinations, weekday, count_forecaster=tod_mean)` forecasts one day once and caches it.
  - `expected_flights(local_hour, t_s, travel_time_s, horizon_s=1800, medical_bases=None, min_prob=0.02)` returns `(prob, node, eta_p10, eta_p50, eta_p90)` tuples, the format `traffic.forecast_traffic(expected=...)` takes:
    - Passenger: rate × P(destination), arriving at the destination.
    - Medical: rate, arriving at the helipad.
    - ETAs are spread over the departure bin.

## Evaluation (`demand/evaluate.py`)

```
uv run python -m aerolink.demand.evaluate [--no-chronos] [--data DIR] [--test-days 7]
    [--chronos-model M] [--seed 42] [--out F]
```

- Generates the history if it's missing.
- Each of the last `test-days` days is forecast one day ahead from all earlier days.
- Count metrics per forecaster and kind:
  - `mae_per_site_bin`
  - `daily_total_bias`
  - `coverage_p10_p90` (integer-rounded band)
  - `rate_calibration` (predicted / observed total)
- Destination metrics on held-out trips, LightGBM against uniform: `top1_accuracy`, `log_loss`.
- Output: `backend/eval-results/demand-e5.4.json`, containing `label`.

## Tests (`tests/test_e54_demand.py`, 5 tests, no downloads)

- The synthetic data is labelled and deterministic, with 100–200 departures a day and never origin = destination.
- Baseline shapes and quantile order are correct, and `last_schedule` returns the previous day.
- Chronos counts use the covariates (checked with a fake pipeline).
- The evaluate CLI reports all four baselines, and LightGBM beats uniform on top-1 accuracy.
- Expected flights have probabilities in (0, 1] and ordered ETAs.

## Acceptance criteria

1. Accuracy is reported against the simple baselines (mean rate, last schedule, constant rate), plus `tod_mean` and `chronos`, for passenger and medical. This includes the medical rate calibration.
2. The destination model is compared with uniform.
3. Data and results are labelled synthetic in the Parquet metadata, the JSON, the module docstrings and the docs.
4. `data/synthetic-demand/` and `eval-results/demand-e5.4.json` are committed. Tests pass, and ruff and ty are clean.
