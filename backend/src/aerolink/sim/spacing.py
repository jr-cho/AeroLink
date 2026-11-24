"""Spacing and separation measurement (PLAN E3.3 T1, E3.3-T1-plan §3.6).

Read-only metrics, logged **once per encounter**:

* ``loss_of_separation`` — two airborne aircraft closer than
  ``min_separation_m`` horizontally *and* ``min_vertical_separation_m``
  vertically. A closing ``loss_of_separation_end`` carries ``duration_s`` and
  the minimum distances (E3.6 needs severity).
* ``bunching`` — the ARCHITECTURE §3.4 spacing: two aircraft on the same
  oriented edge less than ``spacing_same_edge_m`` apart along track
  (``where="edge"``), or an aircraft passing a waypoint less than
  ``spacing_junction_s`` after another one (``where="junction"``).

Pad sequencing is the vertiport's job, not separation: a pair where both
aircraft are within ``pad_area_m`` of the same site at the end of their
current legs (origin or destination) is not a loss of separation.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from aerolink.network import Network
from aerolink.sim import events as evlib
from aerolink.sim.state import AircraftState, WorldState
from aerolink.sim.world_config import SeparationConfig

M_PER_DEG = math.pi / 180.0 * 6_371_008.8


# an edge bunching encounter ends once the gap opens 10 % past the threshold,
# so a follower hovering around it is one event, not many
BUNCHING_HYSTERESIS = 1.1


def horizontal_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Flat-earth distance; < 0.01% error at the < 5 km scales it is used for."""
    k = math.cos(math.radians(0.5 * (lat1 + lat2)))
    dx = (lon2 - lon1) * M_PER_DEG * k
    dy = (lat2 - lat1) * M_PER_DEG
    return math.hypot(dx, dy)


def site_area(ac: AircraftState, network: Network, sites: set[str], radius_m: float) -> str | None:
    """Return site node if ``ac`` is currently within ``radius_m`` of it."""
    pos = ac.position
    for node in sites:
        npos = network.nodes[node]
        if horizontal_m(pos.lat, pos.lon, npos["lat"], npos["lon"]) <= radius_m:
            return node
    return None


@dataclass
class _Encounter:
    start_t: float
    min_h: float
    min_v: float
    last_t: float


class SpacingMonitor:
    """Counts and logs loss-of-separation / bunching events."""

    def __init__(self, network: Network, cfg: SeparationConfig):
        self.network = network
        self.cfg = cfg
        self.sites: set[str] = {node for node in self.network.nodes if any(self.network.serves(node))}
        self._los: dict[tuple[str, str], _Encounter] = {}
        self._bunch: set[tuple[str, str, str, str]] = set()
        self._last_pass: dict[str, tuple[float, str]] = {}
        self._flown: dict[str, tuple[str, int]] = {}
        self.counts: dict[str, int] = {
            "loss_of_separation": 0,
            "bunching_edge": 0,
            "bunching_junction": 0,
        }

    def step(
        self,
        world: WorldState,
        log: Callable[..., None],
        background_states: list | None = None,
    ) -> None:
        t = world.t
        cfg = self.cfg
        air = [world.aircraft[a] for a in sorted(world.aircraft) if world.aircraft[a].airborne]
        self._junctions(world, air, log)

        # Include live background aircraft in separation measurements.
        all_air: list = list(air)
        if background_states:
            all_air.extend(background_states)

        los_now: set[tuple[str, str]] = set()
        bunch_now: set[tuple[str, str, str, str]] = set()
        areas = {ac.aircraft_id: site_area(ac, self.network, self.sites, cfg.pad_area_m) for ac in air}
        for i, a in enumerate(all_air):
            pa = a.position
            for b in all_air[i + 1 :]:
                pb = b.position
                # cheap reject: 0.02 deg is > 1.7 km in both axes at Florida latitudes
                if abs(pa.lat - pb.lat) > 0.02 or abs(pa.lon - pb.lon) > 0.02:
                    continue
                key = (_aircraft_id(a), _aircraft_id(b))
                h = horizontal_m(pa.lat, pa.lon, pb.lat, pb.lon)
                v = abs(pa.alt_msl_m - pb.alt_msl_m)
                la, lb = _leg(a), _leg(b)
                if (
                    la is not None
                    and lb is not None
                    and hasattr(la, "edge")
                    and hasattr(lb, "edge")
                    and la.edge.edge_id == lb.edge.edge_id
                    and la.start_node == lb.start_node
                ):
                    gap = abs(_distance_along(a) - _distance_along(b))
                    bkey = (_aircraft_id(a), _aircraft_id(b), la.edge.edge_id, la.start_node)
                    ongoing = bkey in self._bunch
                    if gap < cfg.spacing_same_edge_m * (BUNCHING_HYSTERESIS if ongoing else 1.0):
                        bunch_now.add(bkey)
                        if not ongoing:
                            self.counts["bunching_edge"] += 1
                            log(
                                evlib.BUNCHING,
                                _aircraft_id(a),
                                other_id=_aircraft_id(b),
                                where="edge",
                                edge_id=la.edge.edge_id,
                                spacing_m=round(gap, 3),
                            )
                if h >= cfg.min_separation_m or v >= cfg.min_vertical_separation_m:
                    continue
                # background aircraft have no site-area exclusion
                area_a = None if _is_background(a, background_states) else areas.get(a.aircraft_id)
                area_b = None if _is_background(b, background_states) else areas.get(b.aircraft_id)
                if area_a is not None and area_a == area_b:
                    continue
                los_now.add(key)
                enc = self._los.get(key)
                if enc is None:
                    self._los[key] = _Encounter(t, h, v, t)
                    self.counts["loss_of_separation"] += 1
                    log(
                        evlib.LOSS_OF_SEPARATION,
                        _aircraft_id(a),
                        other_id=_aircraft_id(b),
                        horizontal_m=round(h, 3),
                        vertical_m=round(v, 3),
                        status=_status(a),
                        other_status=_status(b),
                    )
                else:
                    enc.min_h = min(enc.min_h, h)
                    enc.min_v = min(enc.min_v, v)
                    enc.last_t = t
        for key in sorted(set(self._los) - los_now):
            enc = self._los.pop(key)
            log(
                evlib.LOSS_OF_SEPARATION_END,
                key[0],
                other_id=key[1],
                duration_s=round(enc.last_t - enc.start_t + 0.0, 6),
                min_horizontal_m=round(enc.min_h, 3),
                min_vertical_m=round(enc.min_v, 3),
            )
        self._bunch = bunch_now

    def _junctions(self, world: WorldState, air: list[AircraftState], log: Callable[..., None]) -> None:
        for ac in air:
            plan = ac.active_flight
            if plan is None:
                continue
            fid = str(plan.flight_id)
            prev = self._flown.get(ac.aircraft_id)
            n0 = prev[1] if prev is not None and prev[0] == fid else len(plan.flown)
            for node in plan.flown[n0:]:
                if node in self.sites:
                    continue
                last = self._last_pass.get(node)
                if last is not None and last[1] != ac.aircraft_id and world.t - last[0] < self.cfg.spacing_junction_s:
                    self.counts["bunching_junction"] += 1
                    log(
                        evlib.BUNCHING,
                        ac.aircraft_id,
                        other_id=last[1],
                        where="junction",
                        node=node,
                        gap_s=round(world.t - last[0], 6),
                    )
                self._last_pass[node] = (world.t, ac.aircraft_id)
            self._flown[ac.aircraft_id] = (fid, len(plan.flown))


def _aircraft_id(obj) -> str:
    return getattr(obj, "aircraft_id", getattr(obj, "id", str(id(obj))))


def _leg(obj):
    return getattr(obj, "leg", None)


def _distance_along(obj) -> float:
    return getattr(obj, "distance_along_edge_m", 0.0)


def _status(obj) -> str:
    return getattr(obj, "status", "background")


def _is_background(obj, background_states: list | None) -> bool:
    if not background_states:
        return False
    return isinstance(obj, type(background_states[0]))
