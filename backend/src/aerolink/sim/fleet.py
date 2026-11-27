"""Fleet creation and home assignment (PLAN E3.2)."""

from __future__ import annotations

import numpy as np

from aerolink.contracts import VehicleType
from aerolink.network import Network
from aerolink.sim.pads import has_parking, park
from aerolink.sim.physics import PAD_ALT_M
from aerolink.sim.reach import reachable_on_full_charge
from aerolink.sim.state import AircraftState, PadState, SimPosition
from aerolink.sim.world_config import EnergyConfig
from aerolink.vehicle import DEFAULT_MEDICAL_COUNT, DEFAULT_PASSENGER_COUNT


def pad_position(network: Network, node_id: str) -> SimPosition:
    node = network.nodes[node_id]
    return SimPosition(lat=float(node["lat"]), lon=float(node["lon"]), alt_msl_m=PAD_ALT_M)


def home_sites(network: Network, vehicle_type: VehicleType) -> list[str]:
    """Sites an aircraft of this type may be based at: sites that serve it."""
    return sorted(network.nodes_for(vehicle_type))


def create_fleet(
    network: Network,
    pads: dict[str, PadState],
    rng: np.random.Generator,
    passenger: int = DEFAULT_PASSENGER_COUNT,
    medical: int = DEFAULT_MEDICAL_COUNT,
    staged: dict[str, int] | None = None,
) -> dict[str, AircraftState]:
    """Create the fleet parked at home sites, on stands first, then pads, never
    exceeding either.

    Homes are spread round-robin over a seeded permutation of valid sites. Each
    ``staged`` ({site: count}) puts that many passenger aircraft at a site first (an operator
    positioning aircraft for an event); the rest are spread as usual. Aircraft are
    parked in the same order, so an empty ``staged`` changes nothing. Each
    aircraft gets two independent RNG streams (behaviour, telemetry noise)
    spawned from ``rng`` so changing noise settings never changes trajectories.
    """
    if passenger < 0 or medical < 0:
        raise ValueError("fleet counts must be non-negative")
    counts: list[tuple[VehicleType, int, str]] = [("passenger", passenger, "p"), ("medical", medical, "m")]
    root = np.random.SeedSequence(int(rng.integers(0, 2**63 - 1)))
    streams = root.spawn(2 * (passenger + medical))
    fleet: dict[str, AircraftState] = {}
    n = 0
    for vtype, count, prefix in counts:
        if count == 0:
            continue
        sites = home_sites(network, vtype)
        if not sites:
            raise ValueError(f"network has no sites serving {vtype}")
        order = [sites[i] for i in rng.permutation(len(sites))]
        free = sum(pads[s].free_slots() + pads[s].stands - len(pads[s].stand_occupants) for s in order)
        if free < count:
            raise ValueError(f"not enough free pad slots or stands for {count} {vtype} aircraft")
        i = 0
        placed = (
            [site for site in sorted(staged or {}) for _ in range((staged or {})[site])] if vtype == "passenger" else []
        )
        if len(placed) > count:
            raise ValueError(f"{len(placed)} staged aircraft but a passenger fleet of {count}")
        for k in range(count):
            if k < len(placed):
                home = placed[k]
                if home not in pads or not has_parking(pads[home]):
                    raise ValueError(f"cannot stage an aircraft at {home}: unknown site or no free pad or stand")
            else:
                while not has_parking(pads[order[i % len(order)]]):
                    i += 1
                home = order[i % len(order)]
                i += 1
            aid = f"sim-{prefix}{k:02d}"
            on_stand = park(pads[home], aid)
            fleet[aid] = AircraftState(
                aircraft_id=aid,
                vehicle_type=vtype,
                home_node=home,
                status="parked",
                current_node=home,
                position=pad_position(network, home),
                on_stand=on_stand,
                rng=np.random.default_rng(streams[2 * n]),
                telemetry_rng=np.random.default_rng(streams[2 * n + 1]),
            )
            n += 1
    return dict(sorted(fleet.items()))


def choose_destination(
    network: Network,
    ac: AircraftState,
    rng: np.random.Generator,
    return_home_probability: float = 0.0,
    energy_cfg: EnergyConfig | None = None,
) -> str | None:
    """A reachable destination other than the current node, or None if there is none.

    Reachable means some route fits a full battery (``reach.reachable_on_full_charge``): an
    operator never books a trip its aircraft can't fly. Whether the *current* charge suffices
    is the pilot's departure check.

    Away from home, the aircraft returns home with ``return_home_probability``
    (if home is reachable; otherwise it picks another destination).
    """
    here = ac.current_node

    def reachable(n: str) -> bool:
        return here is None or reachable_on_full_charge(network, ac, here, n, energy_cfg)

    if here is not None and here != ac.home_node and rng.random() < return_home_probability:
        if reachable(ac.home_node):
            return ac.home_node
    candidates = [n for n in sorted(network.nodes_for(ac.vehicle_type)) if n != here and reachable(n)]
    if not candidates:
        return None
    return candidates[int(rng.integers(len(candidates)))]
