"""Classical navigation baseline B1 (PLAN E3.5).

B1 is a non-AI route advisor.  It predicts:

* cruise-time ETA windows (±60 s fixed)
* pad waits by greedy first-come-first-served, medical first
* junction waits by first-come-first-served at merge waypoints
* ranks options by predicted total time
* flags off-route aircraft when they leave a 300 m / time-window tube around the plan

B1 never approves or rejects flights; it only returns options and predictions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aerolink.contracts import VehicleType
from aerolink.network import Network
from aerolink.vehicle import vehicle_cfg

if TYPE_CHECKING:
    from aerolink.sim.state import AircraftState


@dataclass(frozen=True)
class RouteOption:
    path: list[str]
    eta_s: float  # predicted arrival time (s since simulation start)
    eta_window_s: tuple[float, float]  # p10/p90-ish fixed ±60 s window
    predicted_pad_wait_s: float
    predicted_junction_wait_s: float
    total_predicted_s: float
    reason: str


@dataclass(frozen=True)
class B1Config:
    eta_window_s: float = 60.0
    off_route_distance_m: float = 300.0
    off_route_time_s: float = 30.0
    junction_interval_s: float = 60.0
    medical_priority: bool = True


class B1Advisor:
    """Classical route advisor: k-shortest paths + deterministic wait model."""

    def __init__(self, network: Network, cfg: B1Config | None = None, k: int = 3):
        self.network = network
        self.cfg = cfg or B1Config()
        self.k = k

    def request_routes(
        self,
        origin: str,
        destination: str,
        vehicle_type: VehicleType,
        t_s: float,
    ) -> list[RouteOption]:
        paths = self.network.shortest_paths(origin, destination, vehicle_type=vehicle_type, k=self.k)
        if not paths:
            return []
        options = [self._score(path, vehicle_type, t_s) for path in paths]
        return sorted(options, key=lambda o: o.total_predicted_s)

    def suggest(self, ac: AircraftState, from_node: str, t_s: float) -> RouteOption | None:
        plan = ac.active_flight
        if plan is None:
            return None
        options = self.request_routes(from_node, plan.destination_node, ac.vehicle_type, t_s)
        if not options:
            return None
        best = options[0]
        current_path = [from_node] + [leg.end_node for leg in ac.legs[ac.leg_index :]]
        current_eta = self._cruise_time(current_path, ac.vehicle_type)
        # suggest only if it improves total predicted time by >10%
        if best.total_predicted_s >= 0.9 * current_eta:
            return None
        return best

    def is_off_route(self, ac: AircraftState, t_s: float) -> bool:
        """Return True when the aircraft has left the planned route tube."""
        plan = ac.active_flight
        if plan is None or not ac.legs:
            return False
        # distance from current position to nearest point on remaining route
        pos = ac.position
        min_dist = float("inf")
        for leg in ac.legs[ac.leg_index :]:
            start = self.network.nodes[leg.start_node]
            end = self.network.nodes[leg.end_node]
            d = _point_to_segment_m(pos.lat, pos.lon, start["lat"], start["lon"], end["lat"], end["lon"])
            min_dist = min(min_dist, d)
        if min_dist > self.cfg.off_route_distance_m:
            return True
        # time-based: more than ``off_route_time_s`` ahead/behind the schedule implied by cruise speed
        if ac.leg is None:
            return False
        expected = ac.distance_along_edge_m / max(1.0, ac.velocity_mps) if ac.velocity_mps > 0 else 0.0
        # crude: off-route if no recent progress along planned leg
        return (
            ac.status == "flying"
            and min_dist > self.cfg.off_route_distance_m * 0.5
            and expected > self.cfg.off_route_time_s
        )

    # --------------------------------------------------------------------- internal

    def _score(self, path: list[str], vehicle_type: VehicleType, t_s: float) -> RouteOption:
        cruise = self._cruise_time(path, vehicle_type)
        eta = t_s + cruise
        pad_wait = self._predict_pad_wait(path[-1], vehicle_type, eta)
        junction_wait = self._predict_junction_wait(path, vehicle_type, eta)
        total = cruise + pad_wait + junction_wait
        return RouteOption(
            path=path,
            eta_s=eta,
            eta_window_s=(eta - self.cfg.eta_window_s, eta + self.cfg.eta_window_s),
            predicted_pad_wait_s=pad_wait,
            predicted_junction_wait_s=junction_wait,
            total_predicted_s=total,
            reason=f"cruise {cruise:.0f}s + pad wait {pad_wait:.0f}s + junction wait {junction_wait:.0f}s",
        )

    def _cruise_time(self, path: list[str], vehicle_type: VehicleType) -> float:
        length = self.network.route_length_m(path)
        speed = vehicle_cfg(vehicle_type).cruise_speed_mps
        return length / speed

    def _predict_pad_wait(self, node: str, vehicle_type: VehicleType, eta_s: float) -> float:
        # Simple model: if the node has one passenger pad or one medical slot,
        # expect one aircraft ahead at busy periods.  No calendar access.
        pads = self.network.nodes[node].get("pads", 1)
        base = 60.0 if vehicle_type == "medical" else 120.0
        return base / max(1, pads)

    def _predict_junction_wait(self, path: list[str], vehicle_type: VehicleType, eta_s: float) -> float:
        # Each intermediate waypoint with >1 inbound edge is a junction.
        # Conservative: add a small sequencing wait at each merge.
        wait = 0.0
        for node in path[1:-1]:
            inbound = len([e for e in self.network.edges if e[1] == node])
            if inbound > 1:
                wait += self.cfg.junction_interval_s / 2.0
        # medical aircraft are assumed to receive priority at junctions
        if self.cfg.medical_priority and vehicle_type == "medical":
            wait *= 0.5
        return wait


def _point_to_segment_m(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    """Shortest distance from point p to segment ab (degrees -> flat-earth m)."""
    k = math.cos(math.radians(0.5 * (py + ay)))
    dx = (bx - ax) * math.radians(1) * 6_371_008.8 * k
    dy = (by - ay) * math.radians(1) * 6_371_008.8
    if dx == 0 and dy == 0:
        return math.hypot((px - ax) * math.radians(1) * 6_371_008.8 * k, (py - ay) * math.radians(1) * 6_371_008.8)
    t = max(0.0, min(1.0, ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / ((bx - ax) ** 2 + (by - ay) ** 2)))
    closest_lat = ay + t * (by - ay)
    closest_lon = ax + t * (bx - ax)
    kk = math.cos(math.radians(0.5 * (py + closest_lat)))
    return math.hypot(
        (px - closest_lon) * math.radians(1) * 6_371_008.8 * kk,
        (py - closest_lat) * math.radians(1) * 6_371_008.8,
    )


# keep interface compatible with controller.RouteAdvisor
class B1RouteAdvisorWrapper:
    """Wrap B1Advisor so it satisfies ``controller.RouteAdvisor``."""

    def __init__(self, advisor: B1Advisor):
        self.advisor = advisor

    def request_routes(self, origin: str, destination: str, vehicle_type: VehicleType, t_s: float) -> list[list[str]]:
        return [opt.path for opt in self.advisor.request_routes(origin, destination, vehicle_type, t_s)]

    def suggest(self, ac: AircraftState, from_node: str, t_s: float) -> list[str] | None:
        opt = self.advisor.suggest(ac, from_node, t_s)
        return opt.path if opt else None

    def predict(self, origin: str, destination: str, vehicle_type: VehicleType, t_s: float) -> list[RouteOption]:
        """Expose B1's predictions without breaking the RouteAdvisor protocol."""
        return self.advisor.request_routes(origin, destination, vehicle_type, t_s)
