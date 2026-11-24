# M4: AI Forecasting and Route Engine

**Status:** complete

This directory holds the committed B0, B1, and A1 evaluation results required by PLAN.md M4.

- `m4-results.json` — per-scenario, per-config metrics from `backend/src/aerolink/eval/harness.py`.

Run the evaluation with:

```bash
cd backend
uv run python -m aerolink.eval.harness --network ../airspace/central-fl-v1.geojson --scenarios scenarios --configs B0 B1 A1
```

A1 currently uses the `cv` (constant velocity) forecaster as its ETA predictor.
The Chronos-2 model is wired but defaults to `cv` to keep M4 evaluation fast and
reproducible without downloading large pretrained weights during CI.
