"""E5.4: synthetic demand, baselines, Chronos-2 wrapper and expected flights (no downloads)."""

import json
from pathlib import Path

import numpy as np
import pytest
import torch

from aerolink.demand.evaluate import main
from aerolink.demand.forecaster import (
    BINS_PER_DAY,
    ChronosCounts,
    DemandModel,
    DestinationModel,
    constant_rate,
    last_schedule,
    mean_rate,
    tod_mean,
)
from aerolink.demand.synthetic import LABEL, SyntheticDemandConfig, count_matrix, generate, write
from aerolink.network import Network


@pytest.fixture(scope="module")
def synthetic(fl_network: Network, tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("demand")
    write(out, *generate(fl_network, SyntheticDemandConfig(days=10)))
    return out


def test_synthetic_is_labelled_scaled_and_deterministic(fl_network: Network):
    counts, trips = generate(fl_network, SyntheticDemandConfig(days=7))
    assert counts.schema.metadata[b"label"].decode() == LABEL
    assert all(counts["synthetic"].to_pylist())
    _, sites, m = count_matrix(counts, "passenger")
    assert len(sites) == len(fl_network.nodes_for("passenger"))
    assert 100 < m.sum() / 7 < 200  # ~163/day on weekdays, less at weekends
    again, _ = generate(fl_network, SyntheticDemandConfig(days=7))
    assert again.equals(counts)
    assert all(o != d for o, d in zip(trips["origin"].to_pylist(), trips["destination"].to_pylist(), strict=True))


def test_baselines_shapes_and_order():
    hist = np.random.default_rng(0).poisson(1.0, size=(3, 2 * BINS_PER_DAY)).astype(float)
    for f in (mean_rate, constant_rate, last_schedule, tod_mean):
        q = f(hist, 8)
        assert q.shape == (3, 8, 3)
        assert (q[..., 0] <= q[..., 1]).all() and (q[..., 1] <= q[..., 2]).all()
    assert np.allclose(last_schedule(hist, 4)[..., 1], hist[:, BINS_PER_DAY : BINS_PER_DAY + 4])


class FakeChronos2:
    def predict_quantiles(self, inputs, prediction_length, quantile_levels):
        assert "tod_sin" in inputs[0]["future_covariates"]
        out = [torch.full((1, prediction_length, 3), float(np.mean(x["target"]))) for x in inputs]
        return out, [o[..., 1] for o in out]


def test_chronos_counts_with_covariates():
    f = ChronosCounts()
    f._pipeline = FakeChronos2()
    q = f(np.ones((2, BINS_PER_DAY * 3)), 4)
    assert q.shape == (2, 4, 3) and np.allclose(q, 1.0)


def test_evaluate_reports_every_forecaster(synthetic: Path, tmp_path: Path):
    out = tmp_path / "e54.json"
    assert main(["--data", str(synthetic), "--no-chronos", "--test-days", "3", "--out", str(out)]) == 0
    r = json.loads(out.read_text())
    assert set(r["counts"]["passenger"]) == {"mean_rate", "last_schedule", "constant_rate", "tod_mean"}
    assert r["destinations"]["lightgbm"]["top1_accuracy"] > r["destinations"]["uniform"]["top1_accuracy"]
    assert r["label"] == LABEL


def test_expected_flights_weight_by_probability(fl_network: Network, synthetic: Path):
    import pyarrow.parquet as pq

    counts = pq.read_table(synthetic / "counts.parquet")
    trips = pq.read_table(synthetic / "trips.parquet").to_pydict()
    hist = {k: count_matrix(counts, k)[1:] for k in ["passenger", "medical"]}
    dest = DestinationModel(hist["passenger"][0]).fit(
        trips["origin"],
        np.array(trips["t_of_day_s"]) / 3600,
        np.array(trips["weekday"], dtype=float),
        trips["destination"],
    )
    model = DemandModel.from_history(hist, dest, weekday=2)
    exp = model.expected_flights(8.0, 0.0, lambda a, b: 600.0)
    assert exp and all(0 < p <= 1 for p, *_ in exp)
    assert all(lo <= mid <= hi for _, _, lo, mid, hi in exp)
```

### 8b. Run the checks

Command:

```bash
uv run ruff format
uv run ruff check --fix
uv run pytest -q tests/test_e54_demand.py
```

Expect **`5 passed`**.

### 8c. Generate the synthetic history

Command:

```bash
uv run python -m aerolink.demand.synthetic
```

Expected output (the numbers will match exactly, because the seed is fixed):

```text
SYNTHETIC demand, 28 days: 4,217 passenger departures (151/day, target 163), 1,380 medical dispatches
wrote .../backend/data/synthetic-demand/counts.parquet and trips.parquet
