"""``aerolink-gbm``: LightGBM quantile forecaster calibrated with MAPIE (PLAN E5.3, ARCHITECTURE §7.1).

Two heads, both residuals over constant velocity (``cv``):

* position head: for each horizon (30/60/120/300 s) and axis (along-track, cross-track, up),
  a LightGBM model per quantile (p10, p50, p90) predicts where the aircraft will be
  relative to the CV point. MAPIE's ``ConformalizedQuantileRegressor`` widens or narrows
  the p10-p90 interval on held-back calibration data so it really covers ~80%.
* ETA head: one calibrated model predicts log(actual time / CV time) to reach the
  destination; each node's ETA is the CV time to that node scaled by the predicted ratio.

The trained models live in ``backend/models/aerolink-gbm/`` (``model.joblib`` +
``manifest.json`` with seeds, data and library versions). Train with
``uv run python -m aerolink.forecast.train_gbm``.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any, cast

import joblib
import lightgbm as lgb
import numpy as np
from mapie.regression import ConformalizedQuantileRegressor

from aerolink.contracts import VehicleType
from aerolink.forecast.features import (
    POSITION_HORIZONS_S,
    cv_point,
    cv_speed,
    cv_state,
    eta_row,
    from_track_frame,
    position_row,
)
from aerolink.forecast.interface import EtaPrediction, Forecaster, NodeEta, PositionPrediction
from aerolink.network import Network
from aerolink.vehicle import vehicle_cfg

MODEL_DIR = Path(__file__).resolve().parents[3] / "models" / "aerolink-gbm"
AXES = ("along", "cross", "up")


class _QuietMapie(logging.Filter):
    """MAPIE logs an INFO line on every call when p10 > p90 for some rows; we sort them ourselves."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "ill-sorted" not in record.getMessage()


logging.getLogger().addFilter(_QuietMapie())
COVERAGE_TARGET = 0.80  # p10-p90


def make_quantile_model(alpha: float, seed: int) -> lgb.LGBMRegressor:
    return lgb.LGBMRegressor(
        objective="quantile",
        alpha=alpha,
        n_estimators=300,
        learning_rate=0.05,
        num_leaves=31,
        min_child_samples=20,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.9,
        random_state=seed,
        verbose=-1,
    )


def fit_calibrated(
    X_train: np.ndarray, y_train: np.ndarray, X_cal: np.ndarray, y_cal: np.ndarray, seed: int
) -> ConformalizedQuantileRegressor:
    """Three quantile models (p10, p90, p50, in MAPIE's order) + conformal calibration."""
    models = [make_quantile_model(a, seed).fit(X_train, y_train) for a in (0.1, 0.9, 0.5)]
    cqr = ConformalizedQuantileRegressor(cast(Any, models), confidence_level=COVERAGE_TARGET, prefit=True)
    cqr.conformalize(X_cal, y_cal)
    return cqr


def predict_q(model: ConformalizedQuantileRegressor, X: np.ndarray) -> np.ndarray:
    """(N, 3) calibrated p10, p50, p90."""
    p50, pis = model.predict_interval(X)
    lo, hi = pis[:, 0, 0], pis[:, 1, 0]
    return np.column_stack([np.minimum(lo, p50), p50, np.maximum(hi, p50)])


class GBMForecaster(Forecaster):
    def __init__(self, network: Network | None, bundle: dict[str, Any]):
        self.network = network
        self.position: dict[tuple[int, str], ConformalizedQuantileRegressor] = bundle["position"]
        self.eta: ConformalizedQuantileRegressor | None = bundle.get("eta")
        self.manifest: dict = bundle.get("manifest", {})
        # Optional context the route engine sets before asking for ETAs (A2+): number of
        # planned flights sharing a node with the path being scored.
        self.traffic_ahead: float = math.nan
        self.local_hour: float | None = None  # local hour of day, if the caller knows it

    @property
    def name(self) -> str:
        return "aerolink-gbm"

    @classmethod
    def load(cls, network: Network | None, model_dir: Path = MODEL_DIR) -> GBMForecaster:
        path = model_dir / "model.joblib"
        if not path.exists():
            raise FileNotFoundError(f"{path} not found: train it with `uv run python -m aerolink.forecast.train_gbm`")
        return cls(network, joblib.load(path))

    # --- position head -------------------------------------------------------------------

    def predict_position_batch(
        self,
        tracks: list[np.ndarray],
        horizons_s: list[int] | None = None,
        vehicles: list[str] | None = None,
        hours: list[float] | None = None,
        graphs: list[dict[str, float]] | None = None,
    ) -> list[list[PositionPrediction]]:
        horizons = horizons_s or POSITION_HORIZONS_S
        tracks = [np.asarray(t, dtype=float) for t in tracks]
        n = len(tracks)
        out: list[list[PositionPrediction | None]] = [[None] * len(horizons) for _ in range(n)]
        for j, h in enumerate(horizons):
            X = np.array(
                [
                    position_row(
                        tracks[i],
                        h,
                        vehicles[i] if vehicles else None,
                        hours[i] if hours else None,
                        graphs[i] if graphs else None,
                    )
                    for i in range(n)
                ]
            )
            q = {axis: predict_q(self.position[(h, axis)], X) for axis in AXES}
            for i in range(n):
                ce, cn = cv_point(tracks[i], h)
                _, course = cv_state(tracks[i])
                # p50, then the bounding box of the rotated along/cross p10-p90 rectangle
                e50, n50 = from_track_frame(q["along"][i, 1], q["cross"][i, 1], course)
                corners = [
                    from_track_frame(a, c, course)
                    for a in (q["along"][i, 0], q["along"][i, 2])
                    for c in (q["cross"][i, 0], q["cross"][i, 2])
                ]
                es = [c[0] for c in corners]
                ns = [c[1] for c in corners]
                out[i][j] = PositionPrediction(
                    horizon_s=h,
                    east_p10=ce + min(es),
                    east_p50=ce + e50,
                    east_p90=ce + max(es),
                    north_p10=cn + min(ns),
                    north_p50=cn + n50,
                    north_p90=cn + max(ns),
                    up_p10=float(q["up"][i, 0]),
                    up_p50=float(q["up"][i, 1]),
                    up_p90=float(q["up"][i, 2]),
                )
        return [[p for p in row if p is not None] for row in out]

    def predict_position(
        self,
        track: list[tuple[float, float, float]],
        t_s: float,
        vehicle_type: VehicleType,
        horizons_s: list[int] | None = None,
    ) -> list[PositionPrediction]:
        if len(track) < 2:
            return [PositionPrediction(h, -10.0, 0.0, 10.0, -10.0, 0.0, 10.0, -5.0, 0.0, 5.0) for h in horizons_s or []]
        return self.predict_position_batch([np.asarray(track)], horizons_s, vehicles=[vehicle_type])[0]

    # --- ETA head ------------------------------------------------------------------------

    def predict_eta(
        self,
        path: list[str],
        origin_t_s: float,
        vehicle_type: VehicleType,
        track: list[tuple[float, float, float]] | None = None,
        current_speed_mps: float | None = None,
    ) -> EtaPrediction:
        assert self.network is not None and self.eta is not None, "ETA head needs the network and an ETA model"
        cruise = vehicle_cfg(vehicle_type).cruise_speed_mps
        speed = cv_speed(cruise, current_speed_mps)
        total_m = self.network.route_length_m(path)
        cv_total = total_m / max(1.0, speed)
        merges = sum(self.network.graph.degree(n) >= 3 for n in path[1:-1])
        row = eta_row(
            vehicle_type,
            self.local_hour,
            total_m,
            cv_total,
            # not airborne yet (no speed): assume cruise, as the training flights were in the air
            current_speed_mps if current_speed_mps is not None and current_speed_mps > 1.0 else cruise,
            cruise,
            len(path),
            merges,
            self.traffic_ahead,
        )
        r10, r50, r90 = np.exp(predict_q(self.eta, np.array([row]))[0])
        etas: list[NodeEta] = []
        cumul_m = 0.0
        for i, node in enumerate(path):
            if i > 0:
                cumul_m += self.network.edges[(path[i - 1], node)].length_m
            cv_t = cumul_m / max(1.0, speed)
            etas.append(
                NodeEta(
                    node=node,
                    eta_p10=origin_t_s + cv_t * float(r10),
                    eta_p50=origin_t_s + cv_t * float(r50),
                    eta_p90=origin_t_s + cv_t * float(r90),
                )
            )
        return EtaPrediction(path=path, origin_t_s=origin_t_s, etas=etas)
