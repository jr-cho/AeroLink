"""Contract-shaped (v4.0 ``Telemetry``) records with seeded noise and dropout."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import numpy as np

from aerolink.contracts import SCHEMA_VERSION
from aerolink.sim.state import AircraftState

M_PER_DEG_LAT = 111_320.0


@dataclass(frozen=True)
class TelemetryNoise:
    position_m: float = 5.0  # 1-sigma per horizontal axis
    speed_mps: float = 0.5  # 1-sigma
    dropout_rate: float = 0.0  # probability a record is not delivered

    def __post_init__(self) -> None:
        if self.position_m < 0 or self.speed_mps < 0:
            raise ValueError("noise sigmas must be non-negative")
        if not 0.0 <= self.dropout_rate <= 1.0:
            raise ValueError("dropout_rate must be in [0, 1]")


def seeded_uuid(rng: np.random.Generator) -> UUID:
    """A version-4 UUID drawn from ``rng`` (uuid4() would break reproducibility)."""
    return UUID(bytes=rng.bytes(16), version=4)


def should_emit(ac: AircraftState) -> bool:
    """Telemetry is emitted for airborne aircraft only (not parked or turnaround)."""
    return ac.airborne


def telemetry_record(ac: AircraftState, timestamp: datetime, noise: TelemetryNoise | None = None) -> dict | None:
    """One JSON-ready Telemetry message, or None if not airborne or dropped out.

    All randomness comes from ``ac.telemetry_rng``; a fixed number of draws is
    made per call so dropout does not shift later noise samples.
    """
    if not should_emit(ac):
        return None
    noise = noise or TelemetryNoise()
    rng = ac.telemetry_rng
    drop_u = rng.random()
    dn, de, dv = rng.normal(0.0, 1.0, size=3)
    message_id = seeded_uuid(rng)
    if drop_u < noise.dropout_rate:
        return None

    lat = ac.position.lat + dn * noise.position_m / M_PER_DEG_LAT
    lon = ac.position.lon + de * noise.position_m / (M_PER_DEG_LAT * math.cos(math.radians(ac.position.lat)))
    speed = min(150.0, max(0.0, ac.velocity_mps + dv * noise.speed_mps))
    course = ac.heading_deg % 360.0
    if course >= 360.0:  # guard float edge case (-1e-17 % 360 == 360.0)
        course = 0.0
    return {
        "schema_version": SCHEMA_VERSION,
        "message_id": str(message_id),
        "aircraft_id": ac.aircraft_id,
        "vehicle_type": ac.vehicle_type,
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "position": {"lat": lat, "lon": lon, "alt_msl_m": ac.position.alt_msl_m},
        "motion": {
            "ground_speed_mps": speed,
            "course_deg": course,
            "vertical_speed_mps": max(-50.0, min(50.0, ac.vertical_speed_mps)),
        },
        "fix": {"type": "3d", "hdop": 0.9, "satellites": 11, "replayed": False},
        "active_flight_id": str(ac.active_flight.flight_id) if ac.active_flight else None,
    }
