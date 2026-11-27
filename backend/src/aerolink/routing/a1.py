"""A1 route engine: B1 + AI ETA windows + capacity model + bunching risk (PLAN E4.4).

This module is pure calculation layer: it has no database or MQTT access and
returns deterministic route options for a given snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aerolink.contracts import VehicleType
from aerolink.forecast.interface import EtaPrediction, Forecaster
from aerolink.network import Network
from aerolink.traffic import TrafficForecast, forecast_traffic, node_capacity

if TYPE_CHECKING:
    from aerolink.sim.state import AircraftState, FlightPlan, WorldState

# Default cost trade-off: 600 s per unit of bunching risk.
_MU_RISK_S = 600.0

# Candidate paths must be at least this diverse (share at most 70% edges).
_MIN_PATH_DIVERSITY = 0.70

# Junction service time: one aircraft per 60 s per junction.
_JUNCTION_SERVICE_S = 60.0


@dataclass(frozen=True)
class A1RouteOption:
    path: list[str]
    eta_s: float  # p50 arrival at destination
    eta_window_s: tuple[float, float]  # p10/p90 at destination
    predicted_pad_wait_s: float
    predicted_junction_wait_s: float
    total_predicted_s: float
    bunching_risk: float
    traffic: str  # low / moderate / heavy at destination
    cost_terms: dict[str, float]
    reason: str


@dataclass(frozen=True)
class A1Config:
    eta_window_spread: float = 0.08
    mu_risk_s: float = _MU_RISK_S
    min_path_diversity: float = _MIN_PATH_DIVERSITY
    junction_service_s: float = _JUNCTION_SERVICE_S
    suggestion_improvement: float = 0.10
    en_route_interval_s: float = 120.0


def _path_diversity(path_a: list[str], path_b: list[str]) -> float:
    """Fraction of edges shared by two paths."""
    edges_a = {(path_a[i], path_a[i + 1]) for i in range(len(path_a) - 1)}
    edges_b = {(path_b[i], path_b[i + 1]) for i in range(len(path_b) - 1)}
    if not edges_a or not edges_b:
        return 0.0
    return len(edges_a & edges_b) / min(len(edges_a), len(edges_b))


def _is_merge_node(network: Network, node: str) -> bool:
    return len([e for e in network.edges if e[1] == node]) > 1


class A1Advisor:
    """AI-assisted route advisor using a pluggable forecaster.

    Implements the ``controller.RouteAdvisor`` protocol through
    ``A1RouteAdvisorWrapper``.
    """

    def __init__(
        self,
        network: Network,
        forecaster: Forecaster,
        cfg: A1Config | None = None,
        k: int = 3,
    ):
        self.network = network
        self.forecaster = forecaster
        self.cfg = cfg or A1Config()
        self.k = k
        self.last_gain_s: float | None = None  # predicted saving of the last suggestion made

    def request_routes(
        self,
        origin: str,
        destination: str,
        vehicle_type: VehicleType,
        t_s: float,
        planned: list[FlightPlan] | None = None,
        current_speed_mps: float | None = None,
    ) -> list[A1RouteOption]:
        """Ranked options. ``current_speed_mps`` is the aircraft's observed ground speed for an in-flight
        query; leave it out for a departure request (the forecaster then treats the flight as starting from rest)."""
        paths = self._candidate_paths(origin, destination, vehicle_type)
        if not paths:
            return []
        traffic = self._traffic(planned, t_s)
        options: list[A1RouteOption] = []
        for path in paths:
            eta_pred = self._eta(path, t_s, vehicle_type, planned, current_speed_mps)
            option = self._score(path, eta_pred, vehicle_type, t_s, traffic, planned)
            options.append(option)
        options.sort(key=lambda o: o.total_predicted_s)
        return options

    def suggest(
        self,
        ac: AircraftState,
        from_node: str,
        t_s: float,
        planned: list[FlightPlan] | None = None,
    ) -> A1RouteOption | None:
        self.last_gain_s = None
        plan = ac.active_flight
        if plan is None:
            return None
        # Ignore if we just suggested recently (handled by caller).
        # an in-flight query: pass the observed ground speed (callers with a bare aircraft stand-in may have none)
        speed = getattr(ac, "velocity_mps", None)
        options = self.request_routes(from_node, plan.destination_node, ac.vehicle_type, t_s, planned, speed)
        if not options:
            return None
        best = options[0]
        current_path = [from_node] + [leg.end_node for leg in ac.legs[ac.leg_index :]]
        current_eta = self._eta(current_path, t_s, ac.vehicle_type, planned, speed)
        current_cost = self._cost_from_eta(current_path, current_eta, ac.vehicle_type, t_s, planned)
        if best.total_predicted_s >= (1.0 - self.cfg.suggestion_improvement) * current_cost:
            return None
        self.last_gain_s = current_cost - best.total_predicted_s
        return best

    def _eta(
        self,
        path: list[str],
        t_s: float,
        vehicle_type: VehicleType,
        planned: list[FlightPlan] | None,
        current_speed_mps: float | None = None,
    ) -> EtaPrediction:
        """The forecaster's ETAs for ``path``. A hook: engines whose forecaster reads the traffic picture
        set its inputs here, so every path they score (options and the current route) gets its own."""
        return self.forecaster.predict_eta(path, t_s, vehicle_type, current_speed_mps=current_speed_mps)

    def _candidate_paths(self, origin: str, destination: str, vehicle_type: VehicleType) -> list[list[str]]:
        all_paths = self.network.shortest_paths(origin, destination, vehicle_type=vehicle_type, k=10)
        if not all_paths:
            return []
        selected: list[list[str]] = [all_paths[0]]
        for path in all_paths[1:]:
            if len(selected) >= self.k:
                break
            if all(_path_diversity(path, sp) < self.cfg.min_path_diversity for sp in selected):
                selected.append(path)
        # Always keep the shortest even if not diverse enough.
        return selected[: self.k]

    def _traffic(self, planned: list[FlightPlan] | None, t_s: float) -> TrafficForecast:
        """The traffic picture options are scored against: the planned flights."""
        return forecast_traffic(list(planned or []), self._capacities(planned))

    def _capacities(self, planned: list[FlightPlan] | None = None) -> dict[str, int]:
        caps: dict[str, int] = {}
        for nid, props in self.network.nodes.items():
            if "waypoint" in props.get("types", []):
                continue
            caps[nid] = node_capacity(props)
        return caps

    def _score(
        self,
        path: list[str],
        eta_pred: EtaPrediction,
        vehicle_type: VehicleType,
        t_s: float,
        traffic: TrafficForecast,
        planned: list[FlightPlan] | None,
    ) -> A1RouteOption:
        dest = path[-1]
        dest_eta = next((e for e in eta_pred.etas if e.node == dest), eta_pred.etas[-1])
        eta_s = dest_eta.eta_p50
        window = (dest_eta.eta_p10, dest_eta.eta_p90)
        cruise = eta_s - t_s
        pad_wait = self._predict_pad_wait(dest, vehicle_type, eta_s, traffic)
        junction_wait = self._predict_junction_wait(path, vehicle_type, eta_s, traffic)
        risk = self._bunching_risk(path, eta_pred, planned, vehicle_type)
        cost_terms = {
            "est_time_s": max(0.0, cruise),
            "expected_wait_s": pad_wait + junction_wait,
            "mu": self.cfg.mu_risk_s,
            "bunching_risk": risk,
        }
        total = cost_terms["est_time_s"] + cost_terms["expected_wait_s"] + self.cfg.mu_risk_s * risk
        traffic_level = traffic.level_at(dest, eta_s, "p90")
        reason = self._reason(path, cruise, pad_wait, junction_wait, risk, traffic)
        return A1RouteOption(
            path=path,
            eta_s=eta_s,
            eta_window_s=window,
            predicted_pad_wait_s=pad_wait,
            predicted_junction_wait_s=junction_wait,
            total_predicted_s=total,
            bunching_risk=risk,
            traffic=traffic_level,
            cost_terms=cost_terms,
            reason=reason,
        )

    def _cost_from_eta(
        self,
        path: list[str],
        eta_pred: EtaPrediction,
        vehicle_type: VehicleType,
        t_s: float,
        planned: list[FlightPlan] | None,
    ) -> float:
        """Recompute the A1 cost for a path given its ETA prediction, on the same traffic
        picture ``request_routes`` uses (so a suggestion compares like with like)."""
        traffic = self._traffic(planned, t_s)
        return self._score(path, eta_pred, vehicle_type, t_s, traffic, planned).total_predicted_s

    def _predict_pad_wait(
        self,
        node: str,
        vehicle_type: VehicleType,
        eta_s: float,
        traffic: TrafficForecast,
    ) -> float:
        node_traffic = traffic.bins.get(node)
        if node_traffic is None:
            return 0.0
        # Expected wait is proportional to how full the bin is at p90.
        b = int(eta_s // 60.0)
        p90 = node_traffic.p90_arrivals.get(b, 0.0)
        cap = node_traffic.capacity
        occupancy = min(1.0, p90 / max(1.0, cap))
        base = 60.0 if vehicle_type == "medical" else 120.0
        return base * occupancy

    def _predict_junction_wait(
        self,
        path: list[str],
        vehicle_type: VehicleType,
        eta_s: float,
        traffic: TrafficForecast,
    ) -> float:
        wait = 0.0
        for node in path[1:-1]:
            if not _is_merge_node(self.network, node):
                continue
            node_traffic = traffic.bins.get(node)
            occupancy = 0.0
            if node_traffic is not None:
                b = int(eta_s // 60.0)
                p90 = node_traffic.p90_arrivals.get(b, 0.0)
                occupancy = min(1.0, p90 / max(1.0, node_traffic.capacity))
            wait += self.cfg.junction_service_s * occupancy
        if vehicle_type == "medical":
            wait *= 0.5
        return wait

    def _bunching_risk(
        self,
        path: list[str],
        eta_pred: EtaPrediction,
        planned: list[FlightPlan] | None,
        vehicle_type: VehicleType,
    ) -> float:
        """Probability-like overlap between this candidate and others at shared nodes."""
        if not planned:
            return 0.0
        risk = 0.0
        my_etas = {e.node: (e.eta_p10, e.eta_p50, e.eta_p90) for e in eta_pred.etas}
        for plan in planned or []:
            if plan.purpose == "medical_dispatch" and vehicle_type != "medical":
                # non-medical flights absorb more risk around medical flights
                weight = 1.5
            else:
                weight = 1.0
            for node, eta in plan.eta_by_node.items():
                if node not in my_etas:
                    continue
                p10, p50, p90 = my_etas[node]
                # Overlap of intervals.
                overlap = max(0.0, min(p90, eta + 60.0) - max(p10, eta - 60.0))
                span = max(1.0, p90 - p10)
                risk += weight * (overlap / span)
        return min(1.0, risk / max(1.0, len(path)))

    def _reason(
        self,
        path: list[str],
        cruise: float,
        pad_wait: float,
        junction_wait: float,
        risk: float,
        traffic: TrafficForecast,
    ) -> str:
        dest = path[-1]
        dest_level = traffic.level_at(dest, cruise, "p90")
        parts = [f"cruise {cruise:.0f}s"]
        if pad_wait > 0:
            parts.append(f"pad wait {pad_wait:.0f}s")
        if junction_wait > 0:
            parts.append(f"junction wait {junction_wait:.0f}s")
        if risk > 0.05:
            parts.append(f"bunching risk {risk:.0%}")
        if dest_level != "low":
            parts.append(f"traffic at {dest} {dest_level}")
        return "; ".join(parts)


class A1RouteAdvisorWrapper:
    """Wrap A1Advisor so it satisfies ``controller.RouteAdvisor``. The simulator binds it to
    its world so the traffic forecast is built from the flights actually planned."""

    def __init__(self, advisor: A1Advisor):
        self.advisor = advisor
        self.world: WorldState | None = None

    def bind(self, world: WorldState, controller: object) -> None:
        self.world = world

    @property
    def last_gain_s(self) -> float | None:
        return self.advisor.last_gain_s

    def planned(self) -> list[FlightPlan]:
        if self.world is None:
            return []
        return [ac.active_flight for ac in self.world.aircraft.values() if ac.active_flight is not None]

    def request_routes(self, origin: str, destination: str, vehicle_type: VehicleType, t_s: float) -> list[list[str]]:
        return [opt.path for opt in self.advisor.request_routes(origin, destination, vehicle_type, t_s, self.planned())]

    def suggest(self, ac: AircraftState, from_node: str, t_s: float) -> list[str] | None:
        others = [p for p in self.planned() if ac.active_flight is None or p.flight_id != ac.active_flight.flight_id]
        opt = self.advisor.suggest(ac, from_node, t_s, others)
        return opt.path if opt else None

    def predict(self, origin: str, destination: str, vehicle_type: VehicleType, t_s: float) -> list[A1RouteOption]:
        """Expose A1's predictions without breaking the RouteAdvisor protocol."""
        return self.advisor.request_routes(origin, destination, vehicle_type, t_s, self.planned())
