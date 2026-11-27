"""Per-step world update: orchestrates operator and pilot agents (PLAN E3.3 T0).

The controller now only owns the step order and public scripting API. All
behaviour lives in:

* ``Operator``: pad queues, medical dispatch
* ``Pilot``: one per aircraft, route choice, suggestions, destination changes,
  spacing, metering, energy

This keeps later phases (E4 route engine, E5 off-route detection) from editing
one large file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from aerolink.contracts import VehicleType
from aerolink.network import Network
from aerolink.sim import pads as padlib
from aerolink.sim.clock import SimClock
from aerolink.sim.demand import DemandConfig
from aerolink.sim.operator import Operator
from aerolink.sim.pilot import Pilot
from aerolink.sim.separation import ConflictProbe, following_caps, reset_velocity_caps
from aerolink.sim.spacing import SpacingMonitor
from aerolink.sim.state import AircraftState, WorldState
from aerolink.sim.traffic import TrafficIndex
from aerolink.sim.world_config import (
    BackgroundTrafficConfig,
    EnergyConfig,
    PadBookingConfig,
    SeparationConfig,
    WorldToggles,
)


class RouteAdvisor(Protocol):
    """What the simulator needs from a route service."""

    def request_routes(self, origin: str, destination: str, vehicle_type: VehicleType, t_s: float) -> list[list[str]]:
        """Candidate node paths, best first; empty if none."""
        ...

    def suggest(self, ac: AircraftState, from_node: str, t_s: float) -> list[str] | None:
        """Optional reroute from ``from_node`` to the current destination."""
        ...


class ShortestPathAdvisor:
    """Offline advisor: k shortest paths; suggests the best path when it is
    strictly shorter than the remainder of the current route."""

    def __init__(self, network: Network, k: int = 3):
        self.network = network
        self.k = k

    def request_routes(self, origin: str, destination: str, vehicle_type: VehicleType, t_s: float) -> list[list[str]]:
        return self.network.shortest_paths(origin, destination, vehicle_type=vehicle_type, k=self.k)

    def suggest(self, ac: AircraftState, from_node: str, t_s: float) -> list[str] | None:
        plan = ac.active_flight
        if plan is None:
            return None
        paths = self.network.shortest_paths(from_node, plan.destination_node, vehicle_type=ac.vehicle_type, k=1)
        if not paths:
            return None
        current = [from_node] + [leg.end_node for leg in ac.legs[ac.leg_index :]]
        if paths[0] == current:
            return None
        if self.network.route_length_m(paths[0]) + 1e-6 >= self.network.route_length_m(current):
            return None
        return paths[0]


@dataclass(frozen=True)
class ControllerConfig:
    dt_s: float = 1.0
    start_local_hour: float = 7.0
    turnaround_s: float = padlib.DEFAULT_TURNAROUND_S
    max_hold_s: float = 600.0
    suggestion_acceptance_rate: float = 1.0
    destination_change_probability: float = 0.02
    cruise_speed_sigma: float = 0.03
    demand: DemandConfig = DemandConfig()
    world: WorldToggles = field(default_factory=WorldToggles)
    separation: SeparationConfig = field(default_factory=SeparationConfig)
    energy: EnergyConfig = field(default_factory=EnergyConfig)
    pad_booking: PadBookingConfig = field(default_factory=PadBookingConfig)
    background_traffic: BackgroundTrafficConfig = field(default_factory=BackgroundTrafficConfig)
    schedule: list = field(default_factory=list)
    medical_incidents: list = field(default_factory=list)

    @property
    def background_traffic_aircraft(self):
        return []


class Controller:
    def __init__(
        self,
        network: Network,
        world: WorldState,
        clock: SimClock,
        advisor: RouteAdvisor,
        cfg: ControllerConfig,
        background_aircraft: list | None = None,
    ):
        self.network = network
        self.world = world
        self.clock = clock
        self.advisor = advisor
        self._cfg = cfg
        self.operator = Operator(network, world, cfg)
        self.pilots: dict[str, Pilot] = {
            aid: Pilot(aid, network, world, advisor, cfg) for aid in sorted(world.aircraft)
        }
        for p in self.pilots.values():
            p.init_demand()
        self.spacing: SpacingMonitor | None = None
        self.probe: ConflictProbe | None = None
        self.background_aircraft: list = list(background_aircraft or cfg.background_traffic_aircraft)

    @property
    def cfg(self) -> ControllerConfig:
        return self._cfg

    @cfg.setter
    def cfg(self, value: ControllerConfig) -> None:
        self._cfg = value
        self.operator.cfg = value
        self.operator.schedule = getattr(value, "schedule", [])
        for p in self.pilots.values():
            p.cfg = value

    # ------------------------------------------------------------ public API
    def request_trip(self, aircraft_id: str, destination: str) -> bool:
        """Scripted passenger trip for a parked aircraft (scenarios and tests)."""
        ac = self.world.aircraft[aircraft_id]
        if ac.status != "parked" or ac.current_node == destination:
            return False
        return self.pilots[aircraft_id]._depart_with_booking(destination, purpose="passenger")

    def dispatch_medical(self, node_id: str) -> None:
        """Scripted medical incident at ``node_id``."""
        self.operator.dispatch_medical(node_id, scripted=True)

    @property
    def pending_dispatches(self) -> list[dict]:
        """Backwards-compatible access to operator dispatch queue."""
        return self.operator.pending_dispatches

    def step(self) -> None:
        dt = self.cfg.dt_s
        self.clock = self.clock.tick()
        self.world.t = self.clock.t
        self.world.wind.resample_gust(self.world.rng)

        self.operator.grant_pads()
        self.operator.clear_pads_for_emergencies(
            lambda ac, site: self.pilots[ac.aircraft_id].depart(site, purpose="reposition", log_unavailable=False)
        )

        # schedule handling must happen before per-step pilot updates so the
        # aircraft is still parked at the scheduled departure time
        if self.cfg.world.schedules:
            self.operator.handle_schedule(
                lambda ac, node, purpose, dispatch_t, **kw: self.pilots[ac.aircraft_id]._depart_with_booking(
                    node, purpose=purpose, dispatch_t=dispatch_t, **kw
                )
            )
        else:
            self.operator.handle_schedule(lambda *args, **kwargs: None)
        self.operator.medical_demand()
        self.operator.resolve_dispatches(
            lambda ac, node, purpose, dispatch_t, **kw: self.pilots[ac.aircraft_id]._depart_with_booking(
                node, purpose=purpose, dispatch_t=dispatch_t, **kw
            )
        )

        toggles = self.cfg.world
        separation_cfg = self.cfg.separation if toggles.separation else None
        if separation_cfg is not None:
            if self.probe is None or self.probe.cfg != separation_cfg:
                self.probe = ConflictProbe(self.network, separation_cfg)
            traffic = TrafficIndex(self.world.aircraft)
            if toggles.background_traffic and self.background_aircraft:
                from aerolink.sim.background import background_tracks, step_background

                bg = step_background(self.background_aircraft, self.world.t, self.network)
                traffic.add_background(background_tracks(bg))
            following_caps(self.world, traffic, separation_cfg)
            self.probe.caps(self.world)

        for aid in sorted(self.world.aircraft):
            ac = self.world.aircraft[aid]
            pilot = self.pilots[aid]
            if ac.status == "parked":
                pilot.parked()
            elif ac.status == "turnaround":
                pilot.turnaround()
            elif ac.status == "landing":
                pilot.landing()
            elif ac.status == "holding":
                pilot.holding()
            else:
                pilot.fly(dt)

        if separation_cfg is not None:
            if self.spacing is None or self.spacing.cfg != separation_cfg:
                self.spacing = SpacingMonitor(self.network, separation_cfg)
            self.spacing.step(self.world, self.log)
            reset_velocity_caps(self.world.aircraft)

    # ------------------------------------------------------------ diagnostics
    def log(self, kind: str, aircraft_id: str | None = None, **data) -> None:
        event = {"t": round(self.world.t, 6), "kind": kind}
        if aircraft_id is not None:
            event["aircraft_id"] = aircraft_id
        event.update(data)
        self.world.events.append(event)
