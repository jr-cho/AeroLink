"""Range/energy rules every plan must satisfy (PLAN E3.3 T2).

With the energy toggle off the old ``battery_soc * range_m`` rule is used.
With the toggle on, plans are checked against the phase-aware energy estimate
including the configured reserve and the takeoff/landing transition allowance.
A medical aircraft must also be able to get home from wherever it lands.
"""

from __future__ import annotations

from aerolink.network import Network
from aerolink.sim.state import AircraftState
from aerolink.sim.world_config import EnergyConfig
from aerolink.vehicle import vehicle_cfg

FIT_EPS_M = 1e-6
ROUTE_RETRY_S = 300.0  # back-off before retrying a failed return-home or diversion


def range_left_m(ac: AircraftState) -> float:
    return ac.battery_soc * vehicle_cfg(ac.vehicle_type).range_m


def fits(ac: AircraftState, route_m: float, energy_cfg: EnergyConfig | None = None) -> bool:
    """Return True if ``route_m`` fits in the aircraft's remaining range/energy."""
    if energy_cfg is None:
        return route_m <= range_left_m(ac) + FIT_EPS_M
    # With the energy model, route length by itself is not enough; callers that
    # have the actual path should use ``trip_energy_estimate`` directly.  This
    # length-only form applies the same per-metre cruise rate and transition
    # allowance, so it stays conservative.
    vehicle = vehicle_cfg(ac.vehicle_type)
    return route_m * vehicle.cruise_soc_per_m + vehicle.transition_soc <= ac.battery_soc + FIT_EPS_M


def can_get_home(
    network: Network,
    ac: AircraftState,
    site: str,
    energy_cfg: EnergyConfig | None = None,
) -> bool:
    """A medical aircraft landing at ``site`` can fly home on a full battery.
    Passenger aircraft have no home constraint."""
    if ac.vehicle_type != "medical":
        return True
    if energy_cfg is None:
        return network.route_distance_m(site, ac.home_node, "medical") is not None
    from aerolink.sim.energy import trip_energy_estimate

    paths = network.shortest_paths(site, ac.home_node, vehicle_type="medical", k=1)
    if not paths:
        return False
    return trip_energy_estimate(network, paths[0], ac.vehicle_type, energy_cfg) <= 1.0 + FIT_EPS_M


def can_serve_incident(
    network: Network,
    ac: AircraftState,
    site: str,
    energy_cfg: EnergyConfig | None = None,
) -> bool:
    """A parked medical aircraft can fly to ``site`` now and get home afterwards."""
    if ac.current_node is None:
        return False
    if energy_cfg is None:
        if network.route_distance_m(ac.current_node, site, "medical") is None:
            return False
        return can_get_home(network, ac, site, energy_cfg)
    from aerolink.sim.energy import trip_energy_estimate

    paths = network.shortest_paths(ac.current_node, site, vehicle_type="medical", k=1)
    if not paths:
        return False
    if not can_get_home(network, ac, site, energy_cfg):
        return False
    return trip_energy_estimate(network, paths[0], ac.vehicle_type, energy_cfg) <= ac.battery_soc + FIT_EPS_M


def in_service_area(network: Network, aircraft: dict[str, AircraftState], site: str) -> bool:
    """Some medical aircraft can make the round trip home -> ``site`` -> home."""
    return any(
        ac.vehicle_type == "medical"
        and network.route_distance_m(ac.home_node, site, "medical") is not None
        and network.route_distance_m(site, ac.home_node, "medical") is not None
        for ac in (aircraft[k] for k in sorted(aircraft))
    )
