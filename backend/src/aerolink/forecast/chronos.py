"""Chronos-2 zero-shot forecaster (PLAN E4.2, used on real data in E5.2).

Loads a pretrained Chronos-2 model once (``chronos-forecasting`` >= 2.3) and exposes the
``Forecaster`` protocol:

* position head: the last 60 s of east/north/up positions as one multivariate series;
  the model forecasts the next ``max(horizons)`` seconds and we read out each horizon.
* ETA head: the last 60 s of ground speed; the p10/p50/p90 speed profiles are integrated
  along the path (fast profile -> early arrival, slow profile -> late arrival).

Fine-tuning: supported by the installed library (``Chronos2Pipeline.fit``, full or LoRA);
not used here, see ARCHITECTURE §7.1.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from aerolink.contracts import VehicleType
from aerolink.forecast.cv import _ETA_TIME_SPREAD
from aerolink.forecast.features import to_local
from aerolink.forecast.interface import EtaPrediction, Forecaster, NodeEta, PositionPrediction
from aerolink.network import Network
from aerolink.vehicle import vehicle_cfg

_POSITION_HORIZONS = [30, 60, 120, 300]
_HISTORY_S = 60
_ETA_PREDICTION_S = 64
_QUANTILES = [0.1, 0.5, 0.9]


def _enu_series(track: np.ndarray) -> np.ndarray:
    """(3, T) east/north/up in metres relative to the *last* fix of a (T, 3) lat/lon/alt track."""
    lat0, lon0 = float(track[-1, 0]), float(track[-1, 1])
    east, north = to_local(track[:, 0], track[:, 1], lat0, lon0)
    up = track[:, 2] - track[-1, 2]
    return np.stack([east, north, up]).astype(np.float32)


def _speed_series(track: np.ndarray) -> np.ndarray:
    """(T-1,) ground speed m/s from a 1 Hz lat/lon/alt track."""
    lat0, lon0 = float(track[-1, 0]), float(track[-1, 1])
    east, north = to_local(track[:, 0], track[:, 1], lat0, lon0)
    return np.hypot(np.diff(east), np.diff(north)).astype(np.float32)


class ChronosForecaster(Forecaster):
    """Zero-shot Chronos-2 forecaster (default weights ``autogluon/chronos-2-small``)."""

    def __init__(self, network: Network | None, model_name: str = "autogluon/chronos-2-small"):
        self.network = network
        self.model_name = model_name
        self._pipeline: Any = None

    @property
    def name(self) -> str:
        return "chronos"

    def _load(self) -> Any:
        if self._pipeline is None:
            from chronos import BaseChronosPipeline

            self._pipeline = BaseChronosPipeline.from_pretrained(self.model_name, device_map="cpu")
        return self._pipeline

    def _quantiles(self, context: np.ndarray, prediction_length: int) -> np.ndarray:
        """(B, V, H, 3) p10/p50/p90 for a (B, V, T) context batch."""
        import torch

        quantiles, _ = self._load().predict_quantiles(
            torch.tensor(context, dtype=torch.float32),
            prediction_length=prediction_length,
            quantile_levels=_QUANTILES,
        )
        return np.stack([q.detach().cpu().numpy() for q in quantiles])

    # --- position head -------------------------------------------------------------------

    def predict_position_batch(
        self, tracks: list[np.ndarray], horizons_s: list[int] | None = None, batch_size: int = 64
    ) -> list[list[PositionPrediction]]:
        """Position predictions for many (T, 3) lat/lon/alt tracks at once (much faster on CPU)."""
        horizons = horizons_s or _POSITION_HORIZONS
        out: list[list[PositionPrediction]] = []
        for start in range(0, len(tracks), batch_size):
            chunk = [np.asarray(t, dtype=float)[-_HISTORY_S:] for t in tracks[start : start + batch_size]]
            context = np.stack([_enu_series(t) for t in chunk])  # (B, 3, T)
            q = self._quantiles(context, max(horizons))  # (B, 3, H, 3)
            for b in range(len(chunk)):
                preds = []
                for h in horizons:
                    e, n, u = q[b, 0, h - 1], q[b, 1, h - 1], q[b, 2, h - 1]
                    preds.append(
                        PositionPrediction(
                            horizon_s=h,
                            east_p10=float(e[0]),
                            east_p50=float(e[1]),
                            east_p90=float(e[2]),
                            north_p10=float(n[0]),
                            north_p50=float(n[1]),
                            north_p90=float(n[2]),
                            up_p10=float(u[0]),
                            up_p50=float(u[1]),
                            up_p90=float(u[2]),
                        )
                    )
                out.append(preds)
        return out

    def predict_position(
        self,
        track: list[tuple[float, float, float]],
        t_s: float,
        vehicle_type: VehicleType,
        horizons_s: list[int] | None = None,
    ) -> list[PositionPrediction]:
        horizons = horizons_s or _POSITION_HORIZONS
        if len(track) < 2:
            return [PositionPrediction(h, -10.0, 0.0, 10.0, -10.0, 0.0, 10.0, -5.0, 0.0, 5.0) for h in horizons]
        return self.predict_position_batch([np.asarray(track, dtype=float)], horizons)[0]

    # --- ETA head ------------------------------------------------------------------------

    def predict_eta(
        self,
        path: list[str],
        origin_t_s: float,
        vehicle_type: VehicleType,
        track: list[tuple[float, float, float]] | None = None,
        current_speed_mps: float | None = None,
    ) -> EtaPrediction:
        """Per-node ETAs from forecast speed profiles; constant cruise speed after the forecast ends."""
        assert self.network is not None, "the ETA head needs the corridor network"
        cruise = vehicle_cfg(vehicle_type).cruise_speed_mps
        speed = cruise
        if current_speed_mps is not None and current_speed_mps > 1.0:
            speed = 0.5 * (cruise + current_speed_mps)
        if track is None or len(track) < 3:
            return self._cv_eta(path, origin_t_s, speed)

        speeds = _speed_series(np.asarray(track, dtype=float)[-(_HISTORY_S + 1) :])
        q = self._quantiles(speeds[None, None, :], _ETA_PREDICTION_S)[0, 0]  # (H, 3)
        etas: list[NodeEta] = []
        cumul_m = 0.0
        for i, node in enumerate(path):
            if i > 0:
                cumul_m += self.network.edges[(path[i - 1], node)].length_m
            fast = self._time_to_cover(q[:, 2], cumul_m, speed)  # p90 speed -> early arrival
            mid = self._time_to_cover(q[:, 1], cumul_m, speed)
            slow = self._time_to_cover(q[:, 0], cumul_m, speed)
            etas.append(
                NodeEta(node=node, eta_p10=origin_t_s + fast, eta_p50=origin_t_s + mid, eta_p90=origin_t_s + slow)
            )
        return EtaPrediction(path=path, origin_t_s=origin_t_s, etas=etas)

    @staticmethod
    def _time_to_cover(profile: np.ndarray, distance_m: float, cruise_speed: float) -> float:
        """Seconds to fly ``distance_m`` at a 1 Hz speed profile, then at ``cruise_speed``."""
        if distance_m <= 0:
            return 0.0
        covered = np.cumsum(np.maximum(1.0, profile))
        idx = int(np.searchsorted(covered, distance_m))
        if idx < len(covered):
            before = covered[idx - 1] if idx > 0 else 0.0
            return idx + (distance_m - before) / max(1.0, float(profile[idx]))
        return len(profile) + (distance_m - float(covered[-1])) / max(1.0, cruise_speed)

    def _cv_eta(self, path: list[str], origin_t_s: float, speed: float) -> EtaPrediction:
        assert self.network is not None
        etas: list[NodeEta] = []
        cumul_m = 0.0
        total_m = self.network.route_length_m(path)
        total_s = total_m / max(1.0, speed)
        spread_s = max(30.0, total_s * _ETA_TIME_SPREAD)
        for i, node in enumerate(path):
            if i > 0:
                cumul_m += self.network.edges[(path[i - 1], node)].length_m
            eta = origin_t_s + (cumul_m / max(1.0, total_m)) * total_s
            etas.append(NodeEta(node=node, eta_p10=eta - spread_s, eta_p50=eta, eta_p90=eta + spread_s))
        return EtaPrediction(path=path, origin_t_s=origin_t_s, etas=etas)


def is_available(model_name: str = "autogluon/chronos-2-small") -> bool:
    """True if the weights can be loaded (downloaded once from Hugging Face, then cached)."""
    try:
        ChronosForecaster(None, model_name)._load()
    except Exception:  # noqa: BLE001 - no network, no cache, wrong library version...
        return False
    return True
