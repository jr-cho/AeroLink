# Evaluation results

Every number quoted in `docs/ARCHITECTURE.md` (§7, §9, §10) and the README comes from a file in this directory. They are written by the commands below, run from `backend/`. Results are reported as measured; no parameter is tuned to favour AeroLink.

On macOS set `HF_HUB_OFFLINE=1 OMP_NUM_THREADS=1` for anything that loads the models: torch (Chronos) and LightGBM each bring an OpenMP runtime and clash otherwise.

| File | What | Command |
|---|---|---|
| `m4-results.json` | B0, B1, A1 per scenario (the M4 comparison; A1 uses the `cv` ETAs) | `uv run python -m aerolink.eval.harness --network ../airspace/central-fl-v1.geojson --scenarios scenarios --configs B0 B1 A1` |
| `m5-ablations.json`, `m5-ablations.md` | B0, B1, A1 to A5 and A5 at 50% acceptance over all 10 scenarios (80 runs). The `.md` table is also copied into ARCHITECTURE §9.3 between its `ablation-table` markers | `uv run python -m aerolink.eval.ablations --update-docs` |
| `realdata-e5.1.json` | adsb.lol pipeline counts per day: what each filter removed, segments kept | `uv run python -m aerolink.realdata.fetch --days 14` (the raw days are streamed, never stored) |
| `realdata-e5.2.json` | position head on real held-out days: `cv`, `chronos`, `aerolink-gbm` | `uv run python -m aerolink.realdata.benchmark --forecasters cv chronos aerolink-gbm` |
| `aerolink-gbm-e5.3.json` | `aerolink-gbm` training manifest and results: position error and coverage (simulated and real held-out), ETA error and coverage in flight and at departure | `uv run python -m aerolink.forecast.train_gbm` (also writes `models/aerolink-gbm/`) |
| `demand-e5.4.json` | demand forecaster against simple baselines; labelled synthetic | `uv run python -m aerolink.demand.evaluate` |
| `offroute-e5.5.json` | off-route detection on `rogue_aircraft`: precision, recall and latency of the fixed bounds, the learned tolerance, and the combined AI detector | `uv run python -m aerolink.offroute.evaluate` |
| `summary-e5.6.json` | route-summary LLM: pass rate of the number check over 50 cases | `uv run python -m aerolink.summary.evaluate` (needs `LLM_MODEL` and `LLM_API_KEY`; `--template-only` needs neither) |

## Headlines

- **Ablations (10 scenarios, 161 completed trips):** B1 and A5 produce identical routes and outcomes; no suggestion is offered in any committed scenario. ETA error at departure is 104 s for B1 and 61 s for A2 to A5; p10-p90 window coverage is 84% against 39% for the fixed ±60 s window.
- **Real data (4 held-out days, 5,000 samples):** mean position error at 30 s is 225 m for `aerolink-gbm`, 287 m for `cv` and 371 m for `chronos`; horizontal coverage 0.77 against 0.11 and 0.50.
- **Departure ETA windows (simulated held-out, 333 departures):** coverage 0.748 against the 0.80 target, from 207 calibration rows. Below target, and listed as a known limitation.
- **Off-route (59 flights, 32 injected deviations):** the AI detector reaches precision 1.00 and recall 0.875; the fixed bounds 1.00 and 0.81.
- **Route summary (50 cases, `qwen3-14b-turbo`):** 47 pass the number check (94%); 3 rejected and replaced by the template; 0 wrong numbers shown.

## Notes

- The simulator's committed scenarios have 0 loss-of-separation events and 0 no-route answers. The 223 bunching events in the ablation table are spacing violations of the architecture's modelling parameters and are the same in every configuration, because no configuration changes a route.
- `eval_runs` is not a database table: `/api/eval` serves these files as written.
- The raw adsb.lol days are never committed. The cleaned segments in `backend/data/adsblol/` are a derived database under ODbL; see the README there.
