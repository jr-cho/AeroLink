"""Shared geometry and feature builders for the trained forecaster (PLAN E5.3).

Positions are turned into a local east/north/up frame in metres (flat-earth around the
current fix; error < 0.1% over the 300 s horizons used here). The same helpers are used
for real flights (adsb.lol) and simulated ones, so one model trains on both.

Position-head targets are residuals over constant velocity in the aircraft's own frame:
``along`` (ahead of / behind the CV point), ``cross`` (right / left of the CV line) and
``up``. Turning and speed changes show up there directly, which is easier to learn than
east/north residuals that depend on the heading.
"""

from __future__ import annotations

import math

import numpy as np

from aerolink.contracts import VehicleType

EARTH_R_M = 6_371_008.8
POSITION_HORIZONS_S = [30, 60, 120, 300]
HISTORY_S = 60  # seconds of 1 Hz history a position sample carries

# Vehicle classes as a number for LightGBM. Real data: A1 light, A7 rotorcraft.
VEHICLE_CODE = {"light": 0.0, "rotor": 1.0, "passenger": 2.0, "medical": 3.0}
CATEGORY_TO_VEHICLE = {"A1": "light", "A7": "rotor"}

POSITION_FEATURES = [
    "horizon_s",
    "vehicle",
    "hour",
    "speed_mps",
    "speed_mean_30",
    "speed_std_30",
    "accel_10",
    "accel_30",
    "turn_rate_10",
    "turn_rate_30",
    "vrate_mps",
    "vrate_mean_30",
    "alt_m",
    "speed_bias",
    # Graph context: NaN for real flights (they don't fly AeroLink corridors).
    "dist_next_node_m",
    "turn_at_next_deg",
    "remaining_route_m",
]


def to_local(lat: np.ndarray, lon: np.ndarray, lat0: float, lon0: float) -> tuple[np.ndarray, np.ndarray]:
    """(east_m, north_m) of points relative to (lat0, lon0)."""
    k = math.cos(math.radians(lat0))
    east = np.radians(np.asarray(lon, dtype=float) - lon0) * EARTH_R_M * k
    north = np.radians(np.asarray(lat, dtype=float) - lat0) * EARTH_R_M
    return east, north


def from_local(east: float, north: float, lat0: float, lon0: float) -> tuple[float, float]:
    k = math.cos(math.radians(lat0))
    return lat0 + math.degrees(north / EARTH_R_M), lon0 + math.degrees(east / (EARTH_R_M * k))


def _course(de: np.ndarray, dn: np.ndarray) -> np.ndarray:
    return np.degrees(np.arctan2(de, dn)) % 360.0


def _wrap180(a: np.ndarray | float) -> np.ndarray | float:
    return (np.asarray(a) + 180.0) % 360.0 - 180.0


def cv_state(history: np.ndarray) -> tuple[float, float]:
    """(speed m/s, course deg) from the last two 1 Hz fixes, exactly like ``CVForecaster``."""
    lat0, lon0 = float(history[-1, 0]), float(history[-1, 1])
    e, n = to_local(history[-2:, 0], history[-2:, 1], lat0, lon0)
    de, dn = float(e[1] - e[0]), float(n[1] - n[0])
    return math.hypot(de, dn), float(_course(np.array(de), np.array(dn)))


def kinematics(history: np.ndarray) -> dict[str, float]:
    """Kinematic features from a (T, 3) array of (lat, lon, alt_m), 1 Hz, oldest first."""
    lat0, lon0 = float(history[-1, 0]), float(history[-1, 1])
    e, n = to_local(history[:, 0], history[:, 1], lat0, lon0)
    de, dn = np.diff(e), np.diff(n)
    speed = np.hypot(de, dn)
    course = _course(de, dn)
    up = history[:, 2]
    vrate = np.diff(up)

    def turn_rate(window: int) -> float:
        w = min(window, len(course) - 1)
        if w < 1:
            return 0.0
        return float(np.sum(_wrap180(np.diff(course[-w - 1 :]))) / w)

    def accel(window: int) -> float:
        w = min(window, len(speed) - 1)
        return float((speed[-1] - speed[-1 - w]) / w) if w >= 1 else 0.0

    last30 = speed[-30:]
    return {
        "speed_mps": float(speed[-1]),
        "speed_mean_30": float(last30.mean()),
        "speed_std_30": float(last30.std()),
        "accel_10": accel(10),
        "accel_30": accel(30),
        "turn_rate_10": turn_rate(10),
        "turn_rate_30": turn_rate(30),
        "vrate_mps": float(vrate[-1]),
        "vrate_mean_30": float(vrate[-30:].mean()),
        "alt_m": float(up[-1]),
        "speed_bias": float(speed[-1] / max(1.0, speed.mean())),
    }


def cv_point(history: np.ndarray, horizon_s: float) -> tuple[float, float]:
    """CV-predicted (east, north) displacement from the last fix."""
    speed, course = cv_state(history)
    c = math.radians(course)
    return speed * horizon_s * math.sin(c), speed * horizon_s * math.cos(c)


def to_track_frame(de: float, dn: float, course_deg: float) -> tuple[float, float]:
    """East/north offset -> (along, cross) in a frame aligned with the course (cross > 0 = right)."""
    c = math.radians(course_deg)
    return de * math.sin(c) + dn * math.cos(c), de * math.cos(c) - dn * math.sin(c)


def from_track_frame(along: float, cross: float, course_deg: float) -> tuple[float, float]:
    c = math.radians(course_deg)
    return along * math.sin(c) + cross * math.cos(c), along * math.cos(c) - cross * math.sin(c)


def position_row(
    history: np.ndarray,
    horizon_s: int,
    vehicle: str | None,
    hour: float | None,
    graph: dict[str, float] | None = None,
) -> list[float]:
    """One feature row (``POSITION_FEATURES`` order) for one horizon."""
    k = kinematics(history)
    g = graph or {}
    values = {
        "horizon_s": float(horizon_s),
        "vehicle": VEHICLE_CODE.get(vehicle or "", math.nan),
        "hour": math.nan if hour is None else float(hour),
        **k,
        "dist_next_node_m": g.get("dist_next_node_m", math.nan),
        "turn_at_next_deg": g.get("turn_at_next_deg", math.nan),
        "remaining_route_m": g.get("remaining_route_m", math.nan),
    }
    return [values[f] for f in POSITION_FEATURES]


def position_targets(history: np.ndarray, future: np.ndarray, horizon_s: int) -> tuple[float, float, float]:
    """Residual (along, cross, up) of the true position at ``horizon_s`` over the CV point.

    ``future`` is the true (lat, lon, alt_m) at the horizon.
    """
    lat0, lon0 = float(history[-1, 0]), float(history[-1, 1])
    te, tn = to_local(np.array([future[0]]), np.array([future[1]]), lat0, lon0)
    ce, cn = cv_point(history, horizon_s)
    _, course = cv_state(history)
    along, cross = to_track_frame(float(te[0]) - ce, float(tn[0]) - cn, course)
    return along, cross, float(future[2] - history[-1, 2])


# --- ETA head -----------------------------------------------------------------------------

ETA_FEATURES = [
    "vehicle",
    "hour",
    "remaining_m",
    "cv_remaining_s",
    "speed_mps",
    "speed_ratio",
    "nodes_left",
    "merges_left",
    "from_rest",  # 1 = a departure query (no observed speed yet), 0 = an in-flight one
    "traffic_ahead",
]


def eta_row(
    vehicle: VehicleType,
    hour: float | None,
    remaining_m: float,
    cv_remaining_s: float,
    speed_mps: float,
    cruise_mps: float,
    nodes_left: int,
    merges_left: int,
    traffic_ahead: float,
    from_rest: bool = False,
) -> list[float]:
    values = {
        "vehicle": VEHICLE_CODE.get(vehicle, math.nan),
        "hour": math.nan if hour is None else float(hour),
        "remaining_m": remaining_m,
        "cv_remaining_s": cv_remaining_s,
        "speed_mps": speed_mps,
        "speed_ratio": speed_mps / max(1.0, cruise_mps),
        "nodes_left": float(nodes_left),
        "merges_left": float(merges_left),
        "from_rest": 1.0 if from_rest else 0.0,
        "traffic_ahead": traffic_ahead,
    }
    return [values[f] for f in ETA_FEATURES]


def cv_speed(cruise_mps: float, current_speed_mps: float | None) -> float:
    """The speed ``CVForecaster.predict_eta`` uses: cruise, blended with the observed speed."""
    if current_speed_mps is not None and current_speed_mps > 1.0:
        return 0.5 * (cruise_mps + current_speed_mps)
    return cruise_mps


def eta_query_row(
    network,
    path: list[str],
    vehicle: VehicleType,
    hour: float | None,
    traffic_ahead: float,
    current_speed_mps: float | None = None,
) -> tuple[list[float], float]:
    """The ETA feature row for a whole-path query, and the speed the CV time assumes.

    One function for serving (``GBMForecaster.predict_eta``) and for training the
    departure samples, so the two cannot drift apart. With no observed speed this is a
    departure query: cruise speed is assumed and ``from_rest`` is set, because the real
    flight then also pays for accelerating from rest, climb-out, speed caps and metering.
    """
    from aerolink.vehicle import vehicle_cfg

    cruise = vehicle_cfg(vehicle).cruise_speed_mps
    moving = current_speed_mps is not None and current_speed_mps > 1.0
    speed = cv_speed(cruise, current_speed_mps)
    total_m = network.route_length_m(path)
    merges = sum(network.graph.degree(n) >= 3 for n in path[1:-1])
    row = eta_row(
        vehicle,
        hour,
        total_m,
        total_m / max(1.0, speed),
        current_speed_mps if moving else cruise,
        cruise,
        len(path),
        merges,
        traffic_ahead,
        from_rest=not moving,
    )
    return row, speed
