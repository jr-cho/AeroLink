# E5.7 Full Ablations: Spec

Source: `docs/Carla/M5_GUIDE.md` Parts 11–12, `docs/PLAN.md` E5.7, ARCHITECTURE §9.3.

## Goal

Measure what each AI component adds, in the closed-loop simulator, and publish the table in ARCHITECTURE §9.3. This is the M5 exit criterion: *the full ablation table and the real-data results are committed and reproducible.*

## Stories (PLAN.md)

- Run configurations A2–A5 across all scenarios.
- Run A5 with a 50% suggestion acceptance rate as a sensitivity check.
- Publish the B1 vs A5 comparison.

## Configurations

| Config | Route engine | Forecaster | Features |
|---|---|---|---|
| B0 | `make_advisor("B0")` (shortest path) | — | — |
| B1 | `make_advisor("B1")` (classical) | — | — |
| A1 | `AeroLinkAdvisor`, `ABLATIONS["A1"]` | `cv` | ETA windows; `mu_risk_s=0` |
| A2 | `ABLATIONS["A2"]` | `aerolink-gbm` | ETA windows |
| A3 | `ABLATIONS["A3"]` | `aerolink-gbm` | + demand (expected flights) |
| A4 | `ABLATIONS["A4"]` | `aerolink-gbm` | + off-route mass |
| A5 | `ABLATIONS["A5"]` | `aerolink-gbm` | + risk ranking (μ = 600 s) + en-route suggestions |
| A5@50% | as A5 | `aerolink-gbm` | `suggestion_acceptance_rate = 0.5` |

## Changes

### `sim/runner.py` (final version)

The only change from the E5.1 version: after constructing the `Controller`, the runner calls `bind(world, controller)` if `controller.advisor` has a callable `bind`. The diff is exactly 3 added lines. The golden digests of existing scenarios must not change, because advisors without `bind` behave exactly as before.

### New: `eval/ablations.py` (CLI)

```
uv run python -m aerolink.eval.ablations [--configs ...] [--seeds ...] [--no-chronos]
    [--network F] [--scenarios DIR] [--out F] [--update-docs]
```

- `build_demand(network, scenario_start, use_chronos)`:
  - Loads `data/synthetic-demand` and fits a `DestinationModel`.
  - Count forecasts come from `ChronosCounts`, falling back to `tod_mean` when Chronos is unavailable (with a printed note).
  - The weekday comes from the start of the first scenario.
- `advisor_for(config, network, gbm, demand)`.
- `run_one(network, scenario_file, config, seed, gbm, demand)` returns a row with:
  - scenario, config, seed
  - trips, trip_time_mean_s, trip_time_p95_s
  - holding_s_total, holding_s_per_trip
  - no_route (`route_unavailable`), suggestions_accepted, bunching_events, loss_of_separation
  - medical_response_mean_s
- `summarize(rows)`: per config, trip-weighted means and summed counts.
- `markdown(summary, seeds, n_scenarios)`:
  - A table with the columns #, Configuration, Trips, Mean trip, p95 trip, Holding/trip, No route, Suggestions, Bunching, Medical response.
  - Then a **B1 vs A5 (as measured)** line with the mean trip time delta (s and %), the holding per trip delta, and the bunching delta.
- `update_docs(table, path=docs/ARCHITECTURE.md)`: replaces the text between `<!-- ablation-table:start -->` and `<!-- ablation-table:end -->`, and returns False when the markers are missing.
- The default seed is each scenario's own seed.
- Outputs:
  - `backend/eval-results/m5-ablations.json` (`{summary, runs}`)
  - `backend/eval-results/m5-ablations.md`
  - ARCHITECTURE §9.3, with `--update-docs`

### New: `tests/test_e57_ablations.py` (4 tests)

- The configs switch the right features (A3 demand, A4 off_route without en_route, A5 risk + en_route).
- `mu_risk_s` is 0 in A4 and 600 in A5.
- The engine is bound to the world (`adv.world is result.final_world`), and `nominal_day` has arrivals.
- `run_one("A1")` produces trips, the table contains `| A1 |`, and `update_docs` replaces only the text between the markers.

### Docs update (guide 11d, a one-shot script)

The script is idempotent and refuses to edit anything it can't find exactly once.

PLAN.md M5:
- Amends the story text (E5.1 resampling, Parquet location; E5.5 band rule) and ticks every M5 story.
- Adds a `**Files**` line per epic.
- Records that Chronos fine-tuning is supported (`Chronos2Pipeline.fit`, chronos-forecasting 2.3.2).

ARCHITECTURE.md:
- §10 resampling and write steps as built.
- Cleaned-data ODbL note.
- §7.4 band check as built.
- The ablations command.
- Table markers after the A5 row in §9.3.
- The demand scaling sentence.

## Acceptance criteria

1. The ablation table in ARCHITECTURE §9.3 is fully populated from committed runs (B0, B1, A1–A5, A5@50%, all 7 scenarios).
2. The B1 vs A5 comparison is published as measured. Identical rows are a valid result, so code and scenarios are not tuned to make AeroLink win.
3. PLAN.md has all M5 stories ticked and files listed, and ARCHITECTURE matches what was built.
4. The full check suite passes: `ruff check`, `ruff format --check`, `ty check`, `pytest -q` (~680 tests).
5. Every artifact listed in guide §12c exists:
   - realdata-e5.1/e5.2, aerolink-gbm-e5.3, demand-e5.4, offroute-e5.5, summary-e5.6
   - m5-ablations.json and .md
   - models/aerolink-gbm/{model.joblib, manifest.json}, models/offroute/offroute.joblib
