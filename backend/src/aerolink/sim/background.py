"""Synthetic non-cooperative background traffic (PLAN E3.3 T6).

Background tracks are deterministic given the same seed. They are added to the
traffic index so separation metrics (bunching, loss_of_separation) count them,
but they ignore pad bookings, energy, and route requests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from aerolink.network import Edge, Network
from aerolink.sim.state import SimPosition
from aerolink.sim.traffic import Track
from aerolink.sim.world_config import BackgroundTrafficConfig


def _great_circle_course(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """True course from (lat1, lon1) to (lat2, lon2) in degrees."""
    dlon = math.radians(lon2 - lon1)
    lat1r = math.radians(lat1)
    lat2r = math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


@dataclass(frozen=True)
class BackgroundAircraft:
    """A non-cooperative aircraft following a fixed 4D track."""

    aircraft_id: str
    vehicle_type: Literal["background"] = "background"
    path: list[str] = field(default_factory=list)
    times_s: list[float] = field(default_factory=list)
    alt_msl_m: float = 150.0
    speed_mps: float = 50.0


@dataclass
class BackgroundState:
    """Mutable state of one background aircraft at the current simulation time."""

    aircraft_id: str
    position: SimPosition
    edge: Edge | None = None
    start_node: str = ""
    end_node: str = ""
    distance_along_edge_m: float = 0.0
    velocity_mps: float = 0.0
    heading_deg: float = 0.0
    vertical_speed_mps: float = 0.0
    finished: bool = False

    @property
    def airborne(self) -> bool:
        return not self.finished

    @property
    def status(self) -> str:
        return "cruise"


def generate_tracks(
    network: Network,
    cfg: BackgroundTrafficConfig,
    base_rng: np.random.Generator,
) -> list[BackgroundAircraft]:
    """Generate ``cfg.count`` synthetic tracks that traverse the network."""
    if cfg.count <= 0:
        return []
    rng = np.random.default_rng(int(base_rng.integers(0, 2**63 - 1)) + cfg.seed_offset)
    nodes = sorted(network.nodes)
    aircraft: list[BackgroundAircraft] = []
    i = 0
    while len(aircraft) < cfg.count and i < cfg.count * 20:
        i += 1
        origin = nodes[int(rng.integers(len(nodes)))]
        candidates = [n for n in nodes if n != origin]
        if not candidates:
            continue
        destination = candidates[int(rng.integers(len(candidates)))]
        paths = network.shortest_paths(origin, destination, vehicle_type="passenger", k=1)
        if not paths:
            continue
        path = paths[0]
        length = network.route_length_m(path)
        if length <= 0:
            continue
        speed = max(1.0, rng.normal(cfg.speed_mps, cfg.speed_sigma))
        travel = length / speed
        start = cfg.start_time_s + rng.random() * max(0.0, cfg.duration_s - travel)
        times = [start]
        cumulative = start
        for a, b in zip(path[:-1], path[1:], strict=False):
            cumulative += (network.route_distance_m(a, b, "passenger") or network.edge_between(a, b).length_m) / speed
            times.append(cumulative)
        aircraft.append(
            BackgroundAircraft(
                aircraft_id=f"bg-{len(aircraft):03d}",
                path=path,
                times_s=times,
                alt_msl_m=cfg.altitude_msl_m,
                speed_mps=speed,
            )
        )
    return aircraft


def step_background(
    aircraft: list[BackgroundAircraft],
    t_s: float,
    network: Network,
) -> list[BackgroundState]:
    """Sample each background aircraft at ``t_s`` and build mutable state."""
    states: list[BackgroundState] = []
    for ac in aircraft:
        if t_s < ac.times_s[0] or t_s > ac.times_s[-1]:
            states.append(
                BackgroundState(
                    aircraft_id=ac.aircraft_id,
                    position=SimPosition(
                        lat=float(network.nodes[ac.path[0]]["lat"]),
                        lon=float(network.nodes[ac.path[0]]["lon"]),
                        alt_msl_m=ac.alt_msl_m,
                    ),
                    finished=True,
                )
            )
            continue
        # find segment
        idx = 0
        for j in range(len(ac.times_s) - 1):
            if ac.times_s[j] <= t_s <= ac.times_s[j + 1]:
                idx = j
                break
        start, end = ac.path[idx], ac.path[idx + 1]
        edge = network.edge_between(start, end)
        if edge is None:
            states.append(
                BackgroundState(
                    aircraft_id=ac.aircraft_id,
                    position=SimPosition(
                        lat=float(network.nodes[start]["lat"]),
                        lon=float(network.nodes[start]["lon"]),
                        alt_msl_m=ac.alt_msl_m,
                    ),
                    finished=True,
                )
            )
            continue
        seg_len = network.route_distance_m(start, end, "passenger") or edge.length_m
        frac = (t_s - ac.times_s[idx]) / max(1e-9, ac.times_s[idx + 1] - ac.times_s[idx])
        frac = max(0.0, min(1.0, frac))
        along = frac * seg_len
        n1 = network.nodes[start]
        n2 = network.nodes[end]
        lat = n1["lat"] + (n2["lat"] - n1["lat"]) * frac
        lon = n1["lon"] + (n2["lon"] - n1["lon"]) * frac
        states.append(
            BackgroundState(
                aircraft_id=ac.aircraft_id,
                position=SimPosition(lat=float(lat), lon=float(lon), alt_msl_m=ac.alt_msl_m),
                edge=edge,
                start_node=start,
                end_node=end,
                distance_along_edge_m=along,
                velocity_mps=ac.speed_mps,
                heading_deg=_great_circle_course(
                    float(n1["lat"]), float(n1["lon"]), float(n2["lat"]), float(n2["lon"])
                ),
                vertical_speed_mps=0.0,
            )
        )
    return states


def background_tracks(states: list[BackgroundState]) -> list[Track]:
    """Convert live background states to traffic-index Tracks."""
    tracks: list[Track] = []
    for st in states:
        if st.finished or st.edge is None:
            continue
        tracks.append(
            Track(
                aircraft_id=st.aircraft_id,
                edge=st.edge,
                start_node=st.start_node,
                end_node=st.end_node,
                distance_along_edge_m=st.distance_along_edge_m,
                alt_msl_m=st.position.alt_msl_m,
                velocity_mps=st.velocity_mps,
                heading_deg=st.heading_deg,
                vertical_speed_mps=st.vertical_speed_mps,
                status="cruise",
            )
        )
    return tracks
