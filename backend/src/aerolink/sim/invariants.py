"""Simulator invariant checker: the test oracle (hardening plan §4).

``InvariantChecker.check_step(world)`` runs after every controller step and
records every violated invariant as a ``Violation``; ``check_events`` runs once
on the final event log. ``run_simulation(..., check_invariants=True)`` wires it
in. The checker only reads state, so it never changes a run's output.

Tolerances are explicit constants so a failing check says exactly which bound
was crossed.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass

from aerolink.network import Network
from aerolink.sim import events as evlib
from aerolink.sim.physics import (
    FT_TO_M,
    MAX_ACCEL_MPS2,
    MAX_CLIMB_MPS,
    MAX_DECEL_MPS2,
    MAX_DESCENT_MPS,
    MAX_GROUND_SPEED_MPS,
    PAD_ALT_M,
    flight_level_m,
    segment_length_m,
)
from aerolink.sim.state import AIRBORNE, ON_PAD, AircraftState, WorldState
from aerolink.sim.world_config import SeparationConfig
from aerolink.vehicle import vehicle_cfg

EPS = 1e-6
RANGE_EPS_M = 1.0  # slack on the range guard (rounding along multi-leg plans)
GEOMETRY_SLACK = 0.01  # polyline vs planning length differ by < 0.1% on central-fl-v1
DISPLACEMENT_SLACK_M = 1.0
SPAM_WINDOW_S = 60.0
SPAM_MAX = 5  # same (aircraft, kind) more often than this in one window is spam
MAX_DIRECTIONAL_OFFSET_M = 45.0  # slack above the band ceiling
STUCK_S = 600.0  # flying (not holding) at zero speed longer than this is a gridlock
STUCK_SPEED_MPS = 0.5
M_PER_DEG = math.pi / 180.0 * 6_371_008.8

FLYING = frozenset({"climbing", "cruise", "descending"})

LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "parked": frozenset({"parked", "climbing"}),
    "climbing": FLYING | {"holding", "landing"},
    "cruise": FLYING | {"holding", "landing"},
    "descending": FLYING | {"holding", "landing"},
    "holding": FLYING | {"holding"},
    "landing": frozenset({"landing", "turnaround"}),
    "turnaround": frozenset({"turnaround", "parked"}),
}


@dataclass(frozen=True)
class Violation:
    t: float
    rule: str
    aircraft_id: str | None
    detail: str

    def __str__(self) -> str:
        who = f" {self.aircraft_id}" if self.aircraft_id else ""
        return f"t={self.t:g}{who} [{self.rule}] {self.detail}"


@dataclass(frozen=True)
class _Snap:
    status: str
    lat: float
    lon: float
    alt: float
    v: float


class InvariantChecker:
    def __init__(
        self,
        network: Network,
        dt_s: float,
        max_violations: int = 200,
        separation: SeparationConfig | None = None,
    ):
        """``separation`` (when the world's separation toggle is on) adds the
        ``loss_of_separation`` rule, checked independently of the simulator's
        own ``spacing.SpacingMonitor``."""
        self.network = network
        self.separation = separation
        self._los_pairs: set[tuple[str, str]] = set()
        self._stopped_s: dict[str, float] = {}
        self.dt = dt_s
        self.max_violations = max_violations
        self.violations: list[Violation] = []
        self._prev: dict[str, _Snap] = {}
        self._sites = {n for n, p in network.nodes.items() if "waypoint" not in p.get("types", [])}
        self._home_reachable: dict[tuple[str, str], bool] = {}

    # ------------------------------------------------------------------ helpers
    def _fail(self, t: float, rule: str, aid: str | None, detail: str) -> None:
        if len(self.violations) < self.max_violations:
            self.violations.append(Violation(round(t, 6), rule, aid, detail))

    @property
    def ok(self) -> bool:
        return not self.violations

    # --------------------------------------------------------------- per step
    def check_step(self, world: WorldState) -> None:
        t = world.t
        for aid in sorted(world.aircraft):
            ac = world.aircraft[aid]
            prev = self._prev.get(aid)
            self._check_transition(t, ac, prev)
            self._check_kinematics(t, ac, prev)
            self._check_location(t, ac, world)
            self._check_plan(t, ac)
            self._check_energy(t, ac)
            self._check_medical_home(t, ac)
            self._check_stuck(t, ac)
            self._prev[aid] = _Snap(ac.status, ac.position.lat, ac.position.lon, ac.position.alt_msl_m, ac.velocity_mps)
        self._check_pads(t, world)
        if self.separation is not None:
            self._check_separation(t, world, self.separation)

    def _check_stuck(self, t: float, ac: AircraftState) -> None:
        aid = ac.aircraft_id
        if ac.status in FLYING and ac.velocity_mps < STUCK_SPEED_MPS:
            before = self._stopped_s.get(aid, 0.0)
            now = before + self.dt
            self._stopped_s[aid] = now
            if before <= STUCK_S < now:
                self._fail(t, "stuck", aid, f"{ac.status} at zero speed for {STUCK_S:g} s")
        else:
            self._stopped_s.pop(aid, None)

    def _pad_area(self, ac: AircraftState, radius_m: float) -> str | None:
        leg = ac.leg
        if leg is None:
            return None
        if ac.leg_index == len(ac.legs) - 1 and ac.remaining_route_m() < radius_m and leg.end_node in self._sites:
            return leg.end_node
        if ac.leg_index == 0 and ac.distance_along_edge_m < radius_m and leg.start_node in self._sites:
            return leg.start_node
        return None

    def _check_separation(self, t: float, world: WorldState, cfg: SeparationConfig) -> None:
        """Every pair of airborne aircraft closer than ``min_separation_m``
        horizontally and ``min_vertical_separation_m`` vertically is a violation,
        once per encounter, unless both are in the same site's pad area."""
        air = [world.aircraft[a] for a in sorted(world.aircraft) if world.aircraft[a].airborne]
        now: set[tuple[str, str]] = set()
        for i, a in enumerate(air):
            k = math.cos(math.radians(a.position.lat))
            for b in air[i + 1 :]:
                dv = abs(a.position.alt_msl_m - b.position.alt_msl_m)
                if dv >= cfg.min_vertical_separation_m:
                    continue
                dx = (a.position.lon - b.position.lon) * k * M_PER_DEG
                dy = (a.position.lat - b.position.lat) * M_PER_DEG
                dh = math.hypot(dx, dy)
                if dh >= cfg.min_separation_m:
                    continue
                area = self._pad_area(a, cfg.pad_area_m)
                if area is not None and area == self._pad_area(b, cfg.pad_area_m):
                    continue
                pair = (a.aircraft_id, b.aircraft_id)
                now.add(pair)
                if pair not in self._los_pairs:
                    self._fail(
                        t,
                        "loss_of_separation",
                        a.aircraft_id,
                        f"{b.aircraft_id} {dh:.0f} m horizontal, {dv:.0f} m vertical ({a.status}/{b.status})",
                    )
        self._los_pairs = now

    def _check_transition(self, t: float, ac: AircraftState, prev: _Snap | None) -> None:
        if prev is None:
            if ac.status not in ON_PAD:
                self._fail(t, "start_on_pad", ac.aircraft_id, f"first status {ac.status}")
            return
        if ac.status not in LEGAL_TRANSITIONS.get(prev.status, frozenset()):
            self._fail(t, "status_transition", ac.aircraft_id, f"{prev.status} -> {ac.status}")

    def _check_kinematics(self, t: float, ac: AircraftState, prev: _Snap | None) -> None:
        aid = ac.aircraft_id
        if not 0.0 <= ac.velocity_mps <= MAX_GROUND_SPEED_MPS + EPS:
            self._fail(t, "ground_speed_range", aid, f"v={ac.velocity_mps:.3f}")
        if ac.airborne and ac.position.alt_msl_m < PAD_ALT_M - EPS:
            self._fail(t, "below_pad_alt", aid, f"alt={ac.position.alt_msl_m:.3f}")
        leg = ac.leg
        if ac.airborne and leg is not None and prev is not None:
            # Entering a lower band the aircraft starts above it and descends at
            # most MAX_DESCENT_MPS (documented simplification), so the rule is
            # "never climb while above the band", not "never above the band".
            ceiling = max(leg.edge.band_ceiling_ft * FT_TO_M, flight_level_m(ac, leg)) + MAX_DIRECTIONAL_OFFSET_M
            alt = ac.position.alt_msl_m
            if alt > ceiling + EPS and alt > prev.alt + EPS:
                self._fail(t, "above_band", aid, f"climbing at alt={alt:.1f} > ceiling {ceiling:.1f}")
        if prev is None or prev.status not in AIRBORNE or ac.status not in AIRBORNE:
            return
        dt = self.dt
        if prev.status in FLYING and ac.status in FLYING:
            dv = ac.velocity_mps - prev.v
            if dv > MAX_ACCEL_MPS2 * dt + EPS:
                self._fail(t, "accel_limit", aid, f"dv={dv:.3f} in {dt:g}s")
            if -dv > MAX_DECEL_MPS2 * dt + EPS:
                self._fail(t, "decel_limit", aid, f"dv={dv:.3f} in {dt:g}s")
        dalt = ac.position.alt_msl_m - prev.alt
        if dalt > MAX_CLIMB_MPS * dt + EPS or -dalt > MAX_DESCENT_MPS * dt + EPS:
            self._fail(t, "vertical_rate", aid, f"dalt={dalt:.3f} in {dt:g}s")
        moved = segment_length_m((prev.lon, prev.lat), (ac.position.lon, ac.position.lat))
        bound = 0.5 * (prev.v + ac.velocity_mps) * dt * (1 + GEOMETRY_SLACK) + DISPLACEMENT_SLACK_M
        if moved > bound:
            self._fail(t, "displacement", aid, f"moved {moved:.1f} m > {bound:.1f} m")

    def _check_location(self, t: float, ac: AircraftState, world: WorldState) -> None:
        aid = ac.aircraft_id
        if ac.status in ON_PAD:
            node = ac.current_node
            if node is None or node not in world.pads:
                self._fail(t, "on_pad_without_site", aid, f"current_node={node}")
                return
            if ac.active_flight is not None or ac.legs:
                self._fail(t, "on_pad_with_flight", aid, f"at {node}")
            if ac.departure_waiting and not ac.on_stand and self.separation is None:
                # with separation on, departure release may hold an aircraft on its pad
                self._fail(t, "waiting_off_stand", aid, f"at {node}")
            p = self.network.nodes[node]
            if (ac.position.lat, ac.position.lon) != (float(p["lat"]), float(p["lon"])):
                self._fail(t, "on_pad_position", aid, f"not at {node}")
            if ac.velocity_mps != 0.0 or ac.vertical_speed_mps != 0.0:
                self._fail(t, "on_pad_moving", aid, f"v={ac.velocity_mps} vs={ac.vertical_speed_mps}")
        else:
            if ac.current_node is not None:
                self._fail(t, "airborne_with_node", aid, f"current_node={ac.current_node}")
            if ac.on_stand:
                self._fail(t, "airborne_on_stand", aid, ac.status)
            if ac.active_flight is None or ac.leg is None:
                self._fail(t, "airborne_without_plan", aid, ac.status)

    def _check_plan(self, t: float, ac: AircraftState) -> None:
        plan = ac.active_flight
        if plan is None or not ac.legs:
            return
        aid = ac.aircraft_id
        legs = ac.legs
        for a, b in zip(legs, legs[1:], strict=False):
            if a.end_node != b.start_node:
                self._fail(t, "legs_contiguous", aid, f"{a.end_node} != {b.start_node}")
        if legs[-1].end_node != plan.destination_node:
            self._fail(t, "legs_end_at_destination", aid, f"{legs[-1].end_node} != {plan.destination_node}")
        if plan.destination_node not in self.network.nodes:
            self._fail(t, "unknown_destination", aid, plan.destination_node)
        elif ac.vehicle_type not in self.network.serves(plan.destination_node):
            self._fail(t, "destination_serves_type", aid, f"{plan.destination_node} does not serve {ac.vehicle_type}")
        transit = [leg.end_node for leg in legs[ac.leg_index : -1] if leg.end_node in self._sites]
        if transit:
            self._fail(t, "site_transit", aid, f"plan transits landing sites {transit}")
        if not 0 <= ac.distance_along_edge_m <= legs[ac.leg_index].length_m + EPS:
            self._fail(t, "along_edge_range", aid, f"{ac.distance_along_edge_m:.3f}")

    def _check_energy(self, t: float, ac: AircraftState) -> None:
        aid = ac.aircraft_id
        if not 0.0 <= ac.battery_soc <= 1.0:
            self._fail(t, "soc_range", aid, f"soc={ac.battery_soc}")
        if not ac.airborne:
            return
        if ac.battery_soc <= 0.0:
            self._fail(t, "battery_depleted", aid, "airborne at soc 0")
        left = ac.battery_soc * vehicle_cfg(ac.vehicle_type).range_m
        need = ac.remaining_route_m()
        if need > left + RANGE_EPS_M:
            self._fail(t, "range_guard", aid, f"remaining route {need:.0f} m > range left {left:.0f} m")

    def _check_medical_home(self, t: float, ac: AircraftState) -> None:
        if ac.vehicle_type != "medical" or ac.status not in ON_PAD:
            return
        node = ac.current_node
        if node is None or node == ac.home_node:
            return
        key = (node, ac.home_node)
        if key not in self._home_reachable:
            self._home_reachable[key] = bool(self.network.shortest_paths(node, ac.home_node, "medical", k=1))
        if not self._home_reachable[key]:
            self._fail(t, "medical_stranded", ac.aircraft_id, f"at {node}, home {ac.home_node} out of range")

    def _check_pads(self, t: float, world: WorldState) -> None:
        for nid in sorted(world.pads):
            pad = world.pads[nid]
            if not (pad.occupants or pad.queue or pad.stand_occupants):
                continue  # nothing to check at an empty site
            if len(pad.occupants) > pad.capacity:
                self._fail(t, "pad_capacity", None, f"{nid}: {len(pad.occupants)} > {pad.capacity}")
            if len(set(pad.occupants)) != len(pad.occupants) or len(set(pad.queue)) != len(pad.queue):
                self._fail(t, "pad_duplicates", None, nid)
            if len(pad.stand_occupants) > pad.stands:
                self._fail(t, "stand_capacity", None, f"{nid}: {len(pad.stand_occupants)} > {pad.stands}")
            if len(set(pad.stand_occupants)) != len(pad.stand_occupants):
                self._fail(t, "stand_duplicates", None, nid)
            for aid in pad.stand_occupants:
                ac = world.aircraft.get(aid)
                if aid in pad.occupants:
                    self._fail(t, "stand_and_pad", aid, nid)
                if ac is not None and not (ac.on_stand and ac.status in ON_PAD and ac.current_node == nid):
                    self._fail(t, "stale_stand", aid, f"on a stand at {nid} while {ac.status}")
            for aid in pad.occupants:
                ac = world.aircraft.get(aid)
                if ac is None:
                    continue  # scripted placeholders in tests
                if aid in pad.queue:
                    self._fail(t, "queued_and_occupying", aid, nid)
                on_here = ac.status in ON_PAD and ac.current_node == nid and not ac.on_stand
                inbound = ac.airborne and ac.active_flight is not None and ac.active_flight.destination_node == nid
                if not (on_here or (inbound and ac.pad_cleared)):
                    self._fail(t, "leaked_slot", aid, f"holds a slot at {nid} while {ac.status}")
            for aid in pad.queue:
                ac = world.aircraft.get(aid)
                if ac is None:
                    continue
                if not (ac.airborne and ac.active_flight is not None and ac.active_flight.destination_node == nid):
                    self._fail(t, "stale_queue_entry", aid, f"queued at {nid} while {ac.status}")
        for aid in sorted(world.aircraft):
            ac = world.aircraft[aid]
            if ac.status in ON_PAD and ac.current_node in world.pads:
                site = world.pads[ac.current_node]
                held = site.stand_occupants if ac.on_stand else site.occupants
                if aid not in held:
                    self._fail(t, "on_pad_not_occupant", aid, f"at {ac.current_node} (on_stand={ac.on_stand})")
            if ac.status == "landing" or (ac.airborne and ac.pad_cleared):
                dest = ac.active_flight.destination_node if ac.active_flight else None
                if dest is None or dest not in world.pads or aid not in world.pads[dest].occupants:
                    self._fail(t, "cleared_without_slot", aid, f"{ac.status} to {dest}")

    # ------------------------------------------------------------- whole run
    def check_events(self, events: list[dict], world: WorldState) -> None:
        last_t = -math.inf
        recent: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        spam_reported: set[tuple[str, str]] = set()
        departures: dict[str, int] = defaultdict(int)
        arrivals: dict[str, int] = defaultdict(int)
        for e in events:
            t = e["t"]
            kind = e["kind"]
            aid = e.get("aircraft_id")
            if not evlib.valid(kind):
                self._fail(t, "event_kind", aid, kind)
            if t < last_t:
                self._fail(t, "event_order", aid, f"{kind} at {t} after {last_t}")
            last_t = max(last_t, t)
            if aid is not None and aid not in world.aircraft:
                self._fail(t, "event_unknown_aircraft", aid, kind)
            if aid is not None:
                key = (aid, kind)
                q = recent[key]
                q.append(t)
                while q and q[0] <= t - SPAM_WINDOW_S:
                    q.popleft()
                if len(q) > SPAM_MAX and key not in spam_reported:
                    spam_reported.add(key)
                    self._fail(t, "event_spam", aid, f"{kind} x{len(q)} within {SPAM_WINDOW_S:g}s")
            if kind == evlib.DEPARTURE and aid is not None:
                departures[aid] += 1
            elif kind == evlib.ARRIVAL and aid is not None:
                arrivals[aid] += 1
        for aid in sorted(world.aircraft):
            ac = world.aircraft[aid]
            in_air = 1 if ac.active_flight is not None else 0
            if departures[aid] != arrivals[aid] + in_air:
                self._fail(
                    world.t,
                    "flight_balance",
                    aid,
                    f"{departures[aid]} departures, {arrivals[aid]} arrivals, {in_air} in flight",
                )
