"""Off-route detection: learned tolerance instead of fixed bounds (PLAN E5.5, ARCHITECTURE §7.4).

The classical baseline (ARCHITECTURE §6) flags an aircraft more than a fixed 300 m from its
route. AeroLink replaces that fixed number with tolerances learned from data, and adds
checks the fixed bound cannot do. Every check runs on a 1 Hz track, is evaluated every
10 s (back-to-back seconds are not independent evidence) and needs 3 consecutive hits:

* **Route tolerance (learned):** a LightGBM quantile model (p99) of how far clean simulated
  flights stray from their route, given speed, distance to the next node and the turn
  there. The tolerance is tight on straight legs and wider before turns.
* **Schedule window:** at departure the ETA head gives a p10-p90 arrival window. An
  aircraft that can no longer reach its destination inside that window (widened to p99),
  even at cruise speed, is off its predicted path in time.
* **Band check:** 30 s ago the position head predicted where the aircraft would be now,
  with a calibrated p10-p90 box, widened here to p01-p99 (x1.81). Outside = unexpected
  manoeuvre.
* **Anomaly score:** an ``IsolationForest`` trained on residual features of clean real
  flights (cross-track and along-track error of that 30 s prediction, speed change,
  vertical-rate deviation). Below the 1st percentile of clean flights = anomalous.

AeroLink's rule: route tolerance OR schedule OR (band AND anomaly). The band and anomaly
checks catch sudden manoeuvres early but raise false alarms on their own, so they only
count when they agree.

When an aircraft is flagged, the route engine marks its flight ``off_route``, recalculates
options from its position, and spreads its possible positions over the nodes it can reach
(``reachable_mass``) so the traffic forecast accounts for it (see ``routing/aerolink.py``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
from sklearn.ensemble import IsolationForest

from aerolink.forecast.features import HISTORY_S, VEHICLE_CODE, cv_state, to_local, to_track_frame
from aerolink.network import Network

LEAD_S = 30  # the band is the 30 s position forecast made 30 s earlier
BAND_SCALE = 2.326 / 1.2816  # p10-p90 half-width -> p01-p99
CHECK_EVERY_S = 10
CONSECUTIVE = 3
FIXED_BOUND_M = 300.0
MODEL_DIR = Path(__file__).resolve().parents[3] / "models" / "offroute"
RESIDUAL_FEATURES = ["abs_cross_err_m", "along_err_m", "speed_change_mps", "vrate_dev_mps"]
TOLERANCE_FEATURES = ["vehicle", "speed_mps", "dist_next_node_m", "turn_at_next_deg", "alt_m"]


# --- geometry -------------------------------------------------------------------------------


def route_polyline(network: Network, path: list[str]) -> list[tuple[float, float]]:
    """The planned route as (lat, lon) points, following each corridor's real shape."""
    pts: list[tuple[float, float]] = []
    for a, b in zip(path, path[1:], strict=False):
        for lon, lat in network.edges[(a, b)].oriented_geometry(a):
            if not pts or pts[-1] != (lat, lon):
                pts.append((lat, lon))
    return pts


def cross_track_m(lat: float, lon: float, path_xy: list[tuple[float, float]]) -> float:
    """Distance from (lat, lon) to the nearest piece of a route given as (lat, lon) points."""
    ys = np.array([p[0] for p in path_xy])
    xs = np.array([p[1] for p in path_xy])
    e, n = to_local(ys, xs, lat, lon)
    ax, ay, bx, by = e[:-1], n[:-1], e[1:], n[1:]
    dx, dy = bx - ax, by - ay
    seg2 = np.maximum(dx * dx + dy * dy, 1e-9)
    u = np.clip(-(ax * dx + ay * dy) / seg2, 0.0, 1.0)
    return float(np.min(np.hypot(ax + u * dx, ay + u * dy))) if len(ax) else math.inf


def _speed(xyz: np.ndarray, i: int) -> float:
    if i < 1:
        return 0.0
    e, n = to_local(xyz[i - 1 : i + 1, 0], xyz[i - 1 : i + 1, 1], xyz[i, 0], xyz[i, 1])
    return float(math.hypot(e[1] - e[0], n[1] - n[0]))


def smoothed_speed(xyz: np.ndarray, i: int, window: int = 10) -> float:
    """Ground speed over the last ``window`` seconds (less noisy than one second)."""
    j = max(0, i - window)
    if i <= j:
        return 0.0
    e, n = to_local(xyz[[j, i], 0], xyz[[j, i], 1], xyz[i, 0], xyz[i, 1])
    return float(math.hypot(e[1] - e[0], n[1] - n[0]) / (i - j))


# --- checks over time -------------------------------------------------------------------------


def consecutive(flags: np.ndarray, n: int = CONSECUTIVE, clear: int = 2) -> np.ndarray:
    """Flag after ``n`` consecutive Trues; clear only after ``clear`` consecutive Falses.

    The hysteresis stops one wobbling aircraft from raising a new alarm every few checks.
    """
    run_on = run_off = 0
    state = False
    out = np.zeros(len(flags), dtype=bool)
    for k, f in enumerate(flags):
        run_on, run_off = (run_on + 1, 0) if f else (0, run_off + 1)
        if not state and run_on >= n:
            state = True
        elif state and run_off >= clear:
            state = False
        out[k] = state
    return out


def checked(n: int, idx: np.ndarray, hits: np.ndarray, t: np.ndarray) -> np.ndarray:
    """``consecutive`` on one check every ``CHECK_EVERY_S``; each result holds until the next check."""
    keep = ((t[idx] - t[0]) % CHECK_EVERY_S) == 0
    checks, state = idx[keep], consecutive(hits[keep])
    out = np.zeros(n, dtype=bool)
    for k, i in enumerate(checks):
        end = checks[k + 1] if k + 1 < len(checks) else n
        out[i:end] = state[k]
    return out


# --- band check and anomaly score (position head residuals) ------------------------------------


@dataclass
class TrackForecasts:
    """30 s-ahead predictions made at every second of a track that has 60 s of history."""

    t: np.ndarray  # (N,) time the prediction is FOR (made 30 s earlier)
    box: np.ndarray  # (N, 4) east p10, east p90, north p10, north p90 relative to the fix at t - 30
    p50: np.ndarray  # (N, 2) east, north p50 relative to the fix at t - 30
    course: np.ndarray  # (N,) course at t - 30


def forecast_track(forecaster, xyz: np.ndarray, t: np.ndarray, vehicle: str | None, graphs=None) -> TrackForecasts:
    """Batch-predict the 30 s position for every anchor along one track (1 Hz)."""
    anchors = list(range(HISTORY_S - 1, len(t) - LEAD_S))
    if not anchors:
        z = np.zeros((0,))
        return TrackForecasts(z, np.zeros((0, 4)), np.zeros((0, 2)), z)
    tracks = [xyz[i - HISTORY_S + 1 : i + 1] for i in anchors]
    if getattr(forecaster, "name", "") == "aerolink-gbm":
        preds = forecaster.predict_position_batch(
            tracks, [LEAD_S], vehicles=[vehicle] * len(tracks), graphs=[graphs[i] for i in anchors] if graphs else None
        )
    elif hasattr(forecaster, "predict_position_batch"):
        preds = forecaster.predict_position_batch(tracks, [LEAD_S])
    else:
        preds = [forecaster.predict_position([tuple(p) for p in tr], 0.0, "passenger", [LEAD_S]) for tr in tracks]
    p = [row[0] for row in preds]
    return TrackForecasts(
        t=t[np.array(anchors) + LEAD_S],
        box=np.array([[q.east_p10, q.east_p90, q.north_p10, q.north_p90] for q in p]),
        p50=np.array([[q.east_p50, q.north_p50] for q in p]),
        course=np.array([cv_state(tr)[1] for tr in tracks]),
    )


def outside_band(xyz: np.ndarray, t: np.ndarray, fc: TrackForecasts) -> tuple[np.ndarray, np.ndarray]:
    """(index into the track, True where the fix is outside the p01-p99 box)."""
    idx = np.searchsorted(t, fc.t)
    out = np.zeros(len(idx), dtype=bool)
    for k, i in enumerate(idx):
        j = i - LEAD_S
        e, n = to_local(xyz[i : i + 1, 0], xyz[i : i + 1, 1], xyz[j, 0], xyz[j, 1])
        e0, e1, n0, n1 = fc.box[k]
        ce, cn = fc.p50[k]
        e0, e1 = ce - (ce - e0) * BAND_SCALE, ce + (e1 - ce) * BAND_SCALE
        n0, n1 = cn - (cn - n0) * BAND_SCALE, cn + (n1 - cn) * BAND_SCALE
        out[k] = not (e0 <= float(e[0]) <= e1 and n0 <= float(n[0]) <= n1)
    return idx, out


def residual_features(xyz: np.ndarray, t: np.ndarray, fc: TrackForecasts) -> tuple[np.ndarray, np.ndarray]:
    """(index into the track, residual features) for every sample that has a forecast."""
    idx = np.searchsorted(t, fc.t)
    rows = []
    for k, i in enumerate(idx):
        j = i - LEAD_S
        e, n = to_local(xyz[i : i + 1, 0], xyz[i : i + 1, 1], xyz[j, 0], xyz[j, 1])
        along, cross = to_track_frame(float(e[0]) - fc.p50[k, 0], float(n[0]) - fc.p50[k, 1], float(fc.course[k]))
        vr = np.diff(xyz[j : i + 1, 2])
        rows.append(
            [
                abs(cross),
                along,
                _speed(xyz, int(i)) - _speed(xyz, int(j)),
                float(vr[-1] - vr.mean()) if len(vr) else 0.0,
            ]
        )
    return idx, np.array(rows, dtype=float).reshape(-1, len(RESIDUAL_FEATURES))


@dataclass
class AnomalyModel:
    forest: IsolationForest
    threshold: float  # decision_function below this = anomalous
    manifest: dict = field(default_factory=dict)

    def anomalous(self, X: np.ndarray) -> np.ndarray:
        if not len(X):
            return np.zeros(0, dtype=bool)
        return self.forest.decision_function(X) < self.threshold


def train_anomaly_model(X: np.ndarray, seed: int = 42, clean_quantile: float = 0.01) -> AnomalyModel:
    forest = IsolationForest(n_estimators=200, contamination="auto", random_state=seed).fit(X)
    threshold = float(np.quantile(forest.decision_function(X), clean_quantile))
    return AnomalyModel(forest, threshold, {"seed": seed, "clean_quantile": clean_quantile, "samples": len(X)})


# --- learned route tolerance ---------------------------------------------------------------------


@dataclass
class ToleranceModel:
    """p99 of the distance from the route that clean flights show, given their situation."""

    model: lgb.LGBMRegressor
    manifest: dict = field(default_factory=dict)

    def tolerance_m(self, X: np.ndarray) -> np.ndarray:
        return np.maximum(30.0, self.model.predict(X)) if len(X) else np.zeros(0)


def tolerance_row(vehicle: str | None, speed: float, graph: dict[str, float], alt_m: float) -> list[float]:
    return [
        VEHICLE_CODE.get(vehicle or "", math.nan),
        speed,
        graph.get("dist_next_node_m", math.nan),
        graph.get("turn_at_next_deg", math.nan),
        alt_m,
    ]


def train_tolerance_model(X: np.ndarray, cross_m: np.ndarray, seed: int = 42) -> ToleranceModel:
    model = lgb.LGBMRegressor(
        objective="quantile",
        alpha=0.99,
        n_estimators=200,
        learning_rate=0.05,
        num_leaves=15,
        min_child_samples=50,
        random_state=seed,
        verbose=-1,
    )
    model.fit(X, cross_m)
    return ToleranceModel(model, {"seed": seed, "quantile": 0.99, "samples": len(X)})


# --- everything together --------------------------------------------------------------------------


@dataclass
class OffRouteModels:
    tolerance: ToleranceModel
    anomaly: AnomalyModel

    def save(self, model_dir: Path = MODEL_DIR) -> Path:
        model_dir.mkdir(parents=True, exist_ok=True)
        path = model_dir / "offroute.joblib"
        joblib.dump(self, path, compress=3)
        return path

    @staticmethod
    def load(model_dir: Path = MODEL_DIR) -> OffRouteModels:
        return joblib.load(model_dir / "offroute.joblib")


@dataclass
class Detection:
    """Per track sample: True while each check has the aircraft flagged."""

    fixed: np.ndarray  # classical: > 300 m from the route
    tolerance: np.ndarray
    schedule: np.ndarray
    band: np.ndarray
    anomaly: np.ndarray

    @property
    def ai(self) -> np.ndarray:
        return self.tolerance | self.schedule | (self.band & self.anomaly)


def detect(
    xyz: np.ndarray,
    t: np.ndarray,
    route: list[tuple[float, float]],
    graphs: list[dict[str, float]],
    forecaster,
    models: OffRouteModels,
    vehicle: str | None,
    cruise_mps: float,
    arrival_window: tuple[float, float] | None = None,
) -> Detection:
    """Run every check over one flight's 1 Hz track.

    ``route`` is the planned route polyline, ``graphs`` the graph context at each sample
    (``forecast.simdata.graph_context``), ``arrival_window`` the ETA head's (p50, p90)
    arrival time at the destination, predicted at departure.
    """
    n = len(t)
    all_idx = np.arange(n)
    cross = np.array([cross_track_m(xyz[i, 0], xyz[i, 1], route) for i in range(n)])
    fixed = consecutive(cross > FIXED_BOUND_M, n=1, clear=1)

    speeds = np.array([smoothed_speed(xyz, i) for i in range(n)])
    X = np.array([tolerance_row(vehicle, speeds[i], graphs[i], xyz[i, 2]) for i in range(n)])
    tolerance = checked(n, all_idx, cross > models.tolerance.tolerance_m(X), t)

    schedule = np.zeros(n, dtype=bool)
    if arrival_window is not None:
        p50, p90 = arrival_window
        latest = p90 + (p90 - p50) * BAND_SCALE
        remaining = np.array([g.get("remaining_route_m", 0.0) for g in graphs])
        schedule = checked(n, all_idx, t + remaining / max(1.0, cruise_mps) > latest, t)

    band = np.zeros(n, dtype=bool)
    anom = np.zeros(n, dtype=bool)
    fc = forecast_track(forecaster, xyz, t, vehicle, graphs)
    if len(fc.t):
        idx, out = outside_band(xyz, t, fc)
        band = checked(n, idx, out, t)
        idx, R = residual_features(xyz, t, fc)
        anom = checked(n, idx, models.anomaly.anomalous(R), t)
    return Detection(fixed, tolerance, schedule, band, anom)


def reachable_mass(
    network: Network, lat: float, lon: float, speed_mps: float, t_s: float, horizon_s: float = 600.0
) -> list[tuple[float, str, float, float, float]]:
    """Spread an off-route aircraft over every node it can reach within ``horizon_s``.

    Returns traffic-forecast entries (probability, node, eta p10, p50, p90): equal
    probability per reachable node (straight-line flight at its current speed), so the
    total mass is one aircraft.
    """
    speed = max(10.0, speed_mps)
    reach = []
    for nid, props in network.nodes.items():
        e, n = to_local(np.array([props["lat"]]), np.array([props["lon"]]), lat, lon)
        eta = math.hypot(float(e[0]), float(n[0])) / speed
        if eta <= horizon_s:
            reach.append((nid, eta))
    if not reach:
        return []
    p = 1.0 / len(reach)
    return [(p, nid, t_s + 0.8 * eta, t_s + eta, t_s + 1.3 * eta + 30.0) for nid, eta in reach]
