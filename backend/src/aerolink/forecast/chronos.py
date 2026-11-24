"""Chronos-2 zero-shot forecaster (PLAN E4.2).

Loads a pretrained Chronos-2 model once and exposes the ``Forecaster``
protocol for position and ETA heads.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np
import torch

from aerolink.contracts import VehicleType
from aerolink.forecast.cv import _ETA_TIME_SPREAD
from aerolink.forecast.interface import EtaPrediction, Forecaster, NodeEta, PositionPrediction
from aerolink.network import Network
from aerolink.vehicle import vehicle_cfg

if TYPE_CHECKING:
    from chronos import ChronosPipeline

_POSITION_HORIZONS = [30, 60, 120, 300]

# Chronos native prediction length is typically 64.  We predict per-step and
# then read out the requested horizons.
_CHRONOS_PREDICTION_LENGTH = 64


def _haversine_course_m(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """Return (distance_m, course_deg) from (lat1,lon1) to (lat2,lon2)."""
    r = 6_371_008.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    dist = 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    course = (math.degrees(math.atan2(x, y)) + 360.0) % 360.0
    return dist, course


def _to_local_en_series(
    track: list[tuple[float, float, float]],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, float]:
    """Convert a lat/lon/alt track into local east/north/up series centered on the first point."""
    lats = np.array([p[0] for p in track], dtype=np.float32)
    lons = np.array([p[1] for p in track], dtype=np.float32)
    alts = np.array([p[2] for p in track], dtype=np.float32)
    lat0, lon0 = lats[0], lons[0]
    k = math.cos(math.radians(lat0))
    east_m = (lons - lon0) * math.radians(1) * 6_371_008.8 * k
    north_m = (lats - lat0) * math.radians(1) * 6_371_008.8
    up_m = alts - alts[0]
    return east_m, north_m, up_m, lat0, lon0


def _global_from_en(east_m: float, north_m: float, lat0: float, lon0: float) -> tuple[float, float]:
    k = math.cos(math.radians(lat0))
    lat = lat0 + north_m / (math.radians(1) * 6_371_008.8)
    lon = lon0 + east_m / (math.radians(1) * 6_371_008.8 * k)
    return lat, lon


class ChronosForecaster(Forecaster):
    """Zero-shot Chronos-2 forecaster.

    The position head predicts east/north/up displacement from the current fix.
    The ETA head predicts per-step ground speed and integrates along the path.
    """

    def __init__(self, network: Network, model_name: str = "autogluon/chronos-2-small"):
        self.network = network
        self.model_name = model_name
        self._pipeline: ChronosPipeline | None = None

    @property
    def name(self) -> str:
        return self.model_name

    def _load(self) -> ChronosPipeline:
        if self._pipeline is None:
            from chronos import ChronosPipeline

            self._pipeline = ChronosPipeline.from_pretrained(self.model_name)
        return self._pipeline

    def predict_position(
        self,
        track: list[tuple[float, float, float]],
        t_s: float,
        vehicle_type: VehicleType,
        horizons_s: list[int] | None = None,
    ) -> list[PositionPrediction]:
        if horizons_s is None:
            horizons_s = _POSITION_HORIZONS
        if len(track) < 2:
            # fallback to stationary prediction
            return [
                PositionPrediction(
                    horizon_s=h,
                    east_p10=-10.0,
                    east_p50=0.0,
                    east_p90=10.0,
                    north_p10=-10.0,
                    north_p50=0.0,
                    north_p90=10.0,
                    up_p10=-5.0,
                    up_p50=0.0,
                    up_p90=5.0,
                )
                for h in horizons_s
            ]

        east_m, north_m, up_m, lat0, lon0 = _to_local_en_series(track)
        # use recent 60 s of history for the position head
        history_len = min(60, len(east_m))
        context = np.stack([east_m[-history_len:], north_m[-history_len:], up_m[-history_len:]], axis=0)
        context = torch.tensor(context, dtype=torch.float32)
        pipeline = self._load()
        max_h = max(horizons_s)
        pred_len = max(_CHRONOS_PREDICTION_LENGTH, max_h)
        samples = pipeline.predict(context, prediction_length=pred_len, num_samples=20)  # (3, 20, pred_len)
        samples_np = samples.numpy()
        preds: list[PositionPrediction] = []
        for h in horizons_s:
            idx = h - 1
            e_samples = samples_np[0, :, idx]
            n_samples = samples_np[1, :, idx]
            u_samples = samples_np[2, :, idx]
            preds.append(
                PositionPrediction(
                    horizon_s=h,
                    east_p10=float(np.quantile(e_samples, 0.10)),
                    east_p50=float(np.quantile(e_samples, 0.50)),
                    east_p90=float(np.quantile(e_samples, 0.90)),
                    north_p10=float(np.quantile(n_samples, 0.10)),
                    north_p50=float(np.quantile(n_samples, 0.50)),
                    north_p90=float(np.quantile(n_samples, 0.90)),
                    up_p10=float(np.quantile(u_samples, 0.10)),
                    up_p50=float(np.quantile(u_samples, 0.50)),
                    up_p90=float(np.quantile(u_samples, 0.90)),
                )
            )
        return preds

    def predict_eta(
        self,
        path: list[str],
        origin_t_s: float,
        vehicle_type: VehicleType,
        track: list[tuple[float, float, float]] | None = None,
        current_speed_mps: float | None = None,
    ) -> EtaPrediction:
        """Predict per-node ETAs using Chronos on ground-speed samples.

        The model forecasts speed for the next ``prediction_length`` seconds;
        we integrate speed along the path and fall back to cruise speed once the
        horizon is exhausted.
        """
        cfg = vehicle_cfg(vehicle_type)
        speed = cfg.cruise_speed_mps
        if current_speed_mps is not None and current_speed_mps > 1.0:
            speed = 0.5 * (speed + current_speed_mps)

        if track is None or len(track) < 2:
            # No history: behave like CV with a slightly wider window.
            return self._cv_eta(path, origin_t_s, speed)

        # Compute 1-Hz ground-speed series from recent track.
        speeds: list[float] = []
        for i in range(1, len(track)):
            dist, _ = _haversine_course_m(track[i - 1][0], track[i - 1][1], track[i][0], track[i][1])
            speeds.append(dist)
        if not speeds:
            return self._cv_eta(path, origin_t_s, speed)
        context = torch.tensor([speeds[-60:]], dtype=torch.float32)
        pipeline = self._load()
        samples = pipeline.predict(context, prediction_length=_CHRONOS_PREDICTION_LENGTH, num_samples=20)
        # samples shape: (1, 20, pred_len)
        speed_quantiles = torch.quantile(
            samples[0], torch.tensor([0.1, 0.5, 0.9], dtype=samples.dtype), dim=0
        ).numpy()  # (3, pred_len)

        total_m = self.network.route_length_m(path)
        etas: list[NodeEta] = []
        cumul_m = 0.0
        for i, node in enumerate(path):
            if i > 0:
                cumul_m += self.network.edges[(path[i - 1], node)].length_m
            target_s = (cumul_m / max(1.0, total_m)) * (total_m / speed) if total_m > 0 else 0.0
            eta_p50 = self._integrate_to_time(speed_quantiles[1], target_s, speed, origin_t_s)
            eta_p10 = self._integrate_to_time(speed_quantiles[0], target_s, speed, origin_t_s)
            eta_p90 = self._integrate_to_time(speed_quantiles[2], target_s, speed, origin_t_s)
            etas.append(NodeEta(node=node, eta_p10=eta_p10, eta_p50=eta_p50, eta_p90=eta_p90))
        return EtaPrediction(path=path, origin_t_s=origin_t_s, etas=etas)

    def _cv_eta(self, path: list[str], origin_t_s: float, speed: float) -> EtaPrediction:
        etas: list[NodeEta] = []
        cumul_m = 0.0
        total_m = self.network.route_length_m(path)
        total_s = total_m / max(1.0, speed)
        spread_s = max(30.0, total_s * _ETA_TIME_SPREAD)
        for i, node in enumerate(path):
            if i > 0:
                cumul_m += self.network.edges[(path[i - 1], node)].length_m
            frac = cumul_m / max(1.0, total_m)
            eta = origin_t_s + frac * total_s
            etas.append(NodeEta(node=node, eta_p10=eta - spread_s, eta_p50=eta, eta_p90=eta + spread_s))
        return EtaPrediction(path=path, origin_t_s=origin_t_s, etas=etas)

    def _integrate_to_time(
        self,
        speed_profile: np.ndarray,
        target_s: float,
        cruise_speed: float,
        origin_t_s: float,
    ) -> float:
        """Integrate a 1-second speed profile until ``target_s`` metres are covered."""
        dist = 0.0
        for t, spd in enumerate(speed_profile):
            spd = max(1.0, float(spd))
            step = min(spd, target_s - dist)
            dist += step
            if dist >= target_s - 1e-6:
                return origin_t_s + float(t + 1)
        remaining_m = max(0.0, target_s - dist)
        return origin_t_s + float(len(speed_profile)) + remaining_m / max(1.0, cruise_speed)
