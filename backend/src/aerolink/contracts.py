"""Message models for AeroLink contracts v4.0 (ARCHITECTURE §12).

The JSON Schemas in contracts/schemas/ are the source of truth. These Pydantic
models mirror them for use in Python; tests/test_models.py proves the two agree
on every example and on generated mutations.

Parse incoming JSON with `Model.model_validate_json(raw)`.
"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "4.0"

# --- shared types (common.schema.json) ----------------------------------------
SchemaVersion = Literal["4.0"]
AircraftId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,31}$")]
Callsign = Annotated[str, Field(pattern=r"^[A-Z0-9-]{2,10}$")]
NodeId = Annotated[str, Field(pattern=r"^[a-z]{2}-[a-z0-9-]+$")]
EdgeId = Annotated[str, Field(pattern=r"^[a-z]{2}-[a-z0-9-]+->[a-z]{2}-[a-z0-9-]+$")]
VehicleType = Literal["passenger", "medical"]
TrafficLevel = Literal["low", "moderate", "heavy"]
Lat = Annotated[float, Field(ge=-90, le=90)]
Lon = Annotated[float, Field(ge=-180, le=180)]
AltMslM = Annotated[float, Field(ge=-100, le=10000)]
CourseDeg = Annotated[float, Field(ge=0, lt=360)]
NonNeg = Annotated[float, Field(ge=0)]
Probability = Annotated[float, Field(ge=0, le=1)]
Timestamp = AwareDatetime


class Strict(BaseModel):
    """Unknown fields are errors, types are not coerced (matches additionalProperties: false)."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Message(Strict):
    schema_version: SchemaVersion
    message_id: UUID


# --- telemetry -----------------------------------------------------------------
class Position(Strict):
    lat: Lat
    lon: Lon
    alt_msl_m: AltMslM


class Motion(Strict):
    ground_speed_mps: Annotated[float, Field(ge=0, le=150)]
    course_deg: CourseDeg
    vertical_speed_mps: Annotated[float, Field(ge=-50, le=50)]


class Fix(Strict):
    type: Literal["none", "2d", "3d"]
    hdop: NonNeg
    satellites: Annotated[int, Field(ge=0, le=64)]
    replayed: bool


class Telemetry(Message):
    # v4.1 (additive, G2): a unit may report its callsign; 4.0 telemetry (no callsign) stays valid
    schema_version: Literal["4.0", "4.1"]  # ty: ignore[incompatible-attribute-override]  # deliberate widening (additive 4.1)
    callsign: Callsign | None = None
    aircraft_id: AircraftId
    vehicle_type: VehicleType
    timestamp: Timestamp
    position: Position
    motion: Motion
    fix: Fix
    active_flight_id: UUID | None


# --- route request / response ---------------------------------------------------
class RouteRequest(Message):
    request_id: UUID
    aircraft_id: AircraftId
    vehicle_type: VehicleType
    timestamp: Timestamp
    destination_node: NodeId


class CostTerms(Strict):
    est_time_s: NonNeg
    expected_wait_s: NonNeg
    mu: NonNeg
    bunching_risk: Probability


class Eta(Strict):
    node: NodeId
    eta_p10: Timestamp
    eta_p50: Timestamp
    eta_p90: Timestamp


Waypoint = tuple[Lat, Lon, AltMslM]


class Segment(Strict):
    """One leg of a route: the AI-predicted traffic level when the aircraft reaches its far end (U-API §1)."""

    model_config = ConfigDict(serialize_by_alias=True)
    from_: NodeId = Field(alias="from")
    to: NodeId
    eta_s: NonNeg  # epoch seconds (p50) of reaching ``to``
    level: TrafficLevel


class Route(Strict):
    route_id: UUID
    rank: Annotated[int, Field(ge=1, le=3)]
    label: Literal["fastest", "alternative"]
    distance_m: NonNeg
    est_time_s: NonNeg
    expected_wait_s: NonNeg
    bunching_risk: Probability
    traffic: TrafficLevel
    cost_terms: CostTerms
    reason: Annotated[str, Field(max_length=200)]
    eta: Annotated[list[Eta], Field(min_length=1)]
    waypoints: Annotated[list[Waypoint], Field(min_length=2)]
    segments: list[Segment] = []  # optional on the wire: older units ignore it; the backend always sends it


class RouteResponse(Message):
    request_id: UUID
    aircraft_id: AircraftId
    revision: Annotated[int, Field(ge=0)]
    status: Literal["ok", "no_route", "error"]
    method: Literal["classical", "aerolink"]
    forecast_time: Timestamp
    expires_at: Timestamp
    forecaster: Annotated[str, Field(min_length=1)]
    graph_version: Annotated[str, Field(min_length=1)]
    constraints_cycle: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    summary: Annotated[str, Field(max_length=400)] | None
    reason: Annotated[str, Field(max_length=200)] | None
    routes: Annotated[list[Route], Field(max_length=3)]

    @model_validator(mode="after")
    def _status_matches_routes(self) -> Self:
        if self.status == "ok" and not self.routes:
            raise ValueError("status ok needs at least one route")
        if self.status != "ok" and (self.routes or self.reason is None):
            raise ValueError("no_route/error carries a reason and no routes")
        return self


# --- route selection / flight status --------------------------------------------
class RouteSelection(Message):
    request_id: UUID
    aircraft_id: AircraftId
    route_id: UUID
    timestamp: Timestamp


class FlightStatus(Message):
    aircraft_id: AircraftId
    flight_id: UUID
    status: Literal["planned", "active", "off_route", "replaced", "ended"]
    reason: Annotated[str, Field(max_length=200)] | None
    timestamp: Timestamp
    detected_by: Literal["ai", "rule"] | None = None  # optional (contract §7): off_route only; who flagged it


# --- traffic ---------------------------------------------------------------------
class NearbyAircraft(Strict):
    id: AircraftId
    vehicle_type: VehicleType
    lat: Lat
    lon: Lon
    alt_msl_m: AltMslM
    course_deg: CourseDeg
    off_route: bool


class EdgeTraffic(Strict):
    edge: EdgeId
    now: TrafficLevel
    in_15_min: TrafficLevel
    in_30_min: TrafficLevel


class Traffic(Message):
    aircraft_id: AircraftId
    forecast_time: Timestamp
    aircraft: Annotated[list[NearbyAircraft], Field(max_length=50)]
    edges: Annotated[list[EdgeTraffic], Field(max_length=100)]


# --- health heartbeat (contract v4.1, additive: every v4.0 message is unchanged) --------------
class HealthGnss(Strict):
    fix: Literal["none", "2d", "3d"]
    satellites: Annotated[int, Field(ge=0, le=64)]
    hdop: NonNeg


class Health(Strict):
    """``aerolink/aircraft/{id}/health`` (and ``aerolink/sim/{id}/health``), every 10 s."""

    schema_version: Literal["4.1"]
    message_id: UUID
    aircraft_id: AircraftId
    timestamp: Timestamp
    uptime_s: NonNeg
    reset_reason: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,32}$")]
    wifi_rssi_dbm: Annotated[int, Field(ge=-127, le=0)]
    gnss: HealthGnss
    free_heap_bytes: Annotated[int, Field(ge=0)]
    firmware_version: Annotated[str, Field(min_length=1, max_length=32)]
    supply_mv: Annotated[int, Field(ge=0, le=60000)] | None = None


# Schema name (contracts/schemas/<name>.schema.json) -> model
MODELS: dict[str, type[Strict]] = {
    "telemetry": Telemetry,
    "route-request": RouteRequest,
    "route-response": RouteResponse,
    "route-selection": RouteSelection,
    "flight-status": FlightStatus,
    "traffic": Traffic,
    "health": Health,
}
