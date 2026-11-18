"""Pydantic models for the AeroLink message contracts.

These models mirror the JSON Schemas in contracts/schemas/. The tests in
tests/test_models.py enforce that the two agree on every example and on
edge-case mutations of every field (formats, types, ranges, conditionals).
"""

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

# --- Format checks (mirror the JSON Schema format checkers) --------------------
# Pydantic's own parsers are more lenient than the schema's `format` checks.
# The validators below narrow them so both sides accept exactly the same input.

# jsonschema's "uuid" format: hyphenated 8-4-4-4-12 hex only. uuid.UUID alone
# would also accept unhyphenated, "urn:uuid:" and "{...}" forms.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# jsonschema's "date-time" format (RFC 3339, same regex as rfc3339-validator,
# applied case-insensitively). AwareDatetime alone would also accept unix
# timestamps ("1763391600"), a space separator, "+0000" and missing seconds.
_RFC3339_RE = re.compile(
    r"^\d{4}-(0[1-9]|1[0-2])-\d{2}T([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d+)?"
    r"(Z|[+-]([01]\d|2[0-3]):[0-5]\d)$",
    re.ASCII | re.IGNORECASE,
)


# The before-validators do the type check themselves (str in, or an already
# parsed value), so the inner parse can run non-strict: a BeforeValidator hands
# it a Python str, which strict mode would reject for UUID/datetime. Anything
# lax mode would over-accept (ints, bytes) never reaches it.


def _check_uuid(v: Any) -> Any:
    if isinstance(v, uuid.UUID):
        return v
    if not isinstance(v, str) or not _UUID_RE.fullmatch(v):
        raise ValueError("must be a hyphenated UUID string (8-4-4-4-12 hex)")
    return v


def _check_rfc3339(v: Any) -> Any:
    if isinstance(v, datetime):
        return v
    if not isinstance(v, str) or not _RFC3339_RE.fullmatch(v):
        raise ValueError("must be an RFC 3339 date-time string with offset")
    return v


def _integral_float_to_int(v: Any) -> Any:
    # JSON Schema "integer" accepts any number with a zero fractional part
    # (1.0, -0.0, 1e308); strict Pydantic would reject them as floats.
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


Uuid = Annotated[uuid.UUID, Field(strict=False), BeforeValidator(_check_uuid)]
Timestamp = Annotated[
    AwareDatetime, Field(strict=False), BeforeValidator(_check_rfc3339)
]
JsonInt = Annotated[int, BeforeValidator(_integral_float_to_int)]

# --- Shared types (mirror common.schema.json) ---------------------------------

SchemaVersion = Literal["3.0"]
AircraftId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,31}$")]
NodeId = Annotated[str, Field(pattern=r"^[a-z]{2}-[a-z0-9-]+$")]
Mission = Literal["passenger", "cargo", "medical"]
Lat = Annotated[float, Field(ge=-90, le=90)]
Lon = Annotated[float, Field(ge=-180, le=180)]
AltMslM = Annotated[float, Field(ge=-100, le=10000)]

NonEmptyStr = Annotated[str, Field(min_length=1)]
NonNegative = Annotated[float, Field(ge=0)]
Probability = Annotated[float, Field(ge=0, le=1)]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


# --- Telemetry ----------------------------------------------------------------


class Position(_Base):
    lat: Lat
    lon: Lon
    alt_msl_m: AltMslM


class Motion(_Base):
    ground_speed_mps: Annotated[float, Field(ge=0, le=150)]
    course_deg: Annotated[float, Field(ge=0, lt=360)]
    vertical_speed_mps: Annotated[float, Field(ge=-50, le=50)]


class Fix(_Base):
    type: Literal["none", "2d", "3d"]
    hdop: NonNegative
    satellites: Annotated[JsonInt, Field(ge=0, le=64)]
    replayed: bool


class Telemetry(_Base):
    schema_version: SchemaVersion
    message_id: Uuid
    aircraft_id: AircraftId
    mission: Mission
    timestamp: Timestamp
    position: Position
    motion: Motion
    fix: Fix
    active_intent_id: Uuid | None


# --- Route request ------------------------------------------------------------


class RouteRequest(_Base):
    schema_version: SchemaVersion
    message_id: Uuid
    request_id: Uuid
    aircraft_id: AircraftId
    mission: Mission
    timestamp: Timestamp
    destination_node: NodeId


# --- Route response -----------------------------------------------------------


class CostTerms(_Base):
    est_time_s: NonNegative
    expected_hold_s: NonNegative
    mu: NonNegative
    conflict_probability: Probability


class IntentWindow(_Base):
    """Per-node timing; time_start/time_end are the calibrated p10/p90 bounds."""

    node: NodeId
    time_start: Timestamp
    eta_p50: Timestamp
    time_end: Timestamp


class Route(_Base):
    route_id: Uuid
    rank: Annotated[JsonInt, Field(ge=1, le=3)]
    distance_m: NonNegative
    est_time_s: NonNegative
    expected_hold_s: NonNegative
    conflict_probability: Probability
    cost_terms: CostTerms
    reason: NonEmptyStr
    intent: Annotated[list[IntentWindow], Field(min_length=1)]
    waypoints: Annotated[list[tuple[Lat, Lon, AltMslM]], Field(min_length=2)]


class RouteResponse(_Base):
    schema_version: SchemaVersion
    message_id: Uuid
    request_id: Uuid
    aircraft_id: AircraftId
    timestamp: Timestamp
    revision: Annotated[JsonInt, Field(ge=0)]
    status: Literal["ok", "no_route", "rejected", "error"]
    error: NonEmptyStr | None
    method: Literal["classical", "aerolink"]
    forecaster: NonEmptyStr
    priority: Annotated[JsonInt, Field(ge=0, le=100)]
    forecast_time: Timestamp
    expires_at: Timestamp
    graph_version: NonEmptyStr
    constraints_cycle: NonEmptyStr
    briefing: str | None
    routes: Annotated[list[Route], Field(max_length=3)]

    @model_validator(mode="after")
    def _check_status(self) -> "RouteResponse":
        if self.status == "ok":
            if self.error is not None:
                raise ValueError("error must be null when status is ok")
            if len(self.routes) < 1:
                raise ValueError("status ok requires at least one route")
        else:
            if self.error is None:
                raise ValueError("error is required when status is not ok")
            if self.routes:
                raise ValueError("routes must be empty when status is not ok")
        return self


# --- Intent -------------------------------------------------------------------


class Intent(_Base):
    schema_version: SchemaVersion
    message_id: Uuid
    request_id: Uuid
    aircraft_id: AircraftId
    timestamp: Timestamp
    route_id: Uuid


class IntentStatus(_Base):
    schema_version: SchemaVersion
    message_id: Uuid
    aircraft_id: AircraftId
    timestamp: Timestamp
    intent_id: Uuid
    request_id: Uuid
    status: Literal[
        "accepted",
        "activated",
        "nonconforming",
        "contingent",
        "ended",
        "rejected",
        "superseded",
    ]
    reason: NonEmptyStr | None

    @model_validator(mode="after")
    def _check_reason(self) -> "IntentStatus":
        if (
            self.status in ("rejected", "superseded", "nonconforming", "contingent")
            and self.reason is None
        ):
            raise ValueError(f"reason is required when status is {self.status}")
        return self
