"""Point-mass kinematics along corridor polylines (PLAN E3.2, ARCHITECTURE §9.1).

Aircraft move along oriented ``FlightLeg``s. Each step:

* ground-speed target = planned airspeed + along-track wind, capped by a braking
  profile so the aircraft can always stop at its hold point or pad;
* speed changes are limited by ``MAX_ACCEL_MPS2`` / ``MAX_DECEL_MPS2``;
* altitude follows ``min(cruise band, pad + remaining * GLIDE_SLOPE)`` with
  climb/descent rate limits, so aircraft climb out after departure and descend
  on a glide path into the destination;
* travel carries across leg boundaries within one step.

Accepted simplifications (hardening plan D1/D2): wind acts only along track, so
a crosswind changes neither ground speed nor heading; the accel, decel and
climb limits are one set shared by both vehicle types (only cruise speed and
range differ per type).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from aerolink.network import Edge, Network
from aerolink.sim.state import AircraftState, FlightLeg, SimPosition, WindField
from aerolink.sim.world_config import EnergyConfig
from aerolink.vehicle import vehicle_cfg

EARTH_RADIUS_M = 6_371_008.8
FT_TO_M = 0.3048

MAX_ACCEL_MPS2 = 2.0
MAX_DECEL_MPS2 = 3.0
APPROACH_DECEL_MPS2 = 1.0  # comfortable braking used to plan stops; < MAX_DECEL
MAX_CLIMB_MPS = 5.0
MAX_DESCENT_MPS = 5.0
MAX_GROUND_SPEED_MPS = 150.0  # contract upper bound on ground_speed_mps
GLIDE_SLOPE = 0.08  # metres of altitude per metre of remaining route on approach
CLIMB_SPEED_FACTOR = 0.7  # horizontal speed fraction while far below cruise altitude
CLIMB_SPEED_ALT_BAND_M = 30.0
PAD_ALT_M = 30.0  # nominal pad elevation MSL for Central Florida


def edge_cruise_alt_m(edge: Edge) -> float:
    """Middle of the edge's altitude band, in metres MSL."""
    return (edge.band_floor_ft + edge.band_ceiling_ft) / 2.0 * FT_TO_M


def directional_alt_m(edge: Edge, start_node: str) -> float:
    """Semicircular rule (E3.3 T1): the cruise level for flying ``edge`` from
    ``start_node``. The chord course start -> end of 0-179.99 deg flies the band
    floor, 180-359.99 deg the band ceiling, so the two directions of an edge are
    always one band height (500 ft = 152.4 m) apart. The chord, not the local
    heading, is used so the level never changes in the middle of an edge."""
    coords = edge.oriented_geometry(start_node)
    course = bearing_deg(coords[0], coords[-1]) if len(coords) > 1 else 0.0
    ft = edge.band_floor_ft if course < 180.0 else edge.band_ceiling_ft
    return ft * FT_TO_M


def flight_level_m(ac: AircraftState, leg: FlightLeg) -> float:
    """The level ``ac`` flies on ``leg``: its cruise level, or during a
    separation level swap (``ac.level_swap_with``) the other side of the band."""
    if ac.level_swap_with is None:
        return leg.cruise_alt_m
    return (leg.edge.band_floor_ft + leg.edge.band_ceiling_ft) * FT_TO_M - leg.cruise_alt_m


STACK_ALT_OFFSET_M = 180.0  # separation: queued arrivals wait this far above the glide path


def approach_alt_m(ac: AircraftState, leg: FlightLeg, remaining_m: float) -> float:
    """Altitude target at ``remaining_m`` to go: the flight level, capped by the
    glide path (raised by ``STACK_ALT_OFFSET_M`` while queued for a pad with
    separation on, so cleared arrivals pass beneath the holding stack)."""
    offset = STACK_ALT_OFFSET_M if ac.stack_hold and not ac.pad_cleared else 0.0
    return min(flight_level_m(ac, leg), PAD_ALT_M + remaining_m * GLIDE_SLOPE + offset)


def segment_length_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Haversine distance between two (lon, lat) points."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Initial true course from a to b (lon, lat), in [0, 360)."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.degrees(math.atan2(x, y)) % 360.0


_SEGMENTS: dict[tuple[int, str], tuple[Edge, tuple[tuple[float, float], ...], list[float], float]] = {}


def _oriented_segments(edge: Edge, from_node: str) -> tuple[tuple[tuple[float, float], ...], list[float], float]:
    """Oriented polyline, its segment lengths and their sum, memoised per edge
    object (the entry holds the edge, so an ``id`` is never reused while cached)."""
    key = (id(edge), from_node)
    hit = _SEGMENTS.get(key)
    if hit is not None and hit[0] is edge:
        return hit[1], hit[2], hit[3]
    coords = edge.oriented_geometry(from_node)
    seg = [segment_length_m(coords[i], coords[i + 1]) for i in range(len(coords) - 1)]
    total = sum(seg)
    _SEGMENTS[key] = (edge, coords, seg, total)
    return coords, seg, total


def interpolate_on_edge(edge: Edge, distance_m: float, from_node: str) -> tuple[float, float, float]:
    """(lat, lon, course_deg) at ``distance_m`` along ``edge`` travelling from ``from_node``.

    ``distance_m`` is measured against ``edge.length_m`` (the planning length) and
    mapped proportionally onto the polyline; it is clamped to [0, length].
    """
    coords, seg, total = _oriented_segments(edge, from_node)
    if len(coords) == 1:
        return coords[0][1], coords[0][0], 0.0
    if total <= 0:
        return coords[0][1], coords[0][0], 0.0
    frac = 0.0 if edge.length_m <= 0 else min(1.0, max(0.0, distance_m / edge.length_m))
    target = frac * total
    acc = 0.0
    for i, s in enumerate(seg):
        if s <= 0:
            continue
        if acc + s >= target or i == len(seg) - 1:
            local = min(1.0, max(0.0, (target - acc) / s))
            (lon1, lat1), (lon2, lat2) = coords[i], coords[i + 1]
            return lat1 + local * (lat2 - lat1), lon1 + local * (lon2 - lon1), bearing_deg(coords[i], coords[i + 1])
        acc += s
    lon, lat = coords[-1]
    return lat, lon, bearing_deg(coords[-2], coords[-1])


def apply_acceleration(current: float, target: float, dt_s: float) -> float:
    """Move ``current`` toward ``target`` within the acceleration/deceleration limits."""
    if target > current:
        return min(current + MAX_ACCEL_MPS2 * dt_s, target)
    return max(current - MAX_DECEL_MPS2 * dt_s, target)


def update_altitude(current_alt: float, target_alt: float, dt_s: float) -> float:
    """Move altitude toward target within the climb/descent rate limits."""
    diff = target_alt - current_alt
    step = (MAX_CLIMB_MPS if diff > 0 else MAX_DESCENT_MPS) * dt_s
    if abs(diff) <= step:
        return target_alt
    return current_alt + math.copysign(step, diff)


def braking_distance_m(speed_mps: float, decel_mps2: float = APPROACH_DECEL_MPS2) -> float:
    return speed_mps * speed_mps / (2.0 * decel_mps2)


def glide_target_alt_m(cruise_alt_m: float, remaining_m: float, pad_alt_m: float = PAD_ALT_M) -> float:
    return min(cruise_alt_m, pad_alt_m + remaining_m * GLIDE_SLOPE)


def build_legs(
    path: list[str],
    network: Network,
    planned_speed_mps: float | list[float],
    directional: bool = False,
) -> list[FlightLeg]:
    """Oriented legs for a node path. ``planned_speed_mps`` may be per leg.
    ``directional`` flies the semicircular-rule level (``directional_alt_m``)
    instead of the middle of the band."""
    n = len(path) - 1
    speeds = planned_speed_mps if isinstance(planned_speed_mps, list) else [planned_speed_mps] * n
    if len(speeds) != n:
        raise ValueError(f"need {n} leg speeds, got {len(speeds)}")
    legs = []
    for i in range(n):
        edge = network.edge_between(path[i], path[i + 1])
        legs.append(
            FlightLeg(
                edge=edge,
                start_node=path[i],
                end_node=path[i + 1],
                cruise_alt_m=directional_alt_m(edge, path[i]) if directional else edge_cruise_alt_m(edge),
                planned_speed_mps=speeds[i],
            )
        )
    return legs


@dataclass(frozen=True)
class StepResult:
    nodes_passed: list[str]  # intermediate nodes crossed this step, in order
    at_stop: bool  # aircraft has reached (and stopped at) its stop point
    distance_m: float  # ground distance travelled this step


NodeCallback = Callable[[str], list[FlightLeg] | None]


def advance(
    ac: AircraftState,
    wind: WindField,
    dt_s: float,
    stop_remaining_m: float,
    on_node: NodeCallback | None = None,
    energy: EnergyConfig | None = None,
) -> StepResult:
    """Advance an airborne aircraft one step, mutating ``ac`` in place.

    ``stop_remaining_m`` is the remaining-route distance at which the aircraft
    must come to rest (0 = the pad itself, >0 = a hold point short of it).
    ``on_node`` is called for every intermediate node crossed; if it returns new
    legs (a reroute starting at that node), travel continues on them.
    """
    leg = ac.leg
    if leg is None:
        return StepResult([], True, 0.0)

    remaining = ac.remaining_route_m()
    to_stop = max(0.0, remaining - stop_remaining_m)

    # Course of the segment we are on drives the wind correction.
    _, _, course = interpolate_on_edge(leg.edge, ac.distance_along_edge_m, leg.start_node)
    airspeed = leg.planned_speed_mps
    if leg.cruise_alt_m - ac.position.alt_msl_m > CLIMB_SPEED_ALT_BAND_M and ac.status == "climbing":
        airspeed *= CLIMB_SPEED_FACTOR
    ground_target = max(0.0, airspeed + wind.along_track_component(course))
    # Apply any pilot/safety speed cap set before advance (e.g. separation).
    if ac.velocity_cap_mps is not None and ac.velocity_cap_mps < ground_target:
        ground_target = ac.velocity_cap_mps
    v_brake = math.sqrt(2.0 * APPROACH_DECEL_MPS2 * to_stop)
    target = min(ground_target, v_brake, MAX_GROUND_SPEED_MPS)

    v0 = ac.velocity_mps
    v1 = apply_acceleration(v0, target, dt_s)
    travel = 0.5 * (v0 + v1) * dt_s
    at_stop = False
    if travel >= to_stop:
        travel = to_stop
        at_stop = True
        # Only snap to rest if that is physically possible within the decel limit.
        v1 = 0.0 if v0 <= MAX_DECEL_MPS2 * dt_s else v1
        if v1 > 0:
            at_stop = False

    # Move along legs, carrying leftover distance across boundaries.
    passed: list[str] = []
    dist = ac.distance_along_edge_m + travel
    while dist > ac.legs[ac.leg_index].length_m and ac.leg_index < len(ac.legs) - 1:
        dist -= ac.legs[ac.leg_index].length_m
        node = ac.legs[ac.leg_index].end_node
        passed.append(node)
        ac.leg_index += 1
        if on_node is not None:
            new_legs = on_node(node)
            if new_legs is not None:
                ac.legs = new_legs
                ac.leg_index = 0
    ac.distance_along_edge_m = min(dist, ac.legs[ac.leg_index].length_m)
    leg = ac.legs[ac.leg_index]

    # Vertical profile.
    remaining_after = ac.remaining_route_m()
    alt0 = ac.position.alt_msl_m
    alt_target = approach_alt_m(ac, leg, remaining_after)
    if ac.altitude_hold:
        alt_target = alt0  # stopped for traffic below/above: no climb or descent into it
    alt1 = update_altitude(alt0, alt_target, dt_s)
    ac.vertical_speed_mps = (alt1 - alt0) / dt_s

    lat, lon, course = interpolate_on_edge(leg.edge, ac.distance_along_edge_m, leg.start_node)
    ac.position = SimPosition(lat=lat, lon=lon, alt_msl_m=alt1)
    ac.heading_deg = course
    ac.velocity_mps = v1
    if energy is None:
        ac.battery_soc = max(0.0, ac.battery_soc - travel / vehicle_cfg(ac.vehicle_type).range_m)
    else:
        from aerolink.sim.energy import step_energy_drain

        ac.battery_soc = max(0.0, ac.battery_soc - step_energy_drain(ac, travel, dt_s, energy, wind))

    if ac.vertical_speed_mps > 0.05:
        ac.status = "climbing"
    elif ac.vertical_speed_mps < -0.05:
        ac.status = "descending"
    elif ac.altitude_hold and alt1 <= PAD_ALT_M + 1e-6 and ac.status == "climbing":
        ac.status = "climbing"  # a takeoff whose climb separation is holding is still taking off, not cruising
    else:
        ac.status = "cruise"
    return StepResult(passed, at_stop, travel)
