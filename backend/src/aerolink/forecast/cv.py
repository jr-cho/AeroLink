"""Constant-velocity / constant-cruise-speed forecaster (PLAN E4.1 baseline)."""

from __future__ import annotations

import math

from aerolink.contracts import VehicleType
from aerolink.forecast.interface import EtaPrediction, Forecaster, NodeEta, PositionPrediction
from aerolink.network import Network
from aerolink.vehicle import vehicle_cfg

_POSITION_HORIZONS = [30, 60, 120, 300]
_POSITION_SPREAD_M = {30: 20.0, 60: 60.0, 120: 150.0, 300: 400.0}

# Long-haul ETA horizons used by the route engine (minutes -> seconds).
_ETA_HORIZONS_S = [300, 600, 1_200, 1_800, 2_700]

# Fixed uncertainty factor on cruise time for ETA windows (p10/p90).
_ETA_TIME_SPREAD = 0.08


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_008.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class CVForecaster(Forecaster):
    """Non-AI baseline: constant velocity over the last points, cruise speed for ETAs."""

    def __init__(self, network: Network):
        self.network = network

    @property
    def name(self) -> str:
        return "cv"

    def predict_position(
        self,
        track: list[tuple[float, float, float]],
        t_s: float,
        vehicle_type: VehicleType,
        horizons_s: list[int] | None = None,
    ) -> list[PositionPrediction]:
        if horizons_s is None:
            horizons_s = _POSITION_HORIZONS
        speed_mps, course_deg = self._latest_speed_and_course(track)
        preds: list[PositionPrediction] = []
        for h in horizons_s:
            spread = _POSITION_SPREAD_M.get(h, speed_mps * float(h) * 0.25)
            de = speed_mps * float(h) * math.sin(math.radians(course_deg))
            dn = speed_mps * float(h) * math.cos(math.radians(course_deg))
            preds.append(
                PositionPrediction(
                    horizon_s=h,
                    east_p10=de - spread,
                    east_p50=de,
                    east_p90=de + spread,
                    north_p10=dn - spread,
                    north_p50=dn,
                    north_p90=dn + spread,
                    up_p10=-10.0,
                    up_p50=0.0,
                    up_p90=10.0,
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
        speed = vehicle_cfg(vehicle_type).cruise_speed_mps
        if current_speed_mps is not None and current_speed_mps > 1.0:
            # blend observed speed with nominal cruise speed to avoid idle speeds
            speed = 0.5 * (speed + current_speed_mps)
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

    def _latest_speed_and_course(self, track: list[tuple[float, float, float]]) -> tuple[float, float]:
        if len(track) < 2:
            return 0.0, 0.0
        *_, prev, cur = track
        dt = 1.0  # 1 Hz samples
        dist = _haversine_m(prev[0], prev[1], cur[0], cur[1])
        speed = dist / max(0.1, dt)
        lat1, lon1, lat2, lon2 = prev[0], prev[1], cur[0], cur[1]
        dlon = math.radians(lon2 - lon1)
        lat1r, lat2r = math.radians(lat1), math.radians(lat2)
        x = math.sin(dlon) * math.cos(lat2r)
        y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
        course = (math.degrees(math.atan2(x, y)) + 360.0) % 360.0
        return speed, course
