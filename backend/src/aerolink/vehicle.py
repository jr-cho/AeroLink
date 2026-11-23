"""Vehicle type parameters and constraints (PLAN E3.1, ARCHITECTURE §4)."""

from __future__ import annotations

from dataclasses import dataclass

from aerolink.contracts import VehicleType


@dataclass(frozen=True)
class VehicleCfg:
    """Static parameters for a vehicle type."""

    vehicle_type: VehicleType
    cruise_speed_mps: float
    range_m: float
    min_cruise_speed_mps: float  # lower bound for arrival metering
    max_cruise_speed_mps: float  # upper bound for arrival metering
    hover_soc_per_s: float  # most energy-intensive phase
    climb_soc_per_s: float  # intermediate energy rate
    calculation_priority: int  # lower numbers are calculated first (medical = 0)

    @property
    def transition_soc(self) -> float:
        """Energy used for a takeoff/climb and a landing/hover sequence.

        This is a conservative planning allowance; the real model updates SOC
        each simulation step based on actual phase.
        """
        return self.climb_soc_per_s * 60.0 + self.hover_soc_per_s * 40.0

    @property
    def cruise_soc_per_m(self) -> float:
        """Cruise drain calibrated so a max-range trip uses exactly 1.0 SOC
        including the takeoff and landing transitions."""
        return (1.0 - self.transition_soc) / self.range_m


# Fleet parameters from ARCHITECTURE §4.  Range includes reserve and is a planning
# parameter, not a physical limit; routes longer than range_m are not offered.
VEHICLES: dict[VehicleType, VehicleCfg] = {
    "passenger": VehicleCfg(
        vehicle_type="passenger",
        cruise_speed_mps=60.0,
        range_m=160_000.0,
        min_cruise_speed_mps=45.0,
        max_cruise_speed_mps=75.0,
        hover_soc_per_s=0.0008,
        climb_soc_per_s=0.0005,
        calculation_priority=1,
    ),
    "medical": VehicleCfg(
        vehicle_type="medical",
        cruise_speed_mps=65.0,
        range_m=160_000.0,
        min_cruise_speed_mps=50.0,
        max_cruise_speed_mps=80.0,
        hover_soc_per_s=0.0008,
        climb_soc_per_s=0.0005,
        calculation_priority=0,  # medical first
    ),
}


DEFAULT_PASSENGER_COUNT = 7
DEFAULT_MEDICAL_COUNT = 3


def vehicle_cfg(vehicle_type: VehicleType) -> VehicleCfg:
    """Return the static configuration for a vehicle type."""
    if vehicle_type not in VEHICLES:
        raise ValueError(f"unknown vehicle_type: {vehicle_type!r}")
    return VEHICLES[vehicle_type]


def medical_first_order(vtype: VehicleType) -> int:
    """Sorting key that puts medical first across all other types."""
    return vehicle_cfg(vtype).calculation_priority
