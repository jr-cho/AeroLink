"""Tests for vehicle type config (PLAN E3.1)."""

import pytest

from aerolink.vehicle import (
    DEFAULT_MEDICAL_COUNT,
    DEFAULT_PASSENGER_COUNT,
    VEHICLES,
    medical_first_order,
    vehicle_cfg,
)


def test_vehicle_parameters():
    passenger = vehicle_cfg("passenger")
    medical = vehicle_cfg("medical")
    assert passenger.cruise_speed_mps == 60.0
    assert medical.cruise_speed_mps == 65.0
    assert passenger.range_m == 160_000.0
    assert medical.range_m == 160_000.0
    assert medical.calculation_priority < passenger.calculation_priority


def test_medical_first():
    assert medical_first_order("medical") < medical_first_order("passenger")


def test_unknown_vehicle_type():
    # Runtime guard: `vehicle_cfg` raises ValueError for a vehicle_type that is
    # not present in VEHICLES.  We test this with a cast to satisfy `ty`.
    from typing import cast

    from aerolink.contracts import VehicleType

    bad = cast(VehicleType, "cargo")
    with pytest.raises(ValueError):
        vehicle_cfg(bad)


def test_default_fleet_counts():
    assert DEFAULT_PASSENGER_COUNT == 7
    assert DEFAULT_MEDICAL_COUNT == 3


def test_vehicles_immutable_defaults():
    assert VEHICLES["passenger"].vehicle_type == "passenger"
    assert VEHICLES["medical"].vehicle_type == "medical"
