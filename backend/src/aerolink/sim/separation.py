"""Simulated-pilot separation (PLAN E3.3 T1, docs/E3.3-T1-plan.md §3).

AeroLink does no separation (ARCHITECTURE §18). These are the simulated
pilots' own rules for keeping the physical minimum of ``min_separation_m``
horizontally or ``min_vertical_separation_m`` vertically:

* **Levels** (``physics.directional_alt_m``): the two directions of an edge fly
  the band floor and ceiling, 152.4 m apart.
* **Following** (``following_caps``): a speed cap that keeps
  ``follow_target_distance_m`` behind the aircraft ahead on the route, across
  leg boundaries, up to ``follow_lookahead_m``.
* **Conflict probe** (``probe_caps``): every step, each pair of aircraft that
  could meet within ``PROBE_HORIZON_S`` is predicted along its route (speed,
  braking, climb and glide limits). If the predictions come closer than the
  minimum plus a buffer, one aircraft gives way by slowing down so that it stops
  short of the conflict. It handles junction merges and crossings, level
  changes at nodes, climbs and descents near sites, and corridors whose
  geometry overlaps without a shared node (18 edge pairs on central-fl-v1).
  Who gives way: a moving aircraft gives way to a stopped one; the aircraft
  flying into the other gives way to the one ahead; otherwise passenger before
  medical, then the one farther from the conflict, then the higher id. If the
  one giving way cannot stop short of the conflict, the other aircraft also
  gives way, and they wait while the climbing or descending one reaches its level.
* **Holding stack** (``stack_hold_distance_m``): the k-th aircraft queued for
  a pad holds ``stack_distance_m`` further back than the one in front.

All of it only runs with ``world.separation`` on.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

from aerolink.network import Edge, Network
from aerolink.sim.pads import HOLD_DISTANCE_M, PadState
from aerolink.sim.physics import (
    APPROACH_DECEL_MPS2,
    MAX_ACCEL_MPS2,
    MAX_CLIMB_MPS,
    MAX_DECEL_MPS2,
    MAX_DESCENT_MPS,
    approach_alt_m,
)
from aerolink.sim.state import AircraftState, WindField, WorldState
from aerolink.sim.traffic import TrafficIndex
from aerolink.sim.world_config import SeparationConfig

M_PER_DEG = math.pi / 180.0 * 6_371_008.8

PROBE_HORIZON_S = 40.0
PROBE_STEP_S = 2.0
PROBE_SAMPLES = int(PROBE_HORIZON_S / PROBE_STEP_S)
HORIZONTAL_BUFFER_M = 100.0  # probe looks for min_separation_m + this
# extra vertical margin while climbing or descending (levels themselves are
# only 152.4 m apart, so level flight gets no buffer)
VERTICAL_BUFFER_M = 20.0
HYSTERESIS = 1.15  # severity a resolved pair must predict before it is released
INSIDE_TOLERANCE = 0.05  # severity dip tolerated when already inside (ConflictProbe.worsens)
GIVE_WAY_DECEL_MPS2 = 1.5  # braking used to stop short of a conflict
ROUTE_ORDER_M = 5000.0  # an aircraft this far ahead on your own route is "ahead"
MOVING = frozenset({"climbing", "cruise", "descending"})


def reset_velocity_caps(aircraft: dict[str, AircraftState]) -> None:
    """Clear per-step separation speed caps after physics advance."""
    for ac in aircraft.values():
        ac.velocity_cap_mps = None
        ac.altitude_hold = False


def stack_hold_distance_m(pad: PadState, aircraft_id: str, cfg: SeparationConfig) -> float:
    """Hold point (remaining-route distance) of a queued aircraft: the k-th in the
    queue holds ``k * stack_distance_m`` behind the first one."""
    k = pad.queue.index(aircraft_id) if aircraft_id in pad.queue else 0
    return HOLD_DISTANCE_M + k * cfg.stack_distance_m


def _cap(ac: AircraftState, v: float) -> None:
    if ac.velocity_cap_mps is None or v < ac.velocity_cap_mps:
        ac.velocity_cap_mps = max(0.0, v)


# ------------------------------------------------------------------ following
def leader_ahead(ac: AircraftState, traffic: TrafficIndex, lookahead_m: float) -> tuple[str, float, float] | None:
    """(leader id, gap along the route, leader speed) of the nearest aircraft
    ahead on ``ac``'s own route, searching up to ``lookahead_m`` across legs."""
    if ac.leg is None:
        return None
    offset = -ac.distance_along_edge_m  # route distance from ac to the start of leg i
    for i in range(ac.leg_index, len(ac.legs)):
        if offset > lookahead_m:
            return None
        leg = ac.legs[i]
        best = None
        for tr in traffic.tracks_on_edge(leg.edge, leg.start_node, leg.end_node):
            if tr.aircraft_id == ac.aircraft_id or tr.status == "landing":
                continue
            gap = offset + tr.distance_along_edge_m
            if gap <= 0 or gap > lookahead_m:
                continue
            if best is None or (gap, tr.aircraft_id) < (best[1], best[0]):
                best = (tr.aircraft_id, gap, tr.velocity_mps)
        if best is not None:
            return best
        offset += leg.length_m
    return None


def following_cap_mps(gap_m: float, leader_v: float, cfg: SeparationConfig) -> float:
    """Speed that closes the gap error over ``follow_time_constant_s`` and can
    always brake to the leader's speed before ``min_separation_m``."""
    track = leader_v + (gap_m - cfg.follow_target_distance_m) / cfg.follow_time_constant_s
    brake = leader_v + math.sqrt(2.0 * MAX_DECEL_MPS2 * max(0.0, gap_m - cfg.min_separation_m))
    return max(0.0, min(track, brake))


def following_caps(world: WorldState, traffic: TrafficIndex, cfg: SeparationConfig) -> None:
    for aid in sorted(world.aircraft):
        ac = world.aircraft[aid]
        if ac.status not in MOVING:
            continue
        found = leader_ahead(ac, traffic, cfg.follow_lookahead_m)
        if found is not None:
            _cap(ac, following_cap_mps(found[1], found[2], cfg))


# ------------------------------------------------------------------ probe
class _Geometry:
    """Local metric projection of every oriented edge polyline."""

    def __init__(self, network: Network):
        lats = [float(p["lat"]) for p in network.nodes.values()]
        self.lat0 = sum(lats) / len(lats) if lats else 0.0
        self.k = math.cos(math.radians(self.lat0))
        self._cache: dict[tuple[str, str], tuple[list[float], list[float], list[float]]] = {}

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        return lon * M_PER_DEG * self.k, lat * M_PER_DEG

    def _poly(self, edge: Edge, start: str) -> tuple[list[float], list[float], list[float]]:
        key = (edge.edge_id, start)
        hit = self._cache.get(key)
        if hit is None:
            pts = [self.xy(lat, lon) for lon, lat in edge.oriented_geometry(start)]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            cum = [0.0]
            for i in range(1, len(pts)):
                cum.append(cum[-1] + math.hypot(xs[i] - xs[i - 1], ys[i] - ys[i - 1]))
            total = cum[-1] or 1.0
            hit = (xs, ys, [c / total for c in cum])
            self._cache[key] = hit
        return hit

    def point(self, edge: Edge, start: str, frac: float) -> tuple[float, float]:
        xs, ys, cum = self._poly(edge, start)
        if len(xs) == 1:
            return xs[0], ys[0]
        frac = min(1.0, max(0.0, frac))
        i = min(len(cum) - 2, max(0, bisect.bisect_right(cum, frac) - 1))
        seg = cum[i + 1] - cum[i]
        u = 0.0 if seg <= 0 else (frac - cum[i]) / seg
        return xs[i] + u * (xs[i + 1] - xs[i]), ys[i] + u * (ys[i + 1] - ys[i])


@dataclass
class _Pred:
    s: list[float]  # route distance travelled at each sample
    xs: list[float]
    ys: list[float]
    alts: list[float]
    areas: list[str | None]
    stationary: bool
    off_level: list[bool]  # climbing or descending at the sample


class ConflictProbe:
    """Pairwise look-ahead along routes; one instance per run (caches geometry)."""

    def __init__(self, network: Network, cfg: SeparationConfig):
        self.cfg = cfg
        self.geo = _Geometry(network)
        self._follow: dict[str, float | None] = {}
        self._active: set[tuple[str, str]] = set()
        self._wind: WindField | None = None  # pairs resolved in the previous step
        self.sites = frozenset(n for n, p in network.nodes.items() if "waypoint" not in p.get("types", []))
        self.r_h = cfg.min_separation_m + HORIZONTAL_BUFFER_M
        self.r_v = cfg.min_vertical_separation_m

    # -- route geometry
    def _locate(
        self, ac: AircraftState, s: float, remaining0: float, cur: list[float]
    ) -> tuple[float, float, float, str | None]:
        """(x, y, level, pad area) at route distance ``s`` ahead of ``ac``.
        ``cur`` = [leg index, route distance from ``ac`` to that leg's start] is
        advanced in place, so calls must come with non-decreasing ``s``."""
        legs = ac.legs
        i, base = int(cur[0]), cur[1]
        while i < len(legs) - 1 and s - base > legs[i].length_m:
            base += legs[i].length_m
            i += 1
        d = s - base
        cur[0], cur[1] = i, base
        leg = legs[i]
        frac = d / leg.length_m if leg.length_m > 0 else 1.0
        x, y = self.geo.point(leg.edge, leg.start_node, frac)
        remaining = max(0.0, remaining0 - s)
        area = None
        if i == len(legs) - 1 and remaining < self.cfg.pad_area_m and leg.end_node in self.sites:
            area = leg.end_node
        elif i == 0 and d < self.cfg.pad_area_m and leg.start_node in self.sites:
            area = leg.start_node
        level = approach_alt_m(ac, leg, remaining)
        return x, y, level, area

    def predict(
        self,
        ac: AircraftState,
        mode: str = "nominal",
        limit_m: float | None = None,
        follow_cap: float | None = None,
    ) -> _Pred:
        """``nominal``: fly the planned speed, or the following cap if lower
        (probe caps are ignored, so a waiting aircraft is predicted as if
        released), braking for the pad or hold point. ``brake``: stop as fast as
        possible; ``hold``: also stop climbing or descending; ``level``:
        fly on as nominal without climbing or descending. ``limit_m`` stops
        the aircraft after that much route distance."""
        leg = ac.leg
        assert leg is not None
        remaining = ac.remaining_route_m()
        stop = 0.0 if (ac.pad_cleared or not ac.pad_requested) else ac.hold_remaining_m
        to_pad = max(0.0, remaining - stop)
        to_stop = to_pad if limit_m is None else min(to_pad, limit_m)
        stationary = ac.status not in MOVING
        v = 0.0 if stationary else ac.velocity_mps
        v_target = 0.0 if stationary or mode != "nominal" else leg.planned_speed_mps
        if v_target > 0.0 and self._wind is not None:  # ground speed, course held at the current heading
            v_target = max(0.0, v_target + self._wind.along_track_component(ac.heading_deg))
        if follow_cap is not None:
            v_target = min(v_target, follow_cap)
        s = 0.0
        alt = ac.position.alt_msl_m
        cur = [float(ac.leg_index), -ac.distance_along_edge_m]
        x, y, level, area = self._locate(ac, 0.0, remaining, cur)
        pred = _Pred([0.0], [x], [y], [alt], [area], stationary, [abs(level - alt) > 1.0])
        dt = PROBE_STEP_S
        for _ in range(PROBE_SAMPLES):
            if mode in ("brake", "hold"):
                v1 = max(0.0, v - MAX_DECEL_MPS2 * dt)
            else:
                # mirrors physics.advance under a give-way cap: brake curves are
                # targets, reached within the acceleration limits
                want = min(v_target, math.sqrt(2.0 * APPROACH_DECEL_MPS2 * max(0.0, to_pad - s)))
                if limit_m is not None:
                    want = min(want, math.sqrt(2.0 * GIVE_WAY_DECEL_MPS2 * max(0.0, limit_m - s)))
                v1 = min(want, v + MAX_ACCEL_MPS2 * dt) if v < want else max(want, v - MAX_DECEL_MPS2 * dt)
            s = min(to_stop, s + 0.5 * (v + v1) * dt) if not stationary else 0.0
            v = v1
            x, y, level, area = self._locate(ac, s, remaining, cur)
            frozen = mode in ("hold", "level")  # hold altitude (AircraftState.altitude_hold)
            if (not stationary or ac.status == "holding") and not frozen:
                diff = level - alt
                alt += max(-MAX_DESCENT_MPS * dt, min(MAX_CLIMB_MPS * dt, diff))
            pred.s.append(s)
            pred.xs.append(x)
            pred.ys.append(y)
            pred.alts.append(alt)
            pred.areas.append(area)
            pred.off_level.append(abs(level - alt) > 1.0)
        return pred

    def _severity(self, p: _Pred, q: _Pred, k: int) -> float:
        """< 1 inside the protected volume (min separation + horizontal buffer)."""
        a = p.areas[k]
        if a is not None and a == q.areas[k]:
            return math.inf  # pad sequencing, not separation
        h = math.hypot(p.xs[k] - q.xs[k], p.ys[k] - q.ys[k])
        r_v = self.r_v + VERTICAL_BUFFER_M if (p.off_level[k] or q.off_level[k]) else self.r_v
        return max(h / self.r_h, abs(p.alts[k] - q.alts[k]) / r_v)

    def worsens(self, p: _Pred, q: _Pred, limit: float = 1.0) -> int | None:
        """First sample inside the protected volume (severity < ``limit``) and
        closer than now."""
        now = self._severity(p, q, 0)
        # inside the buffered volume already: tolerate a brief small dip on the
        # way out (the buffers keep it clear of the real minimum)
        bound = min(limit, now - (INSIDE_TOLERANCE if now < 1.0 else 1e-3))
        for k in range(1, len(p.s)):
            if self._severity(p, q, k) < bound:
                return k
        return None

    def first_conflict(self, p: _Pred, q: _Pred, limit: float = 1.0) -> int | None:
        """0 if inside the protected volume now, else the first sample where the
        predictions get inside it (``worsens``)."""
        if self._severity(p, q, 0) < 1.0:
            return 0
        return self.worsens(p, q, limit)

    def safe_travel_m(
        self, ac: AircraftState, nominal: _Pred, other: _Pred, follow_cap: float | None = None
    ) -> float | None:
        """Largest route distance ``ac`` may still fly (then wait) without a
        conflict with ``other`` (binary search over the predicted distances);
        0 if only hard braking (with ``altitude_hold`` if needed) avoids it; None
        if even that conflicts."""
        # limits the give-way curve can still honour (cap >= current speed)
        braking = ac.velocity_mps**2 / (2.0 * GIVE_WAY_DECEL_MPS2)
        limits = [s for s in sorted(set(nominal.s)) if s >= braking - 1e-6]
        lo, hi, best = 0, len(limits) - 1, None
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.worsens(self.predict(ac, "nominal", limits[mid], follow_cap), other) is None:
                best = limits[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        if best is not None:
            return best
        if self.worsens(self.predict(ac, "brake"), other) is None:
            return 0.0  # brake hard
        if ac.level_swap_with is None and self.worsens(self.predict(ac, "hold"), other) is None:
            ac.altitude_hold = True  # brake hard and stop the climb/descent into it
            return 0.0
        return None

    # -- who gives way
    @staticmethod
    def _on_route_ahead(a: AircraftState, b: AircraftState, max_m: float) -> bool:
        """True if ``b`` is ahead of ``a`` on ``a``'s own route, within ``max_m``."""
        bl = b.leg
        if bl is None:
            return False
        offset = -a.distance_along_edge_m
        for i in range(a.leg_index, len(a.legs)):
            if offset > max_m:
                return False
            leg = a.legs[i]
            if leg.edge.edge_id == bl.edge.edge_id and leg.start_node == bl.start_node:
                gap = offset + b.distance_along_edge_m
                return 0.0 < gap <= max_m
            offset += leg.length_m
        return False

    def _gives_way(self, a: AircraftState, b: AircraftState, pa: _Pred, pb: _Pred, k: int) -> bool:
        """True if ``a`` gives way to ``b``."""
        if pa.stationary != pb.stationary:
            return pb.stationary
        reach = ROUTE_ORDER_M
        if self._on_route_ahead(a, b, reach):
            return True
        if self._on_route_ahead(b, a, reach):
            return False
        if self._changing_level(pa) != self._changing_level(pb):
            # the level one gives way: slowing a climb or descent (tied to the
            # distance flown) would only slow the vertical separation
            return not self._changing_level(pa)
        ax, ay, bx, by = pa.xs[0], pa.ys[0], pb.xs[0], pb.ys[0]
        dx, dy = bx - ax, by - ay
        d = math.hypot(dx, dy) or 1.0
        ha, hb = math.radians(a.heading_deg), math.radians(b.heading_deg)
        ca = (math.sin(ha) * dx + math.cos(ha) * dy) / d  # a flying toward b
        cb = -(math.sin(hb) * dx + math.cos(hb) * dy) / d  # b flying toward a
        if abs(ca - cb) > 0.5:
            return ca > cb
        if a.vehicle_type != b.vehicle_type:
            return a.vehicle_type == "passenger"
        mx, my = 0.5 * (pa.xs[k] + pb.xs[k]), 0.5 * (pa.ys[k] + pb.ys[k])
        da = math.hypot(mx - ax, my - ay)
        db = math.hypot(mx - bx, my - by)
        if abs(da - db) > 1.0:
            return da > db
        return a.aircraft_id > b.aircraft_id

    @staticmethod
    def _changing_level(p: _Pred) -> bool:
        return abs(p.alts[-1] - p.alts[0]) > 1.0

    def resolve(
        self,
        world: WorldState,
        a: AircraftState,
        b: AircraftState,
        pa: _Pred,
        pb: _Pred,
        k: int,
        follow: dict[str, float | None],
    ) -> None:
        """Cap the speed of whoever gives way. If the preferred one cannot stop
        short of the conflict (e.g. a descent on the glide path only progresses
        by flying forward), the other one gives way instead."""
        first_in = self._landing_first(a, b)
        if first_in is not None:
            y, py, po = (b, pb, pa) if first_in is a else (a, pa, pb)
            if y.status in MOVING:
                limit = self.safe_travel_m(y, py, po, follow[y.aircraft_id])
                _cap(y, 0.0 if limit is None else math.sqrt(2.0 * GIVE_WAY_DECEL_MPS2 * limit))
            return
        first = (a, b, pa, pb) if self._gives_way(a, b, pa, pb, k) else (b, a, pb, pa)
        if self._level_off(world, first[0], first[1], first[2], first[3], follow) or self._level_off(
            world, first[1], first[0], first[3], first[2], follow
        ):
            return
        if k == 0:
            if self.worsens(pa, pb) is None:
                return  # inside, but flying on as planned opens the gap
            # Already too close: neither may close in further.
            capped = False
            level_change = self._changing_level(pa) != self._changing_level(pb)
            for y, o, py, po in ((a, b, pa, pb), (b, a, pb, pa)):
                if y.status not in MOVING or (level_change and self._changing_level(py)):
                    continue
                if self._on_route_ahead(o, y, ROUTE_ORDER_M):
                    continue  # the leader: braking would only let the follower close in
                limit = self.safe_travel_m(y, py, po, follow[y.aircraft_id])
                cap = 0.0 if limit is None else math.sqrt(2.0 * GIVE_WAY_DECEL_MPS2 * limit)
                _cap(y, min(cap, max(0.0, y.velocity_mps - MAX_DECEL_MPS2)) if y is first[0] else cap)
                capped = capped or limit is not None
            if capped:
                return
        else:
            for y, _o, py, po in (first, (first[1], first[0], first[3], first[2])):
                if y.status not in MOVING:
                    continue
                limit = self.safe_travel_m(y, py, po, follow[y.aircraft_id])
                if limit is not None:
                    _cap(y, math.sqrt(2.0 * GIVE_WAY_DECEL_MPS2 * limit))
                    return
        for lead, behind in ((a, b), (b, a)):
            if behind.status in MOVING and self._on_route_ahead(behind, lead, ROUTE_ORDER_M):
                _cap(behind, 0.0)  # same route: the follower stops, the leader flies on
                return
        # Neither can stop short: they would meet nose to nose at the same level.
        # Both stop; if no climb or descent in place separates them, one swaps
        # to the other level of the band (level_swap_with) while hovering.
        y, o, _, _ = first
        _cap(y, 0.0)
        _cap(o, 0.0)
        if self._changing_level(self.predict(y, "brake")) or self._changing_level(self.predict(o, "brake")):
            return
        for ac, other in ((y, o), (o, y)):
            if ac.level_swap_with is None and ac.status in MOVING:
                ac.level_swap_with = other.aircraft_id
                if self._changing_level(self.predict(ac, "brake")) and self._level_clear(ac, world, exclude=other):
                    return
                ac.level_swap_with = None  # on the glide path, or the other level is busy

    def _level_off(
        self, world: WorldState, y: AircraftState, o: AircraftState, py: _Pred, po: _Pred, follow: dict
    ) -> bool:
        """Pass over (or under) a stationary ``o`` by stopping ``y``'s climb or
        descent while it flies on at speed (``altitude_hold``), if that avoids
        ``o`` and everybody else."""
        if not po.stationary or y.status not in MOVING or y.level_swap_with is not None:
            return False  # (only over a holding or landing aircraft: moving traffic is speed controlled)
        if not self._changing_level(py):
            return False
        level = self.predict(y, "level", follow_cap=follow[y.aircraft_id])
        if self.first_conflict(level, po, HYSTERESIS) is not None or not self._level_clear(y, world, o, level):
            return False
        y.altitude_hold = True
        return True

    def _level_clear(
        self, ac: AircraftState, world: WorldState, exclude: AircraftState | None = None, mine: _Pred | None = None
    ) -> bool:
        """True if ``ac``'s climb or descent to its current target level (and
        stopping there), or the given prediction ``mine``, conflicts with nobody
        else."""
        mine = mine or self.predict(ac, "brake")
        x, y = mine.xs[0], mine.ys[0]
        for oid in sorted(world.aircraft):
            other = world.aircraft[oid]
            if other is ac or other is exclude or not other.airborne or other.leg is None:
                continue
            ox, oy = self.geo.xy(other.position.lat, other.position.lon)
            if math.hypot(ox - x, oy - y) > other.leg.planned_speed_mps * PROBE_HORIZON_S + 2 * self.r_h:
                continue
            if self.first_conflict(mine, self.predict(other, follow_cap=self._follow.get(oid))) is not None:
                return False
        return True

    def end_level_swaps(self, world: WorldState) -> None:
        """Return to the own level once that no longer conflicts with the
        aircraft the swap avoided."""
        for aid in sorted(world.aircraft):
            ac = world.aircraft[aid]
            other = world.aircraft.get(ac.level_swap_with) if ac.level_swap_with else None
            if ac.level_swap_with is None:
                continue
            if other is None or not other.airborne or other.leg is None or ac.leg is None:
                ac.level_swap_with = None
                continue
            ac.level_swap_with = None
            back = self.first_conflict(self.predict(ac), self.predict(other))
            if back is not None or not self._level_clear(ac, world):
                ac.level_swap_with = other.aircraft_id

    def _landing_first(self, a: AircraftState, b: AircraftState) -> AircraftState | None:
        """Of two arrivals to the same site inside ``terminal_radius_m`` with at
        least one cleared to land: the one that lands first (cleared, then closer
        to the pad). It never gives way, so a landing is never blocked."""
        fa, fb = a.active_flight, b.active_flight
        if fa is None or fb is None or fa.destination_node != fb.destination_node:
            return None
        if not (a.pad_cleared or b.pad_cleared):
            return None
        ra, rb = a.remaining_route_m(), b.remaining_route_m()
        r = self.cfg.terminal_radius_m
        if ra >= r or rb >= r:
            return None
        if a.pad_cleared != b.pad_cleared:
            return a if a.pad_cleared else b
        return a if (ra, a.aircraft_id) < (rb, b.aircraft_id) else b

    def caps(self, world: WorldState) -> None:
        self._wind = world.wind
        air = [world.aircraft[a] for a in sorted(world.aircraft)]
        air = [ac for ac in air if ac.airborne and ac.leg is not None]
        # following caps, before any probe cap
        follow = self._follow = {ac.aircraft_id: ac.velocity_cap_mps for ac in air}
        self.end_level_swaps(world)
        xy = {ac.aircraft_id: self.geo.xy(ac.position.lat, ac.position.lon) for ac in air}
        reach = {ac.aircraft_id: max(ac.velocity_mps, ac.leg.planned_speed_mps if ac.leg else 0.0) for ac in air}
        preds: dict[str, _Pred] = {}
        active: set[tuple[str, str]] = set()

        def nominal(ac: AircraftState) -> _Pred:
            p = preds.get(ac.aircraft_id)
            if p is None:
                p = preds[ac.aircraft_id] = self.predict(ac, follow_cap=follow[ac.aircraft_id])
            return p

        for i, a in enumerate(air):
            ax, ay = xy[a.aircraft_id]
            for b in air[i + 1 :]:
                if a.status not in MOVING and b.status not in MOVING:
                    continue
                bx, by = xy[b.aircraft_id]
                if (
                    math.hypot(ax - bx, ay - by)
                    > (reach[a.aircraft_id] + reach[b.aircraft_id]) * PROBE_HORIZON_S + self.r_h
                ):
                    continue
                pa, pb = nominal(a), nominal(b)
                pair = (a.aircraft_id, b.aircraft_id)
                # hysteresis: a pair resolved last step stays resolved until the
                # released predictions clear the volume by a margin (no flicker)
                k = self.first_conflict(pa, pb, HYSTERESIS if pair in self._active else 1.0)
                if k is None:
                    continue
                active.add(pair)
                self.resolve(world, a, b, pa, pb, k, follow)
        self._active = active
