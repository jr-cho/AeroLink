"""Energy model helpers (PLAN E3.3 T2).

The model is phase-based: hover/landing consumes the most energy per second,
climb is intermediate, and cruise is the most efficient.  Wind is modelled as
an extra cruise energy cost proportional to headwind component.

When the energy toggle is off, the simulator falls back to the E3.2 linear
range drain in ``physics.advance``; functions in this module are only called
with the toggle on.
"""

from __future__ import annotations

from aerolink.network import Network
from aerolink.sim import physics
from aerolink.sim.state import AircraftState, WindField
from aerolink.sim.world_config import EnergyConfig
from aerolink.vehicle import as_vehicle_type, vehicle_cfg


def trip_energy_estimate(network: Network, path: list[str], vehicle_type: str, energy_cfg: EnergyConfig) -> float:
    """Conservative planning estimate of SOC needed to fly ``path``.

    Includes the actual cruise distance plus fixed takeoff/climb and
    landing/hover transition allowances.  This is intentionally conservative
    (it ignores the energy recovered in descent) so that the departure and
    reserve checks err on the safe side.
    """
    vehicle = vehicle_cfg(as_vehicle_type(vehicle_type))
    length = network.route_length_m(path)
    return length * vehicle.cruise_soc_per_m + vehicle.transition_soc


def _headwind_multiplier(ac: AircraftState, energy_cfg: EnergyConfig, wind: WindField) -> float:
    """Extra cruise/descent energy factor from headwind along the current leg.

    A headwind increases aerodynamic work per ground metre.  Tailwind gives no
    benefit in this simplified model (factor = 1.0).
    """
    leg = ac.leg
    if leg is None:
        return 1.0
    course = physics.bearing_deg(
        leg.edge.oriented_geometry(leg.start_node)[0],
        leg.edge.oriented_geometry(leg.start_node)[-1],
    )
    component = wind.along_track_component(course)
    if component >= 0.0:
        return 1.0
    speed = max(leg.planned_speed_mps, 1.0)
    return 1.0 + energy_cfg.headwind_penalty_factor * (-component) / speed


def step_energy_drain(
    ac: AircraftState,
    travel_m: float,
    dt_s: float,
    energy_cfg: EnergyConfig,
    wind: WindField,
) -> float:
    """SOC drain for one simulation step, based on the aircraft's phase."""
    vehicle = vehicle_cfg(ac.vehicle_type)
    status = ac.status

    # Hover/landing: stationary or final descent; highest per-second drain.
    if status in ("holding", "landing"):
        return vehicle.hover_soc_per_s * dt_s

    # climb / cruise / descending / fallback
    cruise_drain = travel_m * vehicle.cruise_soc_per_m * _headwind_multiplier(ac, energy_cfg, wind)
    if status == "climbing":
        # Climbing: per-second climb drain on top of horizontal drain.
        return cruise_drain + vehicle.climb_soc_per_s * dt_s
    return cruise_drain


def nearest_alternate_energy(network: Network, ac: AircraftState, energy_cfg: EnergyConfig) -> float:
    """Minimum SOC needed to reach the nearest alternate site from the current
    position along ``ac.leg``.

    The current position is treated as distance ``ac.distance_along_edge_m``
    from ``leg.start_node`` and ``leg.length_m - ac.distance_along_edge_m`` from
    ``leg.end_node``.  Only sites that serve the aircraft's vehicle type are
    considered.
    """
    leg = ac.leg
    if leg is None:
        return float("inf")

    vehicle = vehicle_cfg(ac.vehicle_type)
    sites = network.nodes_for(ac.vehicle_type)
    if not sites:
        return float("inf")

    dist_back = ac.distance_along_edge_m
    dist_forward = leg.length_m - ac.distance_along_edge_m
    best = float("inf")

    for site in sites:
        d_start = network.route_distance_m(leg.start_node, site, ac.vehicle_type)
        d_end = network.route_distance_m(leg.end_node, site, ac.vehicle_type)
        if d_start is None and d_end is None:
            continue
        ground = float("inf")
        if d_start is not None:
            ground = min(ground, dist_back + d_start)
        if d_end is not None:
            ground = min(ground, dist_forward + d_end)
        # Conservative: add full transition allowance for landing (and implicit
        # climb out if the alternate is not the destination).
        energy = ground * vehicle.cruise_soc_per_m + vehicle.transition_soc
        if energy < best:
            best = energy

    return best
