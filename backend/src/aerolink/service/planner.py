"""Adapter from the pure calculation layer (A1 / B1 advisors) to v4.0 contract messages.

Everything here is synchronous and side-effect free, so the service runs it in a worker
thread (``asyncio.to_thread``) and the same inputs always give the same options.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import numpy as np

from aerolink.contracts import SCHEMA_VERSION, CostTerms, Eta, Route, RouteResponse, Segment, TrafficLevel, VehicleType
from aerolink.forecast.gbm import GBMForecaster
from aerolink.forecast.interface import Forecaster
from aerolink.forecast.simdata import graph_context
from aerolink.navigation.b1 import B1Advisor
from aerolink.network import Network
from aerolink.offroute.detector import cross_track_m, detect, reachable_mass, route_polyline
from aerolink.routing.aerolink import ABLATIONS, AeroLinkAdvisor
from aerolink.service.ai import route_segments
from aerolink.service.live import distance_m
from aerolink.service.models import ModelRegistry
from aerolink.service.settings import Settings
from aerolink.sim.state import FlightPlan
from aerolink.summary.llm import call_llm, summarize, template_summary
from aerolink.traffic import TrafficForecast, forecast_traffic
from aerolink.vehicle import vehicle_cfg

FT_TO_M = 0.3048
RESPONSE_TTL_S = 60
Method = Literal["classical", "aerolink"]
FL_TZ = ZoneInfo("America/New_York")  # the demand model's time of day is local time
SUGGESTION_ENGINE = "A5"  # the full AeroLink configuration (routing/aerolink.py ABLATIONS)
CONE_STEPS_MIN = tuple(range(0, 50, 5))


def ts(t_s: float) -> datetime:
    return datetime.fromtimestamp(t_s, UTC)


def flight_plan(row: dict[str, Any]) -> FlightPlan:
    """A stored planned flight as the calculation layer's FlightPlan."""
    return FlightPlan(
        flight_id=UUID(row["flight_id"]),
        request_id=UUID(row["request_id"]),
        route_id=UUID(row["route_id"]),
        path=list(row["path"]),
        destination_node=row["destination"],
        departure_time=float(row["departure_s"]),
        eta_by_node={k: float(v) for k, v in row["eta_by_node"].items()},
        purpose="medical_dispatch" if row["vehicle_type"] == "medical" else "passenger",
    )


@dataclass(frozen=True)
class Option:
    """One route option in the shape both engines are reduced to before it becomes a contract Route."""

    path: list[str]
    eta_s: float
    wait_s: float
    risk: float
    reason: str
    mu: float
    traffic: TrafficLevel


class LiveAeroLinkAdvisor(AeroLinkAdvisor):
    """The ablations' full AeroLink engine, bound to live data instead of a simulated world.

    Nothing about routing changes. Only the three inputs the simulator normally supplies do:
    the planned flights (a snapshot the planner sets under its lock), the local time of day
    (from the wall clock) and the off-route aircraft (those the detector flagged).
    """

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.snapshot: list[FlightPlan] = []
        self.off_route: dict[str, tuple[float, float, float]] = {}  # aircraft -> lat, lon, speed
        self.adsb: Any = None  # live ADS-B feed (adsb_live.AdsbFeed): background traffic only, never routed

    def local_hour(self, t_s: float) -> float:
        d = datetime.fromtimestamp(t_s, FL_TZ)
        return d.hour + d.minute / 60.0 + d.second / 3600.0

    def planned_flights(self) -> list[FlightPlan]:
        return self.snapshot

    def off_route_mass(self, t_s: float) -> list[tuple[float, str, float, float, float]]:
        out: list[tuple[float, str, float, float, float]] = []
        for lat, lon, speed in self.off_route.values():
            out += reachable_mass(self.network, lat, lon, speed, t_s)
        if self.adsb is not None:  # real aircraft nearby count as traffic in the forecast
            out += self.adsb.background_mass(self.network, t_s)
        return out

    def etas(self, path: list[str], t_s: float, vehicle_type: VehicleType, planned: list[FlightPlan]):
        """Per-node ETAs with the same context ``request_routes`` gave the forecaster for this path."""
        if isinstance(self.forecaster, GBMForecaster):
            nodes = set(path[1:])
            self.forecaster.traffic_ahead = float(sum(1 for p in planned if nodes & set(p.eta_by_node)))
            self.forecaster.local_hour = self.local_hour(t_s)
        return self.forecaster.predict_eta(path, t_s, vehicle_type)

    def suggest_live(
        self,
        flight: FlightPlan,
        from_node: str,
        vehicle_type: VehicleType,
        t_s: float,
        planned: list[FlightPlan],
        speed_mps: float | None = None,
    ):
        """A1's en-route comparison (``A1Advisor.suggest``) for a flight that is not a simulated aircraft."""
        if from_node not in flight.path:
            return None  # off its route: the off-route path handles it
        rest = flight.path[flight.path.index(from_node) + 1 :]
        ac = SimpleNamespace(
            active_flight=flight,
            vehicle_type=vehicle_type,
            legs=[SimpleNamespace(end_node=n) for n in rest],
            leg_index=0,
            velocity_mps=speed_mps,  # A1Advisor.suggest passes it to the forecaster (in-flight query)
        )
        return self.suggest(cast(Any, ac), from_node, t_s, planned)


class Planner:
    def __init__(
        self,
        network: Network,
        settings: Settings,
        forecaster: Forecaster | None = None,
        registry: ModelRegistry | None = None,
        llm: Callable[..., str] | None = None,
    ):
        self.network = network
        self.settings = settings
        self.method: Method = "classical" if settings.method == "classical" else "aerolink"
        self.registry = registry or ModelRegistry(network, settings)
        self.forecaster: Forecaster = forecaster or self.registry.get(settings.forecaster)
        self.adsb: Any = None  # set by the app when the live ADS-B feed runs
        self.lock = threading.RLock()  # engines keep per-request context, so planning is serialised
        self.b1 = B1Advisor(network)
        self._engines: dict[int, LiveAeroLinkAdvisor] = {}
        self.names = {nid: p.get("name", nid) for nid, p in network.nodes.items()}
        # The LLM only writes summaries and only when configured; tests inject a stub.
        self.llm: Callable[..., str] | None = llm or (
            call_llm if settings.llm_base_url and settings.llm_model else None
        )

    # --- forecaster and engines -------------------------------------------------------------
    def set_forecaster(self, name: str) -> None:
        """Switch the forecaster. Weights already in memory are not reloaded."""
        self.forecaster = self.registry.get(name)

    @property
    def engine(self) -> LiveAeroLinkAdvisor:
        key = id(self.forecaster)
        if key not in self._engines:
            self._engines[key] = LiveAeroLinkAdvisor(
                self.network, self.forecaster, ABLATIONS[SUGGESTION_ENGINE], demand=self.registry.demand
            )
        elif self._engines[key].demand is None and self.registry.demand is not None:
            self._engines[key].demand = self.registry.demand  # demand finished loading after the engine was built
        self._engines[key].adsb = self.adsb  # the feed starts after the planner exists
        return self._engines[key]

    @property
    def loaded_models(self) -> list[dict[str, Any]]:
        return self.registry.describe()

    # --- origin snapping -------------------------------------------------------------------
    def snap(self, lat: float, lon: float, vehicle_type: VehicleType) -> str | None:
        """Nearest node this vehicle type may start from, within the snap radius.

        Waypoints, or landing sites that serve the type (a medical aircraft never starts
        from an eVTOL-only site). A first cut of ARCHITECTURE §12.2's constraint-checked
        connector: the straight hop to the node is not checked against no-go airspace yet.
        """
        best, best_d = None, self.settings.snap_radius_m
        for nid, props in self.network.nodes.items():
            if "lat" not in props or self.network.graph.degree(nid) == 0:
                continue
            if "waypoint" not in props.get("types", []) and vehicle_type not in self.network.serves(nid):
                continue
            d = distance_m(lat, lon, props["lat"], props["lon"])
            if d <= best_d:
                best, best_d = nid, d
        return best

    def nearest_node(self, lat: float, lon: float) -> str:
        """Nearest connected node (what ``AeroLinkAdvisor.recalculate`` uses as the new origin)."""
        k = math.cos(math.radians(lat))
        return min(
            (n for n in self.network.nodes if self.network.graph.degree(n) > 0),
            key=lambda n: (self.network.nodes[n]["lat"] - lat) ** 2 + ((self.network.nodes[n]["lon"] - lon) * k) ** 2,
        )

    def next_node(self, path: list[str], lat: float, lon: float) -> str:
        """The first node of ``path`` still ahead of an aircraft at (lat, lon)."""
        _, i = graph_context(self.network, path, lat, lon)
        return path[min(i, len(path) - 1)]

    # --- traffic -------------------------------------------------------------------------
    def traffic(self, planned: Iterable[FlightPlan], t_s: float | None = None) -> TrafficForecast:
        """Planned flights plus, for AeroLink, demand-expected flights (next 30 min) and off-route mass."""
        plans = list(planned)
        if self.method == "aerolink":
            t = datetime.now(UTC).timestamp() if t_s is None else t_s
            with self.lock:
                return self.engine._traffic(plans, t)
        return forecast_traffic(plans, self.b1_capacities())

    def b1_capacities(self) -> dict[str, int]:
        return self.engine._capacities()

    @staticmethod
    def level(forecast: TrafficForecast, node: str, t_s: float) -> TrafficLevel:
        return cast(TrafficLevel, forecast.level_at(node, t_s, "p90"))

    def edge_levels(self, forecast: TrafficForecast, u: str, v: str, t_s: float) -> TrafficLevel:
        """An edge is as busy as the busier of its two ends (the forecast is per node)."""
        order = {"low": 0, "moderate": 1, "heavy": 2}
        return max((self.level(forecast, n, t_s) for n in (u, v)), key=order.__getitem__)

    def cones(self, flights: list[tuple[str, str, str, FlightPlan]], t0: float) -> list[dict[str, Any]]:
        """Where each planned flight is expected to be at +0..+45 min, with an uncertainty range.

        Position along the route from the planned ETAs; p10/p90 are how far behind/ahead it could
        be, using the same spread as the traffic forecast (8 % of the time left, at least 30 s).
        """
        out = []
        for aircraft_id, status, vehicle_type, plan in flights:
            times = self._leg_times(plan)
            steps = []
            for m in CONE_STEPS_MIN:
                t = t0 + m * 60.0
                spread = max(30.0, (times[-1] - t) * 0.08)
                steps.append(
                    {
                        "min": m,
                        "p10": self._position_at(plan.path, times, t - spread),
                        "p50": self._position_at(plan.path, times, t),
                        "p90": self._position_at(plan.path, times, t + spread),
                    }
                )
            out.append(
                {
                    "aircraft_id": aircraft_id,
                    "flight_id": str(plan.flight_id),
                    "status": status,
                    "vehicle_type": vehicle_type,
                    "path": plan.path,
                    "steps": steps,
                }
            )
        return out

    @staticmethod
    def cone_polygon(steps: list[dict[str, Any]], min_half_width_m: float = 100.0) -> list[list[float]]:
        """A closed [lon, lat] ring around a cone's steps: a ribbon that widens with the p10-p90 spread.

        The p10/p90 positions lie on the route, so the ribbon's width is the along-route spread
        (at least ``min_half_width_m``) laid out across the direction of travel.
        """
        ref_lat = steps[0]["p50"][0]
        kx = 111_320.0 * math.cos(math.radians(ref_lat))
        centre = [(st["p50"][1] * kx, st["p50"][0] * 111_320.0) for st in steps]
        left, right = [], []
        for i, (x, y) in enumerate(centre):
            (x0, y0), (x1, y1) = centre[max(0, i - 1)], centre[min(len(centre) - 1, i + 1)]
            norm = math.hypot(x1 - x0, y1 - y0) or 1.0
            nx, ny = -(y1 - y0) / norm, (x1 - x0) / norm
            a, b = steps[i]["p10"], steps[i]["p90"]
            w = max(min_half_width_m, 0.5 * distance_m(a[0], a[1], b[0], b[1]))
            left.append((x + nx * w, y + ny * w))
            right.append((x - nx * w, y - ny * w))
        ring = [*left, *reversed(right)]
        ring.append(ring[0])
        return [[round(x / kx, 5), round(y / 111_320.0, 5)] for x, y in ring]

    @staticmethod
    def _leg_times(plan: FlightPlan) -> list[float]:
        times = [plan.departure_time]
        for n in plan.path[1:]:
            times.append(max(times[-1], plan.eta_by_node.get(n, times[-1])))
        return times

    def _position_at(self, path: list[str], times: list[float], t: float) -> list[float]:
        """[lat, lon] on the corridor geometry at time t (clamped to the route's ends)."""
        if t <= times[0] or len(path) < 2:
            n = self.network.nodes[path[0]]
            return [round(n["lat"], 5), round(n["lon"], 5)]
        for i in range(1, len(path)):
            if t <= times[i]:
                f = (t - times[i - 1]) / max(1e-6, times[i] - times[i - 1])
                return self._along_edge(path[i - 1], path[i], f)
        n = self.network.nodes[path[-1]]
        return [round(n["lat"], 5), round(n["lon"], 5)]

    def _along_edge(self, u: str, v: str, f: float) -> list[float]:
        pts = self.network.edge_between(u, v).oriented_geometry(u)  # (lon, lat)
        seg = [distance_m(a[1], a[0], b[1], b[0]) for a, b in zip(pts, pts[1:], strict=False)]
        target, run = f * sum(seg), 0.0
        for (a, b), d in zip(zip(pts, pts[1:], strict=False), seg, strict=False):
            if run + d >= target and d > 0:
                w = (target - run) / d
                return [round(a[1] + w * (b[1] - a[1]), 5), round(a[0] + w * (b[0] - a[0]), 5)]
            run += d
        return [round(pts[-1][1], 5), round(pts[-1][0], 5)]

    def predict_arrival(
        self, flight: FlightPlan, vehicle_type: VehicleType, planned: list[FlightPlan], from_node: str, t_s: float
    ) -> tuple[float, float, float]:
        """(p10, p50, p90) arrival at the destination, waits included, given everyone else's flights.

        This is what changes when other traffic appears on a shared pad or junction.
        """
        path = (
            [from_node, *flight.path[flight.path.index(from_node) + 1 :]] if from_node in flight.path else flight.path
        )
        with self.lock:
            if self.method == "aerolink":
                eng = self.engine
                eng.snapshot = planned
                pred = eng.etas(path, t_s, vehicle_type, planned)
                o = eng._score(path, pred, vehicle_type, t_s, eng._traffic(planned, t_s), planned)
                wait = o.predicted_pad_wait_s + o.predicted_junction_wait_s
                return o.eta_window_s[0] + wait, o.eta_s + wait, o.eta_window_s[1] + wait
            b = self.b1._score(path, vehicle_type, t_s, planned)
            wait = b.predicted_pad_wait_s + b.predicted_junction_wait_s
            return b.eta_window_s[0] + wait, b.eta_s + wait, b.eta_window_s[1] + wait

    # --- options --------------------------------------------------------------------------------
    def _options(
        self, origin: str, destination: str, vt: VehicleType, t_s: float, planned: list[FlightPlan]
    ) -> list[Option]:
        with self.lock:
            if self.method == "aerolink":
                eng = self.engine
                eng.snapshot = planned
                return [
                    Option(
                        o.path,
                        o.eta_s,
                        o.predicted_pad_wait_s + o.predicted_junction_wait_s,
                        o.bunching_risk,
                        o.reason,
                        o.cost_terms["mu"],
                        cast(TrafficLevel, o.traffic),
                    )
                    for o in eng.request_routes(origin, destination, vt, t_s, planned)
                ]
            forecast = self.traffic(planned, t_s)
            return [
                Option(
                    o.path,
                    o.eta_s,
                    o.predicted_pad_wait_s + o.predicted_junction_wait_s,
                    0.0,
                    o.reason,
                    0.0,
                    self.level(forecast, destination, o.eta_s),
                )
                for o in self.b1.request_routes(origin, destination, vt, t_s, planned=planned)
            ]

    def _base(self, request_id: UUID, aircraft_id: str, now: datetime, revision: int) -> dict[str, Any]:
        return dict(
            schema_version=SCHEMA_VERSION,
            message_id=uuid4(),
            request_id=request_id,
            aircraft_id=aircraft_id,
            revision=revision,
            method=self.method,
            forecast_time=now,
            expires_at=now + timedelta(seconds=RESPONSE_TTL_S),
            forecaster=self.forecaster.name if self.method == "aerolink" else "none",
            graph_version=self.settings.graph_version,
            constraints_cycle=self.settings.constraints_cycle,
        )

    @staticmethod
    def _no_route(base: dict[str, Any], reason: str) -> RouteResponse:
        return RouteResponse(**base, status="no_route", summary=None, reason=reason[:200], routes=[])

    def plan(
        self,
        *,
        request_id: UUID,
        aircraft_id: str,
        vehicle_type: VehicleType,
        origin: str | None,
        destination: str,
        now: datetime,
        planned: list[FlightPlan],
        revision: int = 0,
        note: str | None = None,
    ) -> RouteResponse:
        t_s = now.timestamp()
        base = self._base(request_id, aircraft_id, now, revision)
        if destination not in self.network.nodes:
            return self._no_route(base, f"unknown destination {destination}")
        if vehicle_type not in self.network.serves(destination):
            return self._no_route(base, f"{destination} does not serve {vehicle_type} aircraft")
        if origin is None:
            return self._no_route(base, "no position fix within range of the corridor network")
        if origin == destination:
            return self._no_route(base, "already at the destination")
        options = self._options(origin, destination, vehicle_type, t_s, planned)
        if not options:
            return self._no_route(base, f"no corridor path from {origin} to {destination}")
        return self._respond(base, options, vehicle_type, t_s, planned, note)

    def _respond(
        self,
        base: dict[str, Any],
        options: list[Option],
        vt: VehicleType,
        t_s: float,
        planned: list[FlightPlan],
        note: str | None = None,
    ) -> RouteResponse:
        routes = []
        forecast = self.traffic(planned, t_s)  # per-leg levels at each leg's arrival time (U-API §1)
        for rank, o in enumerate(options[:3], start=1):
            est = max(0.0, o.eta_s - t_s)
            risk = min(1.0, max(0.0, o.risk))
            etas = self._etas(o.path, t_s, vt, planned)
            eta_s = {e.node: e.eta_p50.timestamp() for e in etas}
            routes.append(
                Route(
                    route_id=uuid4(),
                    rank=rank,
                    label="fastest" if rank == 1 else "alternative",
                    distance_m=self.network.route_length_m(o.path),
                    est_time_s=est,
                    expected_wait_s=o.wait_s,
                    bunching_risk=risk,
                    traffic=o.traffic,
                    cost_terms=CostTerms(est_time_s=est, expected_wait_s=o.wait_s, mu=o.mu, bunching_risk=risk),
                    reason=(o.reason or "shortest corridor path")[:200],
                    eta=etas,
                    waypoints=self._waypoints(o.path),
                    segments=[
                        Segment.model_validate(seg)
                        for seg in route_segments(o.path, eta_s, lambda u, v, t: self.edge_levels(forecast, u, v, t))
                    ],
                )
            )
        resp = RouteResponse(**base, status="ok", summary=None, reason=None, routes=routes)
        text = template_summary(resp.model_dump(mode="json"), self.names)  # template first: never delays options
        if note:
            text = f"{note} {text}"
        return resp.model_copy(update={"summary": text[:400]})

    def llm_revision(self, resp: RouteResponse) -> RouteResponse | None:
        """The same options with an LLM summary that passed the number check, as the next revision.

        None when no LLM is configured or its text was rejected (the template already went out).
        """
        if self.llm is None or resp.status != "ok":
            return None
        s = summarize(resp.model_dump(mode="json"), self.names, llm=self.llm)
        if s.source != "llm":
            return None
        return resp.model_copy(
            update={
                "summary": s.text,
                "revision": resp.revision + 1,
                "message_id": uuid4(),
                "forecast_time": resp.forecast_time,
            }
        )

    def _etas(self, path: list[str], t_s: float, vt: VehicleType, planned: list[FlightPlan]) -> list[Eta]:
        with self.lock:
            if self.method == "aerolink":
                pred = self.engine.etas(path, t_s, vt, planned)
            else:  # classical navigation has no learned ETA: constant cruise speed
                pred = self.registry.get("cv").predict_eta(path, t_s, vt)
        return [
            Eta(node=e.node, eta_p10=ts(e.eta_p10), eta_p50=ts(e.eta_p50), eta_p90=ts(e.eta_p90))
            for e in pred.etas
            if e.node != path[0]
        ] or [Eta(node=path[-1], eta_p10=ts(t_s), eta_p50=ts(t_s), eta_p90=ts(t_s))]

    def _waypoints(self, path: list[str]) -> list[tuple[float, float, float]]:
        """The corridor polyline at each edge's band mid-altitude: what a map draws."""
        out: list[tuple[float, float, float]] = []
        for u, v in zip(path, path[1:], strict=False):
            edge = self.network.edge_between(u, v)
            alt = (edge.band_floor_ft + edge.band_ceiling_ft) / 2 * FT_TO_M
            pts = edge.oriented_geometry(u)
            for lon, lat in pts if not out else pts[1:]:
                out.append((lat, lon, alt))
        return out

    def eta_by_node(self, route: Route) -> dict[str, float]:
        return {e.node: e.eta_p50.timestamp() for e in route.eta}

    def etas_for_path(
        self, path: list[str], t_s: float, vt: VehicleType, planned: list[FlightPlan]
    ) -> dict[str, float]:
        """p50 ETA per node of a path the caller already flies (a simulated aircraft's plan)."""
        return {e.node: e.eta_p50.timestamp() for e in self._etas(path, t_s, vt, planned)}

    # --- en route -----------------------------------------------------------------------------------
    def suggest(
        self,
        aircraft_id: str,
        from_node: str,
        vehicle_type: VehicleType,
        planned: list[FlightPlan],
        flight: FlightPlan | None = None,
        revision: int = 1,
        now: datetime | None = None,
        speed_mps: float | None = None,
    ) -> RouteResponse | None:
        """A better route for an active flight, or None.

        ``planned`` are everyone else's flights (the aircraft does not queue behind itself).
        Classical navigation makes no suggestions after departure (ARCHITECTURE §6.7).
        """
        if self.method != "aerolink" or flight is None:
            return None
        now = now or datetime.now(UTC)
        t_s = now.timestamp()
        with self.lock:
            eng = self.engine
            eng.snapshot = planned
            if eng.suggest_live(flight, from_node, vehicle_type, t_s, planned, speed_mps) is None:
                return None
        options = self._options(from_node, flight.destination_node, vehicle_type, t_s, planned)
        if not options:
            return None
        base = self._base(flight.request_id, aircraft_id, now, revision)
        return self._respond(base, options, vehicle_type, t_s, planned, note="Better route found.")

    def alternatives(
        self,
        *,
        flight: FlightPlan,
        aircraft_id: str,
        vehicle_type: VehicleType,
        from_node: str,
        planned: list[FlightPlan],
        revision: int,
        now: datetime,
    ) -> RouteResponse | None:
        """The route engine's options from ``from_node`` to the flight's destination, for ATC reroute proposals.

        The same engine and response shape as ``suggest``, but without its >=10 % gate: a proposal can be made
        to avoid a predicted conflict even when the route is not much faster. Nothing is applied here.
        """
        t_s = now.timestamp()
        if from_node == flight.destination_node:
            return None
        options = self._options(from_node, flight.destination_node, vehicle_type, t_s, planned)
        if not options:
            return None
        base = self._base(flight.request_id, aircraft_id, now, revision)
        return self._respond(base, options, vehicle_type, t_s, planned, note="Alternative route on predicted traffic.")

    # --- off route ------------------------------------------------------------------------------------
    def arrival_window(self, path: list[str], vt: VehicleType, t0: float) -> tuple[float, float] | None:
        """(p50, p90) arrival at the destination as predicted at ``t0``: the schedule check's reference."""
        gbm = self.registry.get("aerolink-gbm") if self.registry.loaded("aerolink-gbm") else None
        if gbm is None:
            return None
        with self.lock:
            e = gbm.predict_eta(path, t0, vt).etas[-1]
        return e.eta_p50, e.eta_p90

    def detect_off_route(
        self,
        path: list[str],
        vt: VehicleType,
        times: list[float],
        track: list[tuple[float, float, float]],
        window: tuple[float, float] | None,
    ) -> bool | None:
        """AeroLink's off-route rule on the live track; None when it cannot be evaluated (no models/data)."""
        models, gbm = (
            self.registry.offroute,
            self.registry.get("aerolink-gbm") if self.registry.loaded("aerolink-gbm") else None,
        )
        if models is None or gbm is None or len(times) < 40:
            return None
        t_in = np.asarray(times, dtype=float)
        xyz_in = np.asarray(track, dtype=float)
        t = np.floor(t_in[0]) + np.arange(int(t_in[-1] - np.floor(t_in[0])) + 1, dtype=float)  # 1 Hz grid
        xyz = np.column_stack([np.interp(t, t_in, xyz_in[:, k]) for k in range(3)])
        graphs = [graph_context(self.network, path, float(p[0]), float(p[1]))[0] for p in xyz]
        det = detect(
            xyz, t, route_polyline(self.network, path), graphs, gbm, models, vt,
            vehicle_cfg(vt).cruise_speed_mps, window,
        )  # fmt: skip
        return bool(det.ai[-1])

    def classical_off_route(
        self, path: list[str], lat: float, lon: float, now_s: float, eta_dest_s: float | None
    ) -> bool:
        """B1's fixed rule (ARCHITECTURE §6.6): > 300 m from the route, or past the ±60 s arrival window."""
        cfg = self.b1.cfg
        best = cross_track_m(lat, lon, route_polyline(self.network, path))
        if best > cfg.off_route_distance_m:
            return True
        # Early arrival cannot be seen before it happens; a late one is: still flying after the window.
        return eta_dest_s is not None and now_s > eta_dest_s + cfg.eta_window_s

    def mark_off_route(self, aircraft_id: str, lat: float, lon: float, speed_mps: float) -> None:
        with self.lock:
            self.engine.off_route[aircraft_id] = (lat, lon, speed_mps)

    def clear_off_route(self, aircraft_id: str) -> None:
        with self.lock:
            for eng in self._engines.values():
                eng.off_route.pop(aircraft_id, None)

    def recalculate(
        self,
        *,
        flight: FlightPlan,
        aircraft_id: str,
        vehicle_type: VehicleType,
        lat: float,
        lon: float,
        planned: list[FlightPlan],
        revision: int,
        now: datetime,
        speed_mps: float | None = None,
    ) -> tuple[RouteResponse, str] | None:
        """Fresh options from the aircraft's position (``AeroLinkAdvisor.recalculate``), as a route-response."""
        t_s = now.timestamp()
        if self.method != "aerolink":  # classical: B1's options from the nearest node, same response shape
            origin = self.nearest_node(lat, lon)
            options = self._options(origin, flight.destination_node, vehicle_type, t_s, planned)
            if not options:
                return None
            base = self._base(flight.request_id, aircraft_id, now, revision)
            resp = self._respond(
                base, options, vehicle_type, t_s, planned, note="Off route: recalculated from your position."
            )
            return resp, options[0].path[0]
        with self.lock:
            eng = self.engine
            eng.snapshot = planned
            mine = eng.off_route.pop(aircraft_id, None)  # an aircraft does not queue behind its own shadow
            try:
                opts = eng.recalculate(
                    str(flight.flight_id),
                    lat,
                    lon,
                    flight.destination_node,
                    vehicle_type,
                    t_s,
                    current_speed_mps=speed_mps,
                )
            finally:
                if mine is not None:
                    eng.off_route[aircraft_id] = mine
            if not opts:
                return None
            options = [
                Option(o.path, o.eta_s, o.predicted_pad_wait_s + o.predicted_junction_wait_s, o.bunching_risk,
                       o.reason, o.cost_terms["mu"], cast(TrafficLevel, o.traffic))
                for o in opts
            ]  # fmt: skip
        base = self._base(flight.request_id, aircraft_id, now, revision)
        resp = self._respond(
            base, options, vehicle_type, t_s, planned, note="Off route: recalculated from your position."
        )
        return resp, options[0].path[0]
