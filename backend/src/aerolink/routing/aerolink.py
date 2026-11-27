"""Full AeroLink route engine, switchable piece by piece for the ablations (PLAN E5.7, ARCHITECTURE §8, §9.3).

Builds on the A1 engine (``routing/a1.py``) and adds, one configuration at a time:

| Config | Adds |
|---|---|
| A1 | B1 + AI ETA windows (``cv`` ETAs in simulation, as in M4: see PLAN E4.2 note) |
| A2 | ETA windows from ``aerolink-gbm`` |
| A3 | + demand forecaster: expected (not yet planned) flights in the traffic forecast |
| A4 | + off-route detection: aircraft not following a plan are spread over the nodes they can reach |
| A5 | + risk ranking (mu = 600 s per unit of bunching risk) + en-route suggestions |

Unlike the M4 wrapper, this engine sees the live traffic picture: the simulator binds it
to the world (``bind``), so every request is scored against the flights already planned.
It still only predicts and ranks; it never approves, assigns or holds anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from aerolink.contracts import VehicleType
from aerolink.demand.forecaster import DemandModel
from aerolink.forecast.gbm import GBMForecaster
from aerolink.forecast.interface import Forecaster
from aerolink.network import Network
from aerolink.offroute.detector import reachable_mass
from aerolink.routing.a1 import A1Advisor, A1Config, A1RouteOption
from aerolink.traffic import forecast_traffic
from aerolink.vehicle import vehicle_cfg

if TYPE_CHECKING:
    from aerolink.sim.state import AircraftState, FlightPlan, WorldState


@dataclass(frozen=True)
class AeroLinkConfig:
    demand: bool = False  # A3
    off_route: bool = False  # A4
    risk_ranking: bool = False  # A5
    en_route: bool = False  # A5
    a1: A1Config = field(default_factory=A1Config)


ABLATIONS: dict[str, AeroLinkConfig] = {
    "A1": AeroLinkConfig(),
    "A2": AeroLinkConfig(),
    "A3": AeroLinkConfig(demand=True),
    "A4": AeroLinkConfig(demand=True, off_route=True),
    "A5": AeroLinkConfig(demand=True, off_route=True, risk_ranking=True, en_route=True),
}


class AeroLinkAdvisor(A1Advisor):
    def __init__(
        self,
        network: Network,
        forecaster: Forecaster,
        cfg: AeroLinkConfig,
        demand: DemandModel | None = None,
        k: int = 3,
    ):
        a1 = cfg.a1 if cfg.risk_ranking else replace(cfg.a1, mu_risk_s=0.0)
        super().__init__(network, forecaster, a1, k)
        self.config = cfg
        self.demand = demand
        self.world: WorldState | None = None
        self.controller: Any = None
        self.start_local_hour = 0.0
        self.flight_status: dict[str, str] = {}  # flight id -> "off_route" once flagged
        self._expected_cache: dict[int, list] = {}
        self._travel: dict[tuple[str, str], float | None] = {}

    # --- live traffic picture -------------------------------------------------------------

    def bind(self, world: WorldState, controller: Any) -> None:
        """Called by the simulator once the world exists (``sim/runner.py``)."""
        self.world = world
        self.controller = controller
        self.start_local_hour = float(controller.cfg.start_local_hour)

    def local_hour(self, t_s: float) -> float:
        return (self.start_local_hour + t_s / 3600.0) % 24.0

    def planned_flights(self) -> list[FlightPlan]:
        if self.world is None:
            return []
        return [ac.active_flight for ac in self.world.aircraft.values() if ac.active_flight is not None]

    def _travel_time(self, a: str | None, b: str) -> float | None:
        if a is None:
            return 0.0
        if (a, b) not in self._travel:
            d = self.network.route_distance_m(a, b, "passenger")
            self._travel[(a, b)] = None if d is None else d / vehicle_cfg("passenger").cruise_speed_mps
        return self._travel[(a, b)]

    def expected_flights(self, t_s: float) -> list[tuple[float, str, float, float, float]]:
        out: list[tuple[float, str, float, float, float]] = []
        if self.config.demand and self.demand is not None:
            key = int(t_s // 900)
            if key not in self._expected_cache:
                self._expected_cache[key] = self.demand.expected_flights(self.local_hour(t_s), t_s, self._travel_time)
            out += self._expected_cache[key]
        if self.config.off_route:
            out += self.off_route_mass(t_s)
        return out

    def off_route_mass(self, t_s: float) -> list[tuple[float, str, float, float, float]]:
        """Aircraft flying without an AeroLink plan (background / rogue traffic), spread over reachable nodes."""
        out = []
        for bg in getattr(self.controller, "background_aircraft", []) or []:
            times = bg.times_s
            if not times or not (times[0] <= t_s <= times[-1]):
                continue
            k = max(i for i in range(len(times)) if times[i] <= t_s)
            k = min(k, len(times) - 2)
            a, b = self.network.nodes[bg.path[k]], self.network.nodes[bg.path[k + 1]]
            w = (t_s - times[k]) / max(1e-6, times[k + 1] - times[k])
            lat, lon = a["lat"] + w * (b["lat"] - a["lat"]), a["lon"] + w * (b["lon"] - a["lon"])
            out += reachable_mass(self.network, lat, lon, bg.speed_mps, t_s)
        return out

    # --- route requests -------------------------------------------------------------------

    def request_routes(
        self,
        origin: str,
        destination: str,
        vehicle_type: VehicleType,
        t_s: float,
        planned: list[FlightPlan] | None = None,
        current_speed_mps: float | None = None,
    ) -> list[A1RouteOption]:
        paths = self._candidate_paths(origin, destination, vehicle_type)
        if not paths:
            return []
        planned = self.planned_flights() if planned is None else planned
        traffic = self._traffic(planned, t_s)
        options = []
        for path in paths:
            options.append(
                self._score(
                    path,
                    self._eta(path, t_s, vehicle_type, planned, current_speed_mps),
                    vehicle_type,
                    t_s,
                    traffic,
                    planned,
                )
            )
        options.sort(key=lambda o: o.total_predicted_s)
        return options

    def _eta(self, path, t_s, vehicle_type, planned, current_speed_mps=None):
        if isinstance(self.forecaster, GBMForecaster):  # aerolink-gbm uses traffic and time of day
            nodes = set(path[1:])
            self.forecaster.traffic_ahead = float(sum(1 for p in planned or [] if nodes & set(p.eta_by_node)))
            self.forecaster.local_hour = self.local_hour(t_s)
        return self.forecaster.predict_eta(path, t_s, vehicle_type, current_speed_mps=current_speed_mps)

    def _traffic(self, planned: list[FlightPlan] | None, t_s: float):
        """Planned flights plus expected (demand) and off-route mass, per the ablation config."""
        return forecast_traffic(list(planned or []), self._capacities(planned), self.expected_flights(t_s))

    def suggest(
        self, ac: AircraftState, from_node: str, t_s: float, planned: list[FlightPlan] | None = None
    ) -> A1RouteOption | None:
        if not self.config.en_route:
            return None
        if planned is None:  # everyone else's flights; the aircraft doesn't queue behind itself
            own = ac.active_flight.flight_id if ac.active_flight is not None else None
            planned = [p for p in self.planned_flights() if p.flight_id != own]
        return super().suggest(ac, from_node, t_s, planned)

    def recalculate(
        self,
        flight_id: str,
        lat: float,
        lon: float,
        destination: str,
        vehicle_type: VehicleType,
        t_s: float,
        current_speed_mps: float | None = None,
    ) -> list[A1RouteOption]:
        """Off route: mark the flight ``off_route`` and return fresh options from the nearest node."""
        self.flight_status[flight_id] = "off_route"
        k = math.cos(math.radians(lat))
        nearest = min(
            (n for n in self.network.nodes if self.network.graph.degree(n) > 0),
            key=lambda n: (self.network.nodes[n]["lat"] - lat) ** 2 + ((self.network.nodes[n]["lon"] - lon) * k) ** 2,
        )
        return self.request_routes(nearest, destination, vehicle_type, t_s, current_speed_mps=current_speed_mps)


class AeroLinkRouteAdvisorWrapper:
    """``controller.RouteAdvisor`` adapter (paths in, paths out) that the simulator binds to its world."""

    def __init__(self, advisor: AeroLinkAdvisor):
        self.advisor = advisor

    def bind(self, world: WorldState, controller: Any) -> None:
        self.advisor.bind(world, controller)

    @property
    def last_gain_s(self) -> float | None:
        return self.advisor.last_gain_s

    def request_routes(self, origin: str, destination: str, vehicle_type: VehicleType, t_s: float) -> list[list[str]]:
        return [o.path for o in self.advisor.request_routes(origin, destination, vehicle_type, t_s)]

    def suggest(self, ac: AircraftState, from_node: str, t_s: float) -> list[str] | None:
        opt = self.advisor.suggest(ac, from_node, t_s)
        return opt.path if opt else None

    def predict(self, origin: str, destination: str, vehicle_type: VehicleType, t_s: float) -> list[A1RouteOption]:
        return self.advisor.request_routes(origin, destination, vehicle_type, t_s)
