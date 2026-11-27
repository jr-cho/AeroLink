"""Classical navigation baseline B1 (PLAN E3.5).

B1 is a non-AI route advisor.  It predicts:

* cruise-time ETA windows (±60 s fixed)
* pad waits by greedy first-come-first-served, medical first
* junction waits by first-come-first-served at merge waypoints
* ranks options by predicted total time
* flags off-route aircraft when they leave a 300 m / time-window tube around the plan
* makes no suggestions after departure (ARCHITECTURE §6.7)

Its traffic picture is only the flights already planned, assumed to fly their planned
timing exactly (§6.3). In the simulator that picture comes from ``bind``; elsewhere callers
pass ``planned`` explicitly. With no planned flights every wait is zero.

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
    from aerolink.sim.state import AircraftState, FlightPlan, WorldState


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
    # One arrival blocks a pad for landing + turnaround: the simulator's slot length
    # (sim/pads.py LANDING_DURATION_S + DEFAULT_TURNAROUND_S).
    pad_service_s: float = 310.0


class B1Advisor:
    """Classical route advisor: k-shortest paths + deterministic wait model."""

    def __init__(self, network: Network, cfg: B1Config | None = None, k: int = 3):
        self.network = network
        self.cfg = cfg or B1Config()
        self.k = k
        inbound: dict[str, int] = {}
        for _, v in network.edges:
            inbound[v] = inbound.get(v, 0) + 1
        self._merges = {n for n, c in inbound.items() if c > 1}  # merge waypoints (junctions)

    def request_routes(
        self,
        origin: str,
        destination: str,
        vehicle_type: VehicleType,
        t_s: float,
        planned: list[FlightPlan] | None = None,
    ) -> list[RouteOption]:
        paths = self.network.shortest_paths(origin, destination, vehicle_type=vehicle_type, k=self.k)
        if not paths:
            return []
        options = [self._score(path, vehicle_type, t_s, planned or []) for path in paths]
        return sorted(options, key=lambda o: o.total_predicted_s)

    def suggest(self, ac: AircraftState, from_node: str, t_s: float) -> RouteOption | None:
        """Classical navigation makes no suggestions after departure (ARCHITECTURE §6.7)."""
        return None

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
            # (x, y) = (lon, lat): the helper scales x by cos(latitude)
            d = _point_to_segment_m(pos.lon, pos.lat, start["lon"], start["lat"], end["lon"], end["lat"])
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

    def _score(self, path: list[str], vehicle_type: VehicleType, t_s: float, planned: list[FlightPlan]) -> RouteOption:
        speed = vehicle_cfg(vehicle_type).cruise_speed_mps
        ahead = self._ahead(planned, vehicle_type)
        # Walk the path at cruise speed; each merge waypoint adds its FCFS wait, which also
        # delays every node after it.
        t = t_s
        junction_wait = 0.0
        for a, b in zip(path, path[1:], strict=False):
            t += self.network.edges[(a, b)].length_m / speed
            if b != path[-1] and b in self._merges:
                w = self._fcfs_wait([eta for eta, _ in ahead.get(b, [])], t, self.cfg.junction_interval_s, 1)
                junction_wait += w
                t += w
        cruise = self._cruise_time(path, vehicle_type)
        eta = t_s + cruise
        dest = path[-1]
        pads = int(self.network.nodes[dest].get("pads", 1) or 1)
        dest_arrivals = [eta_d for eta_d, plan in ahead.get(dest, []) if plan.destination_node == dest]
        pad_wait = self._fcfs_wait(dest_arrivals, t, self.cfg.pad_service_s, pads)
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

    def _ahead(self, planned: list[FlightPlan], vehicle_type: VehicleType) -> dict[str, list[tuple[float, FlightPlan]]]:
        """Planned passing times per node, from the flights that go before us (medical first:
        a medical aircraft only queues behind other medical flights)."""
        out: dict[str, list[tuple[float, FlightPlan]]] = {}
        for plan in planned:
            if self.cfg.medical_priority and vehicle_type == "medical" and plan.purpose != "medical_dispatch":
                continue
            for node, eta in plan.eta_by_node.items():
                out.setdefault(node, []).append((eta, plan))
        return out

    @staticmethod
    def _fcfs_wait(arrivals: list[float], t: float, service_s: float, servers: int) -> float:
        """Wait at a first-come-first-served resource with ``servers`` slots of ``service_s``,
        given the planned arrival times of the traffic already there. Only arrivals before
        ``t`` are ahead of us; each holds a slot from max(arrival, slot free) for ``service_s``."""
        free = [-math.inf] * max(1, servers)
        for a in sorted(x for x in arrivals if x <= t):
            i = min(range(len(free)), key=free.__getitem__)
            free[i] = max(a, free[i]) + service_s
        return max(0.0, min(free) - t)


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
    """Wrap B1Advisor so it satisfies ``controller.RouteAdvisor``. The simulator binds it to
    its world so B1 sees the flights already planned (ARCHITECTURE §6.3)."""

    def __init__(self, advisor: B1Advisor):
        self.advisor = advisor
        self.world: WorldState | None = None

    def bind(self, world: WorldState, controller: object) -> None:
        self.world = world

    def planned(self) -> list[FlightPlan]:
        if self.world is None:
            return []
        return [ac.active_flight for ac in self.world.aircraft.values() if ac.active_flight is not None]

    def request_routes(self, origin: str, destination: str, vehicle_type: VehicleType, t_s: float) -> list[list[str]]:
        return [opt.path for opt in self.advisor.request_routes(origin, destination, vehicle_type, t_s, self.planned())]

    def suggest(self, ac: AircraftState, from_node: str, t_s: float) -> list[str] | None:
        opt = self.advisor.suggest(ac, from_node, t_s)
        return opt.path if opt else None

    def predict(self, origin: str, destination: str, vehicle_type: VehicleType, t_s: float) -> list[RouteOption]:
        """Expose B1's predictions without breaking the RouteAdvisor protocol."""
        return self.advisor.request_routes(origin, destination, vehicle_type, t_s, self.planned())
