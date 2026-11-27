"""Simulator state model (PLAN E3.2).

Plain mutable dataclasses. The controller mutates them in place, one aircraft
at a time in sorted id order, which keeps runs deterministic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

import numpy as np

from aerolink.contracts import VehicleType
from aerolink.network import Edge

Status = Literal[
    "parked",  # on a pad, ready; occupies a pad slot
    "climbing",  # airborne, gaining altitude
    "cruise",  # airborne, level
    "descending",  # airborne, losing altitude
    "holding",  # airborne, stopped at the hold point waiting for a pad
    "landing",  # final vertical descent onto a reserved pad
    "turnaround",  # on a pad, boarding / patient transfer; occupies a pad slot
]

AIRBORNE: frozenset[str] = frozenset({"climbing", "cruise", "descending", "holding", "landing"})
ON_PAD: frozenset[str] = frozenset({"parked", "turnaround"})


@dataclass
class SimPosition:
    lat: float
    lon: float
    alt_msl_m: float


@dataclass(frozen=True)
class FlightLeg:
    """One edge of a route, oriented from ``start_node`` to ``end_node``.

    Frozen: a leg is never edited in place; replanning builds new legs.
    """

    edge: Edge
    start_node: str
    end_node: str
    cruise_alt_m: float
    planned_speed_mps: float

    @property
    def length_m(self) -> float:
        return self.edge.length_m


FlightPurpose = Literal["passenger", "medical_dispatch", "medical_return", "reposition"]


@dataclass
class FlightPlan:
    flight_id: UUID
    request_id: UUID
    route_id: UUID
    path: list[str]
    destination_node: str
    departure_time: float
    eta_by_node: dict[str, float]
    purpose: FlightPurpose = "passenger"
    revision: int = 0
    accepted_suggestion: bool = False
    diverted: bool = False
    destination_changed: bool = False
    # Index into ``path`` of the node at which a pilot destination change fires.
    destination_change_at: int | None = None
    dispatch_t: float | None = None
    flown: list[str] = field(default_factory=list)  # nodes already passed, origin first


@dataclass
class AircraftState:
    aircraft_id: str
    vehicle_type: VehicleType
    home_node: str
    status: Status
    current_node: str | None  # pad node while on a pad, else None
    position: SimPosition
    rng: np.random.Generator  # behaviour stream (speeds, pilot decisions, demand)
    telemetry_rng: np.random.Generator  # telemetry noise stream, isolated from behaviour
    active_flight: FlightPlan | None = None
    legs: list[FlightLeg] = field(default_factory=list)
    leg_index: int = 0
    distance_along_edge_m: float = 0.0
    velocity_mps: float = 0.0  # along-track ground speed
    vertical_speed_mps: float = 0.0
    heading_deg: float = 0.0
    battery_soc: float = 1.0
    pad_requested: bool = False
    pad_cleared: bool = False
    booked_slot: tuple[str, float, float] | None = None  # (pad_id, t_start, t_end)
    ground_hold_until_s: float | None = None  # pad_booking: wait for slot to open
    hold_remaining_m: float = 0.0  # remaining-route distance of the hold point
    hold_start_s: float | None = None
    landing_until_s: float | None = None
    turnaround_until_s: float | None = None
    next_trip_s: float | None = None
    velocity_cap_mps: float | None = None  # per-step separation/merge speed cap, reset each step
    stack_hold: bool = False  # separation: waits for a pad above the approach path (STACK_ALT_OFFSET_M)
    altitude_hold: bool = False  # per-step: separation stops this aircraft's climb/descent, reset each step
    level_swap_with: str | None = None  # separation level swap: fly the other level of the band to avoid this aircraft
    retry_at_s: float | None = None  # back-off after a failed return-home or diversion attempt
    on_stand: bool = False  # parked/turnaround on a stand rather than holding a pad slot
    pad_clear_at_s: float | None = None  # earliest time to move from the pad to a stand
    departure_waiting: bool = False  # on a stand, waiting for a free pad to take off from
    scheduled_until_s: float | None = None  # reserved for a scheduled leg until this time
    predicted_total_s: float | None = None  # B1 advisory prediction at departure
    pending_destination: str | None = None  # trip waiting for the battery to charge
    low_energy_emergency: bool = False  # reached its reserve while holding: first in line for a pad slot

    @property
    def airborne(self) -> bool:
        return self.status in AIRBORNE

    @property
    def leg(self) -> FlightLeg | None:
        if 0 <= self.leg_index < len(self.legs):
            return self.legs[self.leg_index]
        return None

    def remaining_route_m(self) -> float:
        """Distance left to the end of the final leg."""
        if self.leg is None:
            return 0.0
        rest = sum(leg.length_m for leg in self.legs[self.leg_index + 1 :])
        return max(0.0, self.legs[self.leg_index].length_m - self.distance_along_edge_m) + rest


@dataclass
class PadState:
    """A landing site. ``occupants`` holds every aircraft using a pad slot:
    cleared to land, landing, clearing off, or on the ground with no stand.
    ``stand_occupants`` are on the ground on a stand (hardening plan §7a)."""

    node_id: str
    capacity: int
    occupants: list[str] = field(default_factory=list)
    queue: list[str] = field(default_factory=list)  # holding aircraft, first come first served
    stands: int = 0
    stand_occupants: list[str] = field(default_factory=list)
    bookings: list[tuple[float, float, str]] = field(default_factory=list)  # [(t_start, t_end, aircraft_id)]

    def free_slots(self) -> int:
        return self.capacity - len(self.occupants)

    def has_free_slot(self) -> bool:
        return self.free_slots() > 0

    def has_free_stand(self) -> bool:
        return len(self.stand_occupants) < self.stands

    def free_for_departure(self) -> bool:
        """A pad slot is free that no queued arrival is waiting for."""
        return self.free_slots() > len(self.queue)


@dataclass
class WindField:
    """Spatially constant wind with per-step gusts.

    ``direction_deg`` is where the wind blows *from* (meteorological convention).
    ``gust`` is the current fractional gust multiplier offset, resampled each step.
    """

    speed_mps: float = 5.0
    direction_deg: float = 270.0
    gust_fraction: float = 0.2
    gust: float = 0.0

    def current_speed_mps(self) -> float:
        return max(0.0, self.speed_mps * (1.0 + self.gust))

    def along_track_component(self, course_deg: float) -> float:
        """Signed along-track wind (positive = tailwind) for a true course."""
        toward = (self.direction_deg + 180.0) % 360.0
        return self.current_speed_mps() * math.cos(math.radians(toward - course_deg))

    def resample_gust(self, rng: np.random.Generator) -> None:
        if self.gust_fraction <= 0:
            self.gust = 0.0
            return
        self.gust = float(np.clip(rng.normal(0.0, self.gust_fraction / 2.0), -self.gust_fraction, self.gust_fraction))


@dataclass
class WorldState:
    t: float
    aircraft: dict[str, AircraftState]
    pads: dict[str, PadState]
    rng: np.random.Generator
    wind: WindField = field(default_factory=WindField)
    planned: list[FlightPlan] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
