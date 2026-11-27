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


def path_fits(
    network: Network,
    ac: AircraftState,
    path: list[str],
    energy_cfg: EnergyConfig | None = None,
    soc: float | None = None,
) -> bool:
    """The one planning rule for "can ``ac`` fly ``path`` with ``soc`` (default: its current
    charge)". With the energy model the configured reserve must be left on landing; without it,
    the route must fit the range left. Dispatch, destination choice, and the pilot all use this,
    so a trip one of them accepts is never refused by another."""
    soc = ac.battery_soc if soc is None else soc
    if energy_cfg is None:
        return network.route_length_m(path) <= soc * vehicle_cfg(ac.vehicle_type).range_m + FIT_EPS_M
    from aerolink.sim.energy import trip_energy_estimate

    trip = trip_energy_estimate(network, path, ac.vehicle_type, energy_cfg)
    return trip <= soc - energy_cfg.reserve_soc + 1e-9


def reachable_on_full_charge(
    network: Network, ac: AircraftState, origin: str, destination: str, energy_cfg: EnergyConfig | None = None
) -> bool:
    """Some route ``origin`` -> ``destination`` fits a full battery (the aircraft charges at the pad)."""
    paths = network.shortest_paths(origin, destination, vehicle_type=ac.vehicle_type, k=1)
    return bool(paths) and path_fits(network, ac, paths[0], energy_cfg, soc=1.0)


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
    return reachable_on_full_charge(network, ac, site, ac.home_node, energy_cfg)


def can_serve_incident(
    network: Network,
    ac: AircraftState,
    site: str,
    energy_cfg: EnergyConfig | None = None,
) -> bool:
    """A parked medical aircraft can fly to ``site`` now and get home afterwards."""
    if ac.current_node is None:
        return False
    paths = network.shortest_paths(ac.current_node, site, vehicle_type="medical", k=1)
    if not paths or not path_fits(network, ac, paths[0], energy_cfg):
        return False
    return can_get_home(network, ac, site, energy_cfg)


def in_service_area(network: Network, aircraft: dict[str, AircraftState], site: str) -> bool:
    """Some medical aircraft can make the round trip home -> ``site`` -> home."""
    return any(
        ac.vehicle_type == "medical"
        and network.route_distance_m(ac.home_node, site, "medical") is not None
        and network.route_distance_m(site, ac.home_node, "medical") is not None
        for ac in (aircraft[k] for k in sorted(aircraft))
    )
