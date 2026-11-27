"""Event-kind registry (PLAN E3.3 T0).

Central list so tests can assert that every logged event is a known kind and
so new kinds are added deliberately.
"""

from __future__ import annotations

ARRIVAL = "arrival"
DEPARTURE = "departure"
DESTINATION_CHANGE = "destination_change"
DESTINATION_CHANGE_REJECTED = "destination_change_rejected"
DIVERSION = "diversion"
DIVERSION_UNAVAILABLE = "diversion_unavailable"
DISPATCH_UNSERVICEABLE = "dispatch_unserviceable"
STAND_IN = "stand_in"
DISPATCH_ON_SITE = "dispatch_on_site"
DISPATCH_EXPIRED = "dispatch_expired"
DEPARTURE_WAIT = "departure_wait"
DEPARTURE_RELEASED = "departure_released"
GROUND_HOLD = "ground_hold"
GROUND_HOLD_END = "ground_hold_end"
HOLD_END = "hold_end"
HOLD_START = "hold_start"
LANDING = "landing"
MEDICAL_INCIDENT = "medical_incident"
PAD_CLEARED = "pad_cleared"
PAD_REQUESTED = "pad_requested"
READY = "ready"
ROUTE_UNAVAILABLE = "route_unavailable"
LOW_ENERGY_EMERGENCY = "low_energy_emergency"  # a holding aircraft reached its energy reserve
CHARGE_WAIT = "charge_wait"  # a trip is due but the battery must charge first
SLOT_PREBOOKED = "slot_prebooked"
SLOT_TAKEN = "slot_taken"
SUGGESTION = "suggestion"
TRIP_REJECTED = "trip_rejected"
BUNCHING = "bunching"
LOSS_OF_SEPARATION = "loss_of_separation"
LOSS_OF_SEPARATION_END = "loss_of_separation_end"

SCHEDULED_DEPARTURE = "scheduled_departure"
SCHEDULE_CONFLICT = "schedule_conflict"

PAD_WAIT = "pad_wait"
OFF_ROUTE = "off_route"

ALL: frozenset[str] = frozenset(
    [
        DISPATCH_EXPIRED,
        ARRIVAL,
        DEPARTURE,
        DESTINATION_CHANGE,
        DESTINATION_CHANGE_REJECTED,
        DIVERSION,
        DIVERSION_UNAVAILABLE,
        DISPATCH_UNSERVICEABLE,
        STAND_IN,
        DISPATCH_ON_SITE,
        DEPARTURE_WAIT,
        DEPARTURE_RELEASED,
        GROUND_HOLD,
        GROUND_HOLD_END,
        HOLD_END,
        HOLD_START,
        LANDING,
        MEDICAL_INCIDENT,
        PAD_CLEARED,
        PAD_REQUESTED,
        PAD_WAIT,
        READY,
        ROUTE_UNAVAILABLE,
        CHARGE_WAIT,
        LOW_ENERGY_EMERGENCY,
        SCHEDULED_DEPARTURE,
        SCHEDULE_CONFLICT,
        SLOT_PREBOOKED,
        SLOT_TAKEN,
        SUGGESTION,
        TRIP_REJECTED,
        BUNCHING,
        LOSS_OF_SEPARATION,
        LOSS_OF_SEPARATION_END,
        OFF_ROUTE,
    ]
)


def valid(kind: str) -> bool:
    return kind in ALL
