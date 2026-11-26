"""Training samples from simulated flights (PLAN E5.3).

Runs the scenarios with several seeds and turns the noise-free per-second state
(``SimulationResult.truth``) into:

* position samples, the same shape as the real ones (``realdata.samples.PositionSamples``),
  plus graph context (distance to the next node, turn there, route left);
* ETA samples: features and the true time to reach the destination, for every 30 s of
  every completed flight. "Reach" means arriving at the site (holding or landing starts),
  so pad waits are left to the capacity model and not counted twice.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

import numpy as np

from aerolink.contracts import VehicleType
from aerolink.forecast.features import (
    ETA_FEATURES,
    HISTORY_S,
    POSITION_HORIZONS_S,
    cv_speed,
    eta_row,
    to_local,
)
from aerolink.network import Network
from aerolink.realdata.samples import PositionSamples
from aerolink.sim import load_scenario, run_simulation
from aerolink.sim.runner import SimulationResult
from aerolink.vehicle import vehicle_cfg

AIRBORNE = {"climbing", "cruise", "descending", "holding", "landing"}
AT_SITE = {"holding", "landing"}


@dataclass
class EtaSamples:
    X: np.ndarray  # (N, len(ETA_FEATURES))
    y_log_ratio: np.ndarray  # (N,) log(actual remaining time / CV remaining time)
    cv_remaining_s: np.ndarray  # (N,)
    actual_remaining_s: np.ndarray  # (N,)
    group: np.ndarray  # (N,) flight id, for splitting

    def subset(self, mask: np.ndarray) -> EtaSamples:
        return EtaSamples(
            self.X[mask],
            self.y_log_ratio[mask],
            self.cv_remaining_s[mask],
            self.actual_remaining_s[mask],
            self.group[mask],
        )

    @staticmethod
    def concat(parts: list[EtaSamples]) -> EtaSamples:
        if not parts:
            return EtaSamples(np.zeros((0, len(ETA_FEATURES))), np.zeros(0), np.zeros(0), np.zeros(0), np.zeros(0, str))
        return EtaSamples(*(np.concatenate([getattr(p, f) for p in parts]) for f in EtaSamples.__dataclass_fields__))


def run_scenarios(network: Network, scenario_files: list[Path], seeds: list[int]) -> list[SimulationResult]:
    return [run_simulation(network, load_scenario(f), seed=s) for f in scenario_files for s in seeds]


def is_junction(network: Network, node: str) -> bool:
    return network.graph.degree(node) >= 3


def _bearing(network: Network, a: str, b: str) -> float:
    na, nb = network.nodes[a], network.nodes[b]
    e, n = to_local(np.array([nb["lat"]]), np.array([nb["lon"]]), na["lat"], na["lon"])
    return math.degrees(math.atan2(float(e[0]), float(n[0]))) % 360.0


def graph_context(network: Network, path: list[str], lat: float, lon: float) -> tuple[dict[str, float], int]:
    """Graph features for an aircraft at (lat, lon) flying ``path``; also the index of the next node."""
    xs = np.array([network.nodes[n]["lon"] for n in path])
    ys = np.array([network.nodes[n]["lat"] for n in path])
    e, n = to_local(ys, xs, lat, lon)  # nodes relative to the aircraft
    best_i, best_d = 1, math.inf
    for i in range(1, len(path)):  # nearest leg (straight line between nodes)
        ax, ay, bx, by = e[i - 1], n[i - 1], e[i], n[i]
        dx, dy = bx - ax, by - ay
        u = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
        d = math.hypot(ax + u * dx, ay + u * dy)
        if d < best_d:
            best_i, best_d = i, d
    dist_next = float(math.hypot(e[best_i], n[best_i]))
    turn = 0.0
    if best_i + 1 < len(path):
        a = _bearing(network, path[best_i - 1], path[best_i])
        b = _bearing(network, path[best_i], path[best_i + 1])
        turn = abs((b - a + 180.0) % 360.0 - 180.0)
    remaining = dist_next + network.route_length_m(path[best_i:])
    return {"dist_next_node_m": dist_next, "turn_at_next_deg": turn, "remaining_route_m": remaining}, best_i


def _path_history(result: SimulationResult) -> dict[str, list[tuple[float, list[str] | None]]]:
    """Per aircraft: (time, current path or None) whenever the route changes."""
    hist: dict[str, list[tuple[float, list[str] | None]]] = {}
    for e in result.events:
        aid = e.get("aircraft_id")
        if aid is None:
            continue
        if e["kind"] == "departure":
            hist.setdefault(aid, []).append((e["t"], list(e["path"])))
        elif e["kind"] == "suggestion" and e.get("accepted"):
            hist.setdefault(aid, []).append((e["t"], list(e["path"])))
        elif e["kind"] == "arrival":
            hist.setdefault(aid, []).append((e["t"], None))
    return hist


def _path_at(changes: list[tuple[float, list[str] | None]], t: float) -> list[str] | None:
    current = None
    for tc, p in changes:
        if tc > t:
            break
        current = p
    return current


def _tracks(result: SimulationResult) -> dict[str, dict[str, np.ndarray]]:
    by: dict[str, list[dict]] = {}
    for r in result.truth:
        by.setdefault(r["aircraft_id"], []).append(r)
    out = {}
    for aid, rows in by.items():
        rows.sort(key=lambda r: r["t"])
        out[aid] = {
            "t": np.array([r["t"] for r in rows]),
            "xyz": np.array([[r["lat"], r["lon"], r["alt_msl_m"]] for r in rows]),
            "speed": np.array([r["velocity_mps"] for r in rows]),
            "status": np.array([r["status"] for r in rows]),
        }
    return out


def _vehicle(result: SimulationResult, aid: str) -> str | None:
    ac = result.final_world.aircraft.get(aid)
    return ac.vehicle_type if ac is not None else None


def _start_hour(result: SimulationResult) -> float:
    start = datetime.fromisoformat(result.scenario.start_time)
    return start.hour + start.minute / 60.0


def add_noise(xyz: np.ndarray, rng: np.random.Generator, sigma_m: float) -> np.ndarray:
    """Horizontal GPS-like noise (metres), like the simulator's telemetry noise."""
    out = xyz.copy()
    out[:, 0] += np.degrees(rng.normal(0.0, sigma_m, len(xyz)) / 6_371_008.8)
    out[:, 1] += np.degrees(rng.normal(0.0, sigma_m, len(xyz)) / (6_371_008.8 * math.cos(math.radians(xyz[0, 0]))))
    return out


def position_samples(
    results: list[SimulationResult], network: Network, stride_s: int = 30, noise_m: float = 5.0, seed: int = 0
) -> tuple[PositionSamples, list[dict[str, float]]]:
    """Position samples from every airborne stretch of every simulated aircraft (1 s steps).

    The truth is noise-free, so ``noise_m`` of position noise is added (5 m, as in the
    scenarios' telemetry), like a real receiver would see it.
    """
    horizons = POSITION_HORIZONS_S
    rng = np.random.default_rng(seed)
    rows: dict[str, list] = {k: [] for k in ["seg", "t", "veh", "hour", "hist", "fut", "graph"]}
    for res in results:
        assert res.scenario.dt_s == 1.0, "position samples assume 1 s steps"
        paths = _path_history(res)
        h0 = _start_hour(res)
        for aid, tr in _tracks(res).items():
            if noise_m > 0:
                tr["xyz"] = add_noise(tr["xyz"], rng, noise_m)
            air = np.isin(tr["status"], list(AIRBORNE | {"background"}))
            starts = np.flatnonzero(air & ~np.r_[False, air[:-1]])
            ends = np.flatnonzero(air & ~np.r_[air[1:], False]) + 1
            for a, b in zip(starts, ends, strict=True):
                for i in range(a + HISTORY_S - 1, b - min(horizons), stride_s):
                    fut = np.full((len(horizons), 3), np.nan)
                    for j, h in enumerate(horizons):
                        if i + h < b:
                            fut[j] = tr["xyz"][i + h]
                    path = _path_at(paths.get(aid, []), float(tr["t"][i]))
                    graph: dict[str, float] = {}
                    if path and len(path) >= 2:
                        graph, _ = graph_context(network, path, float(tr["xyz"][i, 0]), float(tr["xyz"][i, 1]))
                    rows["seg"].append(f"{res.scenario.name}-{res.seed}-{aid}-{a}")
                    rows["t"].append(float(tr["t"][i]))
                    rows["veh"].append(_vehicle(res, aid) or "")
                    rows["hour"].append((h0 + float(tr["t"][i]) / 3600.0) % 24.0)
                    rows["hist"].append(tr["xyz"][i - HISTORY_S + 1 : i + 1])
                    rows["fut"].append(fut)
                    rows["graph"].append(graph)
    n = len(rows["t"])
    samples = PositionSamples(
        day=np.array(["sim"] * n),
        segment_id=np.array(rows["seg"]),
        t=np.array(rows["t"]),
        vehicle=np.array(rows["veh"]),
        hour=np.array(rows["hour"]),
        history=np.stack(rows["hist"]) if n else np.zeros((0, HISTORY_S, 3)),
        future=np.stack(rows["fut"]) if n else np.zeros((0, len(horizons), 3)),
        horizons_s=horizons,
    )
    return samples, rows["graph"]


def eta_samples(results: list[SimulationResult], network: Network, stride_s: int = 30) -> EtaSamples:
    """ETA samples from every completed, undiverted simulated flight."""
    X, y, cv_s, act_s, grp = [], [], [], [], []
    for res in results:
        tracks = _tracks(res)
        paths = _path_history(res)
        h0 = _start_hour(res)
        departures = {e["flight_id"]: e for e in res.events if e["kind"] == "departure"}
        active: list[tuple[float, float, str]] = []  # (dep t, arr t, aircraft) for traffic-ahead
        arrivals = [e for e in res.events if e["kind"] == "arrival" and e.get("flight_id") in departures]
        for arr in arrivals:
            active.append((departures[arr["flight_id"]]["t"], arr["t"], arr["aircraft_id"]))
        for arr in arrivals:
            if arr.get("diverted") or arr.get("destination_changed"):
                continue
            dep = departures[arr["flight_id"]]
            aid, vehicle = arr["aircraft_id"], _vehicle(res, arr["aircraft_id"])
            if vehicle is None:
                continue
            cruise = vehicle_cfg(cast(VehicleType, vehicle)).cruise_speed_mps
            tr = tracks[aid]
            in_flight = (tr["t"] > dep["t"]) & (tr["t"] <= arr["t"])
            at_site = np.flatnonzero(in_flight & np.isin(tr["status"], list(AT_SITE)))
            reach_t = float(tr["t"][at_site[0]]) if len(at_site) else float(arr["t"])
            idx = np.flatnonzero(in_flight & (tr["t"] <= reach_t - stride_s))
            for i in idx[::stride_s]:
                t = float(tr["t"][i])
                path = _path_at(paths.get(aid, []), t)
                if not path or path[-1] != arr["node"]:
                    continue
                graph, nxt = graph_context(network, path, float(tr["xyz"][i, 0]), float(tr["xyz"][i, 1]))
                remaining_nodes = path[nxt:]
                speed = float(tr["speed"][i])
                cv_rem = graph["remaining_route_m"] / cv_speed(cruise, speed)
                traffic = 0
                for d_t, a_t, other in active:
                    if other == aid or not (d_t <= t < a_t):
                        continue
                    op = _path_at(paths.get(other, []), t)
                    if op and set(op) & set(remaining_nodes):
                        traffic += 1
                X.append(
                    eta_row(
                        vehicle,
                        (h0 + t / 3600.0) % 24.0,
                        graph["remaining_route_m"],
                        cv_rem,
                        speed,
                        cruise,
                        len(remaining_nodes),
                        sum(is_junction(network, n) for n in remaining_nodes[:-1]),
                        float(traffic),
                    )
                )
                actual = max(1.0, reach_t - t)
                y.append(math.log(actual / max(1.0, cv_rem)))
                cv_s.append(cv_rem)
                act_s.append(actual)
                grp.append(f"{res.scenario.name}-{res.seed}-{arr['flight_id']}")
    return EtaSamples(
        np.array(X, dtype=float).reshape(-1, len(ETA_FEATURES)),
        np.array(y),
        np.array(cv_s),
        np.array(act_s),
        np.array(grp, dtype=str),
    )