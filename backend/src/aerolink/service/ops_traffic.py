"""Predicted conflicts for the ATC dashboard (PLAN E6.10): pure functions, no database, MQTT or clock.

A conflict is a *prediction*, never an instruction: ATC reads it and, if it agrees, speaks to the pilot by radio.

What counts as a conflict between two planned flights (``route_conflicts``):

* **junction / pad**: both reach the same node and their arrival windows overlap. A node is a *pad* when it is
  either flight's destination or origin (a landing site), otherwise a *junction*.
* **opposing**: they use the same corridor edge in opposite directions and their occupancy windows overlap.

Windows are the planned p50 time of each node widened by the same spread the prediction cones use (8 % of the time
left, at least 30 s; ``Planner.cones``), so a conflict shows up while the aircraft are still apart. Severity is
``high`` when the two p50 times are closer than the simulator's junction spacing
(``SeparationConfig.spacing_junction_s``, 60 s), ``medium`` when only the windows overlap. One conflict is
reported per pair of flights (their earliest meeting; other meeting places are listed under ``locations``).

``adsb_conflicts`` projects each live ADS-B aircraft straight ahead (constant velocity, at most 10 min) and flags a
route whose predicted position comes within ``SeparationConfig.follow_target_distance_m`` (``medium``) or
``min_separation_m`` (``high``) horizontally while the altitudes are within ``min_vertical_separation_m`` of the
corridor's altitude band. An ADS-B aircraft without an altitude is treated as possibly in the band.

Ranking: severity first (high before medium), then the sooner conflict. Anything involving a medical flight is
flagged ``medical`` (medical flights go first, ARCHITECTURE §1.5).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from aerolink.service.live import distance_m
from aerolink.service.planner import Planner
from aerolink.sim.state import FlightPlan
from aerolink.sim.world_config import SeparationConfig

SEP = SeparationConfig()
FT_TO_M = 0.3048
KT_TO_MPS = 0.514444
ADSB_HORIZON_S = 600.0
ADSB_STEP_S = 15.0
ADSB_MAX_AGE_S = 60.0  # a live aircraft not heard from for a minute is not projected
BUNCHING_SHOWN = 0.05  # the route engine mentions bunching risk above this (A1._reason)
MIN_SPREAD_S = 30.0
SPREAD_FRACTION = 0.08
SEVERITY_RANK = {"high": 0, "medium": 1}


@dataclass(frozen=True)
class Flight:
    """What the conflict maths needs of a planned flight."""

    flight_id: str
    aircraft_id: str
    vehicle_type: str
    source: str
    status: str
    plan: FlightPlan


def window(p50: float, t0: float) -> tuple[float, float, float]:
    """(p10, p50, p90) of a planned time, as wide as the cones' spread."""
    spread = max(MIN_SPREAD_S, (p50 - t0) * SPREAD_FRACTION)
    return p50 - spread, p50, p50 + spread


def node_windows(plan: FlightPlan, t0: float) -> dict[str, tuple[float, float, float]]:
    """Arrival windows per node still ahead (nodes a flight has passed by more than its spread are dropped)."""
    out: dict[str, tuple[float, float, float]] = {}
    for node in plan.path:
        eta = (
            plan.departure_time if node == plan.path[0] and node not in plan.eta_by_node else plan.eta_by_node.get(node)
        )
        if eta is None:
            continue
        w = window(eta, t0)
        if w[2] >= t0:
            out[node] = w
    return out


def _ref(f: Flight) -> dict[str, str]:
    return {"flight_id": f.flight_id, "aircraft_id": f.aircraft_id, "vehicle_type": f.vehicle_type, "source": f.source}


def _overlap(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float] | None:
    lo, hi = max(a[0], b[0]), min(a[2], b[2])
    return (lo, hi) if lo <= hi else None


def _severity(gap_s: float) -> str:
    return "high" if gap_s < SEP.spacing_junction_s else "medium"


def pair_events(a: Flight, b: Flight, t0: float, horizon_s: float) -> list[dict[str, Any]]:
    """Every place and time two flights may meet inside the horizon."""
    wa, wb = node_windows(a.plan, t0), node_windows(b.plan, t0)
    events: list[dict[str, Any]] = []
    for node in wa.keys() & wb.keys():
        ov = _overlap(wa[node], wb[node])
        if ov is None:
            continue
        t = (ov[0] + ov[1]) / 2
        if t - t0 > horizon_s:
            continue
        site = node in {a.plan.path[0], a.plan.destination_node, b.plan.path[0], b.plan.destination_node}
        gap = abs(wa[node][1] - wb[node][1])
        events.append(
            {"kind": "pad" if site else "junction", "node": node, "t": max(t, t0), "overlap_s": ov[1] - ov[0],
             "gap_s": gap, "severity": _severity(gap)}
        )  # fmt: skip
    edges_a = {(u, v) for u, v in zip(a.plan.path, a.plan.path[1:], strict=False)}
    for u, v in zip(b.plan.path, b.plan.path[1:], strict=False):
        if (v, u) not in edges_a or u not in wb or v not in wb or u not in wa or v not in wa:
            continue
        # b flies u -> v and a flies v -> u: each occupies the edge from its first to its last node
        occ_a = (wa[v][0], (wa[v][1] + wa[u][1]) / 2, wa[u][2])
        occ_b = (wb[u][0], (wb[u][1] + wb[v][1]) / 2, wb[v][2])
        ov = _overlap(occ_a, occ_b)
        if ov is None:
            continue
        t = (ov[0] + ov[1]) / 2
        if t - t0 > horizon_s:
            continue
        gap = abs(occ_a[1] - occ_b[1])
        events.append(
            {"kind": "opposing", "edge": [u, v], "t": max(t, t0), "overlap_s": ov[1] - ov[0], "gap_s": gap,
             "severity": _severity(gap)}
        )  # fmt: skip
    return events


def route_conflicts(
    flights: list[Flight], t0: float, horizon_s: float, risk: dict[str, float] | None = None
) -> list[dict[str, Any]]:
    """Conflicts between planned flights (one per pair), unranked. ``risk``: flight_id -> route-engine bunching risk."""
    out = []
    for a, b in combinations(sorted(flights, key=lambda f: f.flight_id), 2):
        if a.aircraft_id == b.aircraft_id:
            continue
        events = sorted(pair_events(a, b, t0, horizon_s), key=lambda e: e["t"])
        if not events:
            continue
        first = events[0]
        worst = min(events, key=lambda e: SEVERITY_RANK[e["severity"]])
        risks = [r for f in (a, b) if (r := (risk or {}).get(f.flight_id)) is not None]
        out.append(
            {
                "id": f"{a.flight_id}|{b.flight_id}",
                "kind": first["kind"],
                "severity": worst["severity"],
                "medical": "medical" in (a.vehicle_type, b.vehicle_type),
                "t": first["t"],
                "time_to_conflict_s": round(first["t"] - t0, 1),
                "node": first.get("node"),
                "edge": first.get("edge"),
                "overlap_s": round(first["overlap_s"], 1),
                "gap_s": round(first["gap_s"], 1),
                "flights": [_ref(a), _ref(b)],
                "bunching_risk": round(max(risks), 3) if risks else None,
                "locations": [{k: e[k] for k in ("kind", "node", "edge", "t", "severity") if k in e} for e in events],
            }
        )
    return out


def crowded_nodes(flights: list[Flight], t0: float, horizon_s: float, minimum: int = 3) -> list[dict[str, Any]]:
    """Nodes where at least ``minimum`` flights' windows overlap at once (a queue forming: the group case)."""
    per_node: dict[str, list[tuple[Flight, tuple[float, float, float]]]] = {}
    for f in flights:
        for node, w in node_windows(f.plan, t0).items():
            if w[1] - t0 <= horizon_s:
                per_node.setdefault(node, []).append((f, w))
    out = []
    for node, rows in per_node.items():
        best: list[tuple[Flight, tuple[float, float, float]]] = []
        for _, w in rows:  # the largest set that all contain one flight's p50
            here = [(f, x) for f, x in rows if x[0] <= w[1] <= x[2]]
            if len(here) > len(best):
                best = here
        if len(best) >= minimum:
            t = sorted(w[1] for _, w in best)[len(best) // 2]
            out.append(
                {"node": node, "t": t, "time_to_conflict_s": round(max(0.0, t - t0), 1), "count": len(best),
                 "medical": any(f.vehicle_type == "medical" for f, _ in best), "flights": [_ref(f) for f, _ in best]}
            )  # fmt: skip
    return out


def route_state_at(
    planner: Planner, plan: FlightPlan, times: list[float], t: float
) -> tuple[float, float, float, float]:
    """(lat, lon, band_floor_m, band_ceiling_m) of the route at time ``t`` (clamped to its ends)."""
    lat, lon = planner._position_at(plan.path, times, t)
    leg = next((i for i in range(1, len(plan.path)) if t <= times[i]), len(plan.path) - 1)
    leg = max(1, leg)
    edge = planner.network.edge_between(plan.path[leg - 1], plan.path[leg]) if len(plan.path) > 1 else None
    if edge is None:
        return lat, lon, 0.0, 10_000.0
    return lat, lon, edge.band_floor_ft * FT_TO_M, edge.band_ceiling_ft * FT_TO_M


def _project(lat: float, lon: float, track_deg: float, speed_mps: float, s: float) -> tuple[float, float]:
    rad = math.radians(track_deg)
    d = speed_mps * s
    return (
        lat + math.cos(rad) * d / 111_320.0,
        lon + math.sin(rad) * d / (111_320.0 * max(0.2, math.cos(math.radians(lat)))),
    )


def adsb_conflicts(
    planner: Planner, flights: list[Flight], adsb: list[dict[str, Any]], t0: float
) -> list[dict[str, Any]]:
    """Live ADS-B aircraft whose straight-ahead track comes near a planned route (see the module docstring)."""
    out = []
    legs = {f.flight_id: Planner._leg_times(f.plan) for f in flights}
    for ac in adsb:
        gs = float(ac.get("gs_kt") or 0.0) * KT_TO_MPS
        if gs < 1.0 or float(ac.get("seen_s") or 0.0) > ADSB_MAX_AGE_S:
            continue
        alt_m = ac["alt_ft"] * FT_TO_M if isinstance(ac.get("alt_ft"), int | float) else None
        for f in flights:
            times = legs[f.flight_id]
            best: dict[str, Any] | None = None
            for k in range(0, int(ADSB_HORIZON_S / ADSB_STEP_S) + 1):
                s = k * ADSB_STEP_S
                if t0 + s > times[-1] + 30:
                    break
                lat, lon, floor_m, ceil_m = route_state_at(planner, f.plan, times, t0 + s)
                a_lat, a_lon = _project(ac["lat"], ac["lon"], float(ac.get("track_deg") or 0.0), gs, s)
                d = distance_m(a_lat, a_lon, lat, lon)
                if d > SEP.follow_target_distance_m:
                    continue
                if alt_m is not None and not (
                    floor_m - SEP.min_vertical_separation_m <= alt_m <= ceil_m + SEP.min_vertical_separation_m
                ):
                    continue
                if best is None or d < best["min_distance_m"]:
                    best = {"s": s, "min_distance_m": d, "lat": lat, "lon": lon}
            if best is None:
                continue
            out.append(
                {
                    "id": f"adsb:{ac['icao']}|{f.flight_id}",
                    "kind": "adsb",
                    "severity": "high" if best["min_distance_m"] <= SEP.min_separation_m else "medium",
                    "medical": f.vehicle_type == "medical",
                    "t": t0 + best["s"],
                    "time_to_conflict_s": best["s"],
                    "node": None,
                    "edge": None,
                    "min_distance_m": round(best["min_distance_m"], 1),
                    "lat": round(best["lat"], 5),
                    "lon": round(best["lon"], 5),
                    "flights": [_ref(f)],
                    "adsb": {k: ac.get(k) for k in ("icao", "callsign", "category", "alt_ft", "gs_kt", "track_deg")},
                    "bunching_risk": None,
                    "locations": [],
                }
            )
    return out


def rank(conflicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Most urgent first: high severity before medium, then the soonest."""
    return sorted(conflicts, key=lambda c: (SEVERITY_RANK[c["severity"]], c["time_to_conflict_s"], c["id"]))
