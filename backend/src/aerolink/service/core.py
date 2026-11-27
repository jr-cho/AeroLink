"""Message handling and live loops, independent of the MQTT client library.

``Service.handle(topic, payload)`` returns the messages to publish, so the whole device
flow is testable without a broker. ``mqtt.py`` is the thin layer that feeds it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

from aerolink.contracts import (
    SCHEMA_VERSION,
    EdgeTraffic,
    FlightStatus,
    Health,
    NearbyAircraft,
    RouteRequest,
    RouteResponse,
    RouteSelection,
    Telemetry,
    Traffic,
)
from aerolink.service import reroute
from aerolink.service.ai_api import DISCLAIMER_VERSION, record_ack
from aerolink.service.clearance import Legs
from aerolink.service.events import EventLog
from aerolink.service.health import UnitMonitor
from aerolink.service.live import LiveState, Track, distance_m
from aerolink.service.planner import Planner, flight_plan
from aerolink.service.store import Store
from aerolink.service.topics import Topic, parse, reply
from aerolink.traffic import TrafficForecast

log = logging.getLogger("aerolink.service")

Outbound = tuple[str, bytes]
FlightState = Literal["planned", "active", "off_route", "replaced", "ended"]
Detector = Literal["ai", "rule"]  # who flagged an off-route state (contract §7)
ACTIVE_SPEED_MPS = 5.0  # planned -> active once moving faster than this
ARRIVED_M = 300.0  # active -> ended within this distance of the destination ...
ARRIVED_SPEED_MPS = 3.0  # ... and slower than this
STALE_S = 30.0  # no traffic updates to aircraft silent for longer than this
ETA_CHANGE_S = 30.0  # a destination ETA that moves by more than this is recorded in prediction_history
HISTORY_KEEP = 20


class SimFlightPlan(BaseModel):
    """Simulator-only message ``aerolink/sim/{id}/flight-plan`` (not part of the device contract v4.0).

    The simulator normally reports its plan with route-request + route-selection. When none of the
    returned options is the path it actually flies, it sends this instead so the backend still
    records the flown path as a planned flight.
    """

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["4.0"]
    message_id: UUID
    aircraft_id: str
    vehicle_type: Literal["passenger", "medical"]
    request_id: UUID | None = None
    path: list[str]


def utcnow() -> datetime:
    return datetime.now(UTC)


def encode(msg: BaseModel) -> bytes:
    return msg.model_dump_json().encode()


class Service:
    def __init__(self, store: Store, planner: Planner, clock: Callable[[], datetime] = utcnow):
        self.store = store
        self.planner = planner
        self.clock = clock
        self.live = LiveState()
        self.flights: dict[str, dict[str, Any]] = {}  # aircraft_id -> its current planned flight row
        self.forecast: TrafficForecast = planner.traffic([])
        self.stats: Counter[str] = Counter()
        self.revision: dict[str, int] = {}  # request_id -> latest response revision sent
        self.activated: dict[str, float] = {}  # aircraft_id -> unix time its flight became active
        self.windows: dict[str, tuple[float, float] | None] = {}  # aircraft_id -> arrival window at departure
        self.publish: Callable[[list[Outbound]], Awaitable[None]] | None = None  # set by the app (MQTT link)
        self.unsent: list[Outbound] = []  # background messages when nothing is connected (tests)
        self._bg: set[asyncio.Task[None]] = set()
        self.scenario: str = planner.settings.scenario
        self.eta_state: dict[str, dict[str, Any]] = {}  # flight_id -> destination ETA window + prediction_history
        self._known_flights: set[str] = set()
        self._callsign_claims: set[tuple[str, str]] = set()  # (unit, callsign) already logged as a mismatch
        cfg = planner.settings
        self.units = UnitMonitor(cfg.health_stale_s, cfg.no_fix_degraded_s, cfg.rssi_degraded_dbm)
        self.events = EventLog(store, clock)  # the app gives it the hub so alerts reach /ws/ops
        self.forecast_at: datetime | None = None  # when the forecast was last rebuilt (watchdog)
        self.sim_heard_at: datetime | None = None  # last message from the simulator (watchdog)
        self.atc_legs: Legs = {}  # corridor legs' airspace flags (set by the app from the network file)
        self.off_route_by: dict[
            str, Detector
        ] = {}  # aircraft_id -> "ai" | "rule": who flagged its current off-route state

    async def start(self) -> None:
        """Reload planned flights so a restart loses nothing."""
        for row in await self.store.flights(active_only=True):
            self.flights[row["aircraft_id"]] = row
        await self.refresh_forecast()

    # --- dispatch ---------------------------------------------------------------------------
    async def handle(self, topic: str, payload: bytes) -> list[Outbound]:
        t = parse(topic)
        handler = {
            "telemetry": self.on_telemetry,
            "status": self.on_status,
            "route-request": self.on_route_request,
            "route-selection": self.on_route_selection,
            "flight-plan": self.on_flight_plan,
            "health": self.on_health,
            "ai-ack": self.on_ai_ack,
            "reroute-response": self.on_reroute_response,
        }.get(t.kind if t else "")
        if t is None or handler is None:
            self.stats["bad_topic"] += 1
            return []
        if t.source == "sim":
            self.sim_heard_at = self.clock()
        try:
            return await handler(t, payload)
        except ValidationError as e:
            self.stats[f"invalid_{t.kind}"] += 1
            log.info("invalid %s on %s: %s", t.kind, topic, e.errors()[:1])
            return []

    def _wrong_topic(self, t: Topic, aircraft_id: str) -> bool:
        if aircraft_id != t.aircraft_id:
            self.stats["topic_mismatch"] += 1
            log.warning("payload aircraft_id %s on topic of %s: rejected", aircraft_id, t.aircraft_id)
            return True
        return False

    async def _check_device(self, t: Topic, vehicle_type: str, callsign: str | None = None) -> str | None:
        """Physical devices must be registered and use their registered vehicle type.

        Demo devices may change type (the demo button); the change is applied and logged.
        A callsign a unit reports that differs from the registry's is ignored (the registry wins) and logged once.
        Returns None if allowed, else the reason. Simulator aircraft are not registered.
        """
        if t.source == "sim":
            return None
        dev = await self.store.get_device(t.aircraft_id)
        if dev is None:
            return f"{t.aircraft_id} is not a registered device"
        if callsign and dev.get("callsign") and callsign != dev["callsign"]:
            await self._callsign_mismatch(t.aircraft_id, dev["callsign"], callsign)
        if dev["vehicle_type"] != vehicle_type:
            if not dev["demo"]:
                return f"{t.aircraft_id} is registered as {dev['vehicle_type']}"
            log.warning("demo device %s switched %s -> %s", t.aircraft_id, dev["vehicle_type"], vehicle_type)
            await self.events.record(
                "vehicle_type_changed",
                aircraft_id=t.aircraft_id,
                message=f"{t.aircraft_id} switched {dev['vehicle_type']} -> {vehicle_type} (demo)",
                detail={"from": dev["vehicle_type"], "to": vehicle_type},
            )
            await self.store.update_device(t.aircraft_id, vehicle_type=vehicle_type)
        return None

    async def _callsign_mismatch(self, aircraft_id: str, registry: str, claimed: str) -> None:
        """Log (once per distinct claim, not at 1 Hz) that a unit reports a callsign other than its registered one."""
        if (aircraft_id, claimed) in self._callsign_claims:
            return
        self._callsign_claims.add((aircraft_id, claimed))
        log.warning("unit %s reports callsign %s; the registry says %s (registry wins)", aircraft_id, claimed, registry)
        await self.events.record(
            "callsign_mismatch",
            aircraft_id=aircraft_id,
            severity="warn",
            message=f"{aircraft_id} reports callsign {claimed}; registered as {registry} (registry wins)",
            detail={"registry": registry, "reported": claimed},
        )

    # --- telemetry -----------------------------------------------------------------------------
    async def on_telemetry(self, t: Topic, payload: bytes) -> list[Outbound]:
        msg = Telemetry.model_validate_json(payload)
        if self._wrong_topic(t, msg.aircraft_id):
            return []
        if reason := await self._check_device(t, msg.vehicle_type, msg.callsign):
            self.stats["telemetry_rejected"] += 1
            log.warning("telemetry rejected: %s", reason)
            return []
        await self._note_fix(t, msg)  # before accept: a sample dropped for a poor fix still shows the unit is alive
        if dropped := self.live.accept(t.source, msg):
            self.stats[f"telemetry_{dropped}"] += 1
            return []
        self.stats["telemetry_ok"] += 1
        p, m = msg.position, msg.motion
        # a parked simulated aircraft reports 1 Hz forever: keep it live, don't fill the telemetry table with it
        if not (t.source == "sim" and Track.looks_grounded(msg) and not self._flying(msg.aircraft_id)):
            await self.store.add_telemetry(
                [
                    dict(
                        aircraft_id=msg.aircraft_id,
                        source=t.source,
                        timestamp=msg.timestamp,
                        lat=p.lat,
                        lon=p.lon,
                        alt_msl_m=p.alt_msl_m,
                        ground_speed_mps=m.ground_speed_mps,
                        course_deg=m.course_deg,
                        segment_id=self.live.tracks[msg.aircraft_id].segment_id,
                    )
                ]
            )
        if t.source == "aircraft":
            await self.store.update_device(t.aircraft_id, last_seen=msg.timestamp, online=True)
        out = await self._advance_flight(t, msg)
        self._mark_ground(msg)
        return out

    def _mark_ground(self, msg: Telemetry) -> None:
        """Parked = stationary and level with no flight in the air (a hover on an active flight is airborne)."""
        self.live.tracks[msg.aircraft_id].on_ground = Track.looks_grounded(msg) and not self._flying(msg.aircraft_id)

    def _flying(self, aircraft_id: str) -> bool:
        row = self.flights.get(aircraft_id)
        return row is not None and row["status"] in ("active", "off_route")

    async def _note_fix(self, t: Topic, msg: Telemetry) -> None:
        _, change = self.units.note_telemetry(msg.aircraft_id, msg.fix.type, self.clock())
        if change and t.source == "aircraft":
            await self.events.record(
                f"gnss_fix_{change}",
                aircraft_id=msg.aircraft_id,
                severity="warn" if change == "lost" else "info",
                message=f"{msg.aircraft_id} GNSS fix {change}",
                detail={"fix": msg.fix.type, "satellites": msg.fix.satellites, "hdop": msg.fix.hdop},
            )

    async def _advance_flight(self, t: Topic, msg: Telemetry) -> list[Outbound]:
        row = self.flights.get(msg.aircraft_id)
        if row is None:
            return []
        speed = msg.motion.ground_speed_mps
        new: FlightState | None = None
        if row["status"] == "planned" and speed > ACTIVE_SPEED_MPS:
            new = "active"
            self.activated[msg.aircraft_id] = msg.timestamp.timestamp()
            self.windows.pop(msg.aircraft_id, None)
        elif row["status"] in ("active", "off_route"):
            dest = self.planner.network.nodes[row["destination"]]
            near = distance_m(msg.position.lat, msg.position.lon, dest["lat"], dest["lon"]) < ARRIVED_M
            if near and speed < ARRIVED_SPEED_MPS:
                new = "ended"
        if new is None:
            return []
        return [await self._set_status(t, row, new)]

    def _status_msg(
        self, row: dict[str, Any], status: FlightState, reason: str | None = None, detected_by: Detector | None = None
    ) -> FlightStatus:
        return FlightStatus(
            schema_version=SCHEMA_VERSION,
            message_id=uuid4(),
            aircraft_id=row["aircraft_id"],
            flight_id=UUID(row["flight_id"]),
            status=status,
            reason=reason,
            timestamp=self.clock(),
            detected_by=detected_by,
        )

    async def _set_status(
        self,
        t: Topic,
        row: dict[str, Any],
        status: FlightState,
        reason: str | None = None,
        detected_by: Detector | None = None,
    ) -> Outbound:
        msg = self._status_msg(row, status, reason, detected_by)
        await self.store.set_flight_status(row["flight_id"], status)
        row["status"] = status
        if status in ("ended", "replaced"):
            self.off_route_by.pop(row["aircraft_id"], None)
            self.flights.pop(row["aircraft_id"], None)
            self.activated.pop(row["aircraft_id"], None)
            self.windows.pop(row["aircraft_id"], None)
        return reply(t, "flight-status"), encode(msg)

    # --- health heartbeat (contract v4.1) ---------------------------------------------------------
    async def on_health(self, t: Topic, payload: bytes) -> list[Outbound]:
        msg = Health.model_validate_json(payload)
        if self._wrong_topic(t, msg.aircraft_id):
            return []
        if t.source == "aircraft" and await self.store.get_device(t.aircraft_id) is None:
            self.stats["health_unregistered"] += 1
            return []
        self.units.note_health(msg, self.clock())
        self.stats["health_ok"] += 1
        await self.check_unit(t.aircraft_id, push=False)
        self.push_health(t.aircraft_id)
        return []

    def push_health(self, aircraft_id: str, online: bool | None = None) -> None:
        """Tell the unit's paired screens its health (the WebSocket ``health`` event)."""
        if self.events.hub is None:
            return
        if online is None:
            online = self.events.hub.online.get(aircraft_id, True)
        self.events.hub.publish(aircraft_id, "health", self.units.view(aircraft_id, online, self.clock()))

    async def check_unit(self, aircraft_id: str, online: bool | None = None, push: bool = True) -> None:
        """Re-derive one unit's status; log degraded/healthy changes and tell its screens on any change."""
        u = self.units.units.get(aircraft_id)
        if u is None:
            return
        if online is None:
            dev = await self.store.get_device(aircraft_id)
            online = bool(dev and dev["online"])
        status, reasons = self.units.evaluate(aircraft_id, online, self.clock())
        before, u.reasons = u.status, reasons
        if status == before:
            return
        u.status = status
        if status == "degraded":
            await self.events.record(
                "unit_degraded",
                aircraft_id=aircraft_id,
                severity="warn",
                message=f"{aircraft_id} degraded: {', '.join(reasons)}",
                detail={"reasons": reasons},
            )
        elif status == "healthy" and before == "degraded":
            await self.events.record("unit_healthy", aircraft_id=aircraft_id, message=f"{aircraft_id} healthy again")
        if push and "offline" not in (before, status):  # going on/offline has its own events
            self.push_health(aircraft_id, online)

    async def check_units(self) -> None:
        """Run about once a second: degraded can start by time passing alone (no fix for 10 s, stale heartbeat)."""
        for dev in await self.store.list_devices():
            await self.check_unit(dev["id"], bool(dev["online"]))

    # --- status (Last Will) -----------------------------------------------------------------------
    async def on_status(self, t: Topic, payload: bytes) -> list[Outbound]:
        word = payload.strip().lower()
        online = word == b"online"
        if t.source == "sim" and word == b"landed":  # sim-only: the flight is over, the aircraft stays (parked)
            self.stats["status_landed"] += 1
            row = self.flights.get(t.aircraft_id)
            if row is None:
                return []
            out = await self._set_status(t, row, "ended", "landed")
            await self.refresh_forecast()
            return [out]
        if t.source == "sim" and not online:  # a simulated aircraft that landed or was switched off is gone
            self.stats["status_offline"] += 1
            return await self._sim_left(t.aircraft_id, "simulated aircraft left")
        if track := self.live.tracks.get(t.aircraft_id):
            track.online = online
        if online:
            self.live.note_reconnect(t.aircraft_id)  # the next sample starts a new track segment
        if t.source == "aircraft" and await self.store.get_device(t.aircraft_id):
            await self.store.update_device(t.aircraft_id, online=online, last_seen=self.clock())
        self.stats[f"status_{'online' if online else 'offline'}"] += 1
        return []

    # --- route request -> route response -------------------------------------------------------------
    async def on_route_request(self, t: Topic, payload: bytes) -> list[Outbound]:
        req = RouteRequest.model_validate_json(payload)
        if self._wrong_topic(t, req.aircraft_id):
            return []
        stored = await self.store.get_request(str(req.request_id))
        if stored is not None:  # idempotent: same answer again
            self.stats["route_request_duplicate"] += 1
            return [(reply(t, "route-response"), json.dumps(stored["response"]).encode())]

        now = self.clock()
        origin = None
        if reason := await self._check_device(t, req.vehicle_type):
            resp = self._refusal(req, now, reason)
        else:
            track = self.live.tracks.get(req.aircraft_id)
            if track and (now - track.timestamp) <= timedelta(seconds=STALE_S):
                origin = self.planner.snap(track.last.position.lat, track.last.position.lon, req.vehicle_type)
            planned = [flight_plan(r) for r in self.flights.values() if r["aircraft_id"] != req.aircraft_id]
            resp = await asyncio.to_thread(
                self.planner.plan,
                request_id=req.request_id,
                aircraft_id=req.aircraft_id,
                vehicle_type=req.vehicle_type,
                origin=origin,
                destination=req.destination_node,
                now=now,
                planned=planned,
            )
        await self.store.save_request(
            request_id=str(req.request_id),
            aircraft_id=req.aircraft_id,
            source=t.source,
            vehicle_type=req.vehicle_type,
            destination=req.destination_node,
            origin=origin,
            response=resp.model_dump(mode="json"),
        )
        self.stats[f"route_response_{resp.status}"] += 1
        if resp.status == "ok":
            await self._remember(resp.model_dump(mode="json"), origin)
            if self.planner.llm is not None:
                self._spawn(self._llm_revision(t, resp, origin))
        return [(reply(t, "route-response"), encode(resp))]

    async def _remember(self, response: dict[str, Any], origin: str | None) -> None:
        """Persist a response (and every later revision) so a pilot can select from it after a restart.

        The first response of a request is already in ``route_requests``; only revisions are added here.
        """
        rid = response["request_id"]
        if response["revision"] > 0:
            await self.store.add_revision(rid, response["revision"], origin, response)
        self.revision[rid] = max(self.revision.get(rid, 0), response["revision"])

    async def _next_revision(self, request_id: str) -> int:
        """The revision number for the next response; after a restart it continues from the stored ones."""
        if request_id not in self.revision:
            self.revision[request_id] = await self.store.max_revision(request_id)
        return self.revision[request_id] + 1

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _emit(self, messages: list[Outbound]) -> None:
        if self.publish is not None:
            await self.publish(messages)
        else:
            self.unsent.extend(messages)

    async def _llm_revision(self, t: Topic, resp: RouteResponse, origin: str | None) -> None:
        """Send the LLM's checked summary as the next revision once it arrives (never blocks the options)."""
        try:
            rev = await asyncio.to_thread(self.planner.llm_revision, resp)
        except Exception:  # noqa: BLE001 - the template summary already went out
            log.exception("llm summary failed")
            return
        if rev is None:
            self.stats["summary_llm_rejected"] += 1
            return
        await self._remember(rev.model_dump(mode="json"), origin)
        self.stats["summary_llm_revision"] += 1
        await self._emit([(reply(t, "route-response"), encode(rev))])

    def _refusal(self, req: RouteRequest, now: datetime, reason: str) -> RouteResponse:
        s = self.planner.settings
        return RouteResponse(
            schema_version=SCHEMA_VERSION,
            message_id=uuid4(),
            request_id=req.request_id,
            aircraft_id=req.aircraft_id,
            revision=0,
            status="no_route",
            method=self.planner.method,
            forecast_time=now,
            expires_at=now + timedelta(seconds=60),
            forecaster="none",
            graph_version=s.graph_version,
            constraints_cycle=s.constraints_cycle,
            summary=None,
            reason=reason[:200],
            routes=[],
        )

    # --- route selection -> planned flight ----------------------------------------------------------------
    async def on_route_selection(self, t: Topic, payload: bytes) -> list[Outbound]:
        sel = RouteSelection.model_validate_json(payload)
        if self._wrong_topic(t, sel.aircraft_id):
            return []
        if self.live.seen_before(str(sel.message_id)):
            return []
        stored = await self.store.get_request(str(sel.request_id))
        if stored is None or stored["aircraft_id"] != sel.aircraft_id:
            self.stats["selection_unknown_request"] += 1
            return []
        revisions = await self.store.revisions(str(sel.request_id))
        candidates = [(stored["response"], stored["origin"])] + [(r["response"], r["origin"]) for r in revisions]
        route = origin = None
        for response, resp_origin in reversed(candidates):  # newest revision first
            route = next(
                (
                    r
                    for r in RouteResponse.model_validate_json(json.dumps(response)).routes
                    if r.route_id == sel.route_id
                ),
                None,
            )
            if route is not None:
                origin = resp_origin
                break
        if route is None or origin is None:
            self.stats["selection_unknown_route"] += 1
            return []

        path = [origin, *(e.node for e in route.eta)]
        return await self._record_flight(
            t,
            aircraft_id=sel.aircraft_id,
            request_id=str(sel.request_id),
            route_id=str(sel.route_id),
            vehicle_type=stored["vehicle_type"],
            path=path,
            destination=stored["destination"],
            eta_by_node=self.planner.eta_by_node(route),
        )

    async def _record_flight(
        self,
        t: Topic,
        *,
        aircraft_id: str,
        request_id: str,
        route_id: str,
        vehicle_type: str,
        path: list[str],
        destination: str,
        eta_by_node: dict[str, float],
    ) -> list[Outbound]:
        """Store a planned flight (replacing the aircraft's previous one) and announce it."""
        out: list[Outbound] = []
        if old := self.flights.get(aircraft_id):
            out.append(await self._set_status(t, old, "replaced", "pilot selected a new route"))
        row = dict(
            flight_id=str(uuid4()),
            aircraft_id=aircraft_id,
            source=t.source,
            request_id=request_id,
            route_id=route_id,
            vehicle_type=vehicle_type,
            path=path,
            destination=destination,
            departure_s=self.clock().timestamp(),
            eta_by_node=eta_by_node,
            status="planned",
        )
        self._status_msg(row, "planned")  # validate before storing
        await self.store.add_flight(**row)
        self.flights[aircraft_id] = row
        await self.refresh_forecast()
        msg = self._status_msg(row, "planned")
        self.stats["flights_planned"] += 1
        if vehicle_type == "medical":
            await self.events.record(
                "medical_dispatch",
                aircraft_id=aircraft_id,
                message=f"medical flight {aircraft_id} -> {destination}",
                detail={"flight_id": row["flight_id"], "destination": destination},
            )
        return [*out, (reply(t, "flight-status"), encode(msg))]

    # --- simulator plans (replay + report plans, PLAN E6.4) -----------------------------------------
    async def on_flight_plan(self, t: Topic, payload: bytes) -> list[Outbound]:
        """The path a simulated aircraft actually flies, when none of the offered options matched it."""
        msg = SimFlightPlan.model_validate_json(payload)
        if t.source != "sim" or self._wrong_topic(t, msg.aircraft_id):
            self.stats["flight_plan_rejected"] += 1
            return []
        if self.live.seen_before(str(msg.message_id)):
            return []
        bad = [n for n in msg.path if n not in self.planner.network.nodes]
        if len(msg.path) < 2 or bad:
            self.stats["flight_plan_invalid"] += 1
            return []
        planned = [flight_plan(r) for r in self.flights.values() if r["aircraft_id"] != msg.aircraft_id]
        etas = await asyncio.to_thread(
            self.planner.etas_for_path, msg.path, self.clock().timestamp(), msg.vehicle_type, planned
        )
        self.stats["flight_plan_recorded"] += 1
        return await self._record_flight(
            t,
            aircraft_id=msg.aircraft_id,
            request_id=str(msg.request_id or uuid4()),
            route_id=str(uuid4()),
            vehicle_type=msg.vehicle_type,
            path=msg.path,
            destination=msg.path[-1],
            eta_by_node=etas,
        )

    # --- simulated pilots follow AeroLink's rules (contract §8) -----------------------------------------
    def _sim_only(self, t: Topic, kind: str) -> bool:
        """True (and logged) when a simulator-only message arrives in the aircraft namespace: ignored."""
        if t.source == "sim":
            return False
        self.stats[f"{kind}_ignored"] += 1
        log.info("ignored %s on %s: only simulated pilots send it", kind, t)
        return True

    async def on_ai_ack(self, t: Topic, payload: bytes) -> list[Outbound]:
        """The simulated pilot saw the AI disclaimer: recorded as the unit's acknowledgement, at the CURRENT version."""
        if self._sim_only(t, "ai-ack"):
            return []
        try:
            msg = json.loads(payload)
        except ValueError:
            msg = None
        if not isinstance(msg, dict) or msg.get("aircraft_id") != t.aircraft_id:
            self.stats["ai_ack_rejected"] += 1
            log.info("ignored ai-ack on %s: bad payload", t)
            return []
        await record_ack(self, DISCLAIMER_VERSION, by=f"sim {t.aircraft_id}", aircraft_id=t.aircraft_id)
        self.stats["ai_ack_recorded"] += 1
        return []

    async def on_reroute_response(self, t: Topic, payload: bytes) -> list[Outbound]:
        """The simulated pilot accepts or refuses an approved ATC reroute: the same logic as a real pilot's answer."""
        if self._sim_only(t, "reroute-response"):
            return []
        try:
            msg = json.loads(payload)
        except ValueError:
            msg = None
        answer = msg.get("response") if isinstance(msg, dict) else None
        rid = msg.get("reroute_id") if isinstance(msg, dict) else None
        if not isinstance(msg, dict) or msg.get("aircraft_id") != t.aircraft_id or not isinstance(rid, str):
            self.stats["reroute_response_rejected"] += 1
            log.info("ignored reroute-response on %s: bad payload", t)
            return []
        try:
            if answer == "acknowledge":
                _, _, out = await reroute.acknowledge(self, rid, t.aircraft_id)
                return out
            if answer == "unable":
                await reroute.unable(self, rid, t.aircraft_id)
                return []
        except HTTPException as e:  # not approved, someone else's, stale: refused, the state stays as it was
            self.stats["reroute_response_refused"] += 1
            log.info("reroute-response %s from %s refused: %s", rid, t.aircraft_id, e.detail)
            return []
        self.stats["reroute_response_rejected"] += 1
        log.info("ignored reroute-response on %s: unknown answer %r", t, answer)
        return []

    async def _sim_left(self, aircraft_id: str, reason: str) -> list[Outbound]:
        """A simulated aircraft is gone: forget its track and end its flight."""
        self.live.drop(aircraft_id)
        row = self.flights.get(aircraft_id)
        if row is None:
            return []
        out = await self._set_status(Topic("sim", aircraft_id, "flight-status"), row, "ended", reason)
        await self.refresh_forecast()
        return [out]

    async def reset_sim(self) -> list[Outbound]:
        """Scenario switch: every simulated aircraft and flight disappears now (the sim then replays anew)."""
        out: list[Outbound] = []
        for aid in [a for a, tr in self.live.tracks.items() if tr.source == "sim"] + [
            a for a, r in self.flights.items() if r["source"] == "sim"
        ]:
            out += await self._sim_left(aid, "scenario switched")
        self.stats["sim_resets"] += 1
        return out

    def expire_tracks(self) -> int:
        """Drop simulated aircraft that went silent (a stopped simulator sends no goodbye)."""
        now, ttl = self.clock(), self.planner.settings.track_ttl_s
        gone = [
            a for a, tr in self.live.tracks.items() if tr.source == "sim" and (now - tr.timestamp).total_seconds() > ttl
        ]
        for aid in gone:
            self.live.drop(aid)
        return len(gone)

    # --- live loops ---------------------------------------------------------------------------------------
    async def refresh_forecast(self) -> None:
        plans = [flight_plan(r) for r in self.flights.values()]
        self.forecast = await asyncio.to_thread(self.planner.traffic, plans)
        self.forecast_at = self.clock()
        await self._refresh_etas()

    async def _refresh_etas(self) -> None:
        """Re-predict each flight's destination ETA against today's traffic and record what changed it.

        A flight's ``prediction_history`` gets an entry when its ETA moves by more than ETA_CHANGE_S, or
        when a new flight appears on nodes it shares (so a medical dispatch shows up as the reason).
        """
        rows = list(self.flights.values())
        now = self.clock()
        current = {r["flight_id"] for r in rows}
        new_ids = current - self._known_flights
        self._known_flights = current
        for fid in [f for f in self.eta_state if f not in current]:
            del self.eta_state[fid]
        for row in rows:
            fid, aid = row["flight_id"], row["aircraft_id"]
            flight = flight_plan(row)
            from_node = flight.path[0]
            if row["status"] in ("active", "off_route") and (track := self._fresh_track(aid)) is not None:
                from_node = self.planner.next_node(flight.path, track.last.position.lat, track.last.position.lon)
            if from_node == flight.destination_node:
                continue
            others = self._others(aid)
            try:
                p10, p50, p90 = await asyncio.to_thread(
                    self.planner.predict_arrival, flight, row["vehicle_type"], others, from_node, now.timestamp()
                )
            except Exception:  # noqa: BLE001 - a prediction failure must not stop the forecast loop
                log.exception("arrival prediction failed for %s", aid)
                continue
            shared = set(flight.path)
            appeared = [
                r for r in rows if r["flight_id"] in new_ids and r["flight_id"] != fid and shared & set(r["path"])
            ]
            st = self.eta_state.get(fid)
            if st is None:
                st: dict[str, Any] = {"history": []}
                self.eta_state[fid] = st
                reason = "initial estimate"
            else:
                moved = abs(p50 - st["eta_p50"]) > ETA_CHANGE_S
                if not moved and not appeared:
                    continue  # the recorded window stays until something moves it
                reason = "traffic forecast changed"
            if appeared:
                who = ", ".join(
                    f"{'medical' if r['vehicle_type'] == 'medical' else r['vehicle_type']} flight {r['aircraft_id']}"
                    for r in appeared
                )
                reason = f"{who} planned on shared nodes"
            st.update(eta_p10=p10, eta_p50=p50, eta_p90=p90)
            st["history"].append({"t": now.timestamp(), "eta_p50": p50, "eta_p90": p90, "reason": reason})
            del st["history"][:-HISTORY_KEEP]

    def _fresh_track(self, aircraft_id: str):
        track = self.live.tracks.get(aircraft_id)
        if track is None or (self.clock() - track.timestamp).total_seconds() > STALE_S:
            return None
        return track

    def _others(self, aircraft_id: str) -> list:
        return [flight_plan(r) for r in self.flights.values() if r["aircraft_id"] != aircraft_id]

    async def suggest(self) -> list[Outbound]:
        """Re-evaluate every active flight from the next node on its route; revise it if a clearly better one exists."""
        out: list[Outbound] = []
        for row in list(self.flights.values()):
            if row["status"] != "active" or (track := self._fresh_track(row["aircraft_id"])) is None:
                continue
            flight = flight_plan(row)
            pos = track.last.position
            from_node = self.planner.next_node(flight.path, pos.lat, pos.lon)
            if from_node == row["destination"]:
                continue
            rid = row["request_id"]
            resp = await asyncio.to_thread(
                self.planner.suggest,
                aircraft_id=row["aircraft_id"],
                from_node=from_node,
                vehicle_type=row["vehicle_type"],
                planned=self._others(row["aircraft_id"]),
                flight=flight,
                revision=await self._next_revision(rid),
                now=self.clock(),
                speed_mps=track.last.motion.ground_speed_mps,  # in flight: not a from-rest ETA query
            )
            if resp is None or resp.status != "ok":
                await reroute.faster_gone(self, row["flight_id"])  # no better route any more: stale proposal
                continue
            await self._remember(resp.model_dump(mode="json"), from_node)
            out.append((str(Topic(row["source"], row["aircraft_id"], "route-response")), encode(resp)))
            self.stats[f"suggestion_revision_{resp.revision}"] += 1
            await reroute.from_suggestion(self, row, resp, from_node)  # ATC sees it too (the pilot still gets it)
        return out

    async def reroute_scan(self) -> None:
        """ATC reroute proposals: expire the stale ones, then propose for predicted conflicts."""
        await reroute.expire_stale(self)
        await reroute.scan_conflicts(self)

    async def scan_off_route(self) -> list[Outbound]:
        """The method's off-route rule on every flying aircraft; on a hit: status off_route + fresh options.

        AeroLink uses the learned detector; classical uses B1's fixed 300 m / ±60 s rule.
        """
        out: list[Outbound] = []
        aerolink = self.planner.method == "aerolink"
        for row in list(self.flights.values()):
            aid = row["aircraft_id"]
            if row["status"] not in ("active", "off_route") or (track := self._fresh_track(aid)) is None:
                continue
            if not track.judgeable:  # a segment just began after a gap or reconnect: wait for consecutive samples
                continue
            if not aerolink:
                pos = track.last.position
                eta = row["eta_by_node"].get(row["destination"])
                flagged = self.planner.classical_off_route(
                    row["path"], pos.lat, pos.lon, self.clock().timestamp(), float(eta) if eta is not None else None
                )
            else:
                if aid not in self.windows:
                    t0 = self.activated.get(aid)
                    self.windows[aid] = (
                        await asyncio.to_thread(self.planner.arrival_window, row["path"], row["vehicle_type"], t0)
                        if t0 is not None
                        else None
                    )
                flagged = await asyncio.to_thread(
                    self.planner.detect_off_route,
                    row["path"],
                    row["vehicle_type"],
                    list(track.times),
                    list(track.history),
                    self.windows[aid],
                )
            if flagged is None:
                continue
            t = Topic(row["source"], aid, "flight-status")
            pos, speed = track.last.position, track.last.motion.ground_speed_mps
            if flagged and row["status"] == "active":
                track.off_route = True
                if aerolink:
                    self.planner.mark_off_route(aid, pos.lat, pos.lon, speed)
                detected: Detector = (
                    "ai" if aerolink else "rule"
                )  # the learned detector, or B1's fixed rule (classical)
                self.off_route_by[aid] = detected
                out.append(await self._set_status(t, row, "off_route", "left the planned route", detected))
                await self.events.record(
                    "off_route",
                    aircraft_id=aid,
                    severity="warn",
                    message=f"{aid} left the planned route",
                    detail={"flight_id": row["flight_id"], "lat": pos.lat, "lon": pos.lon},
                )
                flight = flight_plan(row)
                resp = await asyncio.to_thread(
                    self.planner.recalculate,
                    flight=flight,
                    aircraft_id=aid,
                    vehicle_type=row["vehicle_type"],
                    lat=pos.lat,
                    lon=pos.lon,
                    planned=self._others(aid),
                    revision=await self._next_revision(row["request_id"]),
                    now=self.clock(),
                    speed_mps=speed,  # in flight: not a from-rest ETA query
                )
                if resp is not None:
                    response, origin = resp
                    await self._remember(response.model_dump(mode="json"), origin)
                    out.append((str(Topic(row["source"], aid, "route-response")), encode(response)))
                    self.stats["offroute_recalculated"] += 1
                await self.refresh_forecast()
            elif not flagged and row["status"] == "off_route":
                track.off_route = False
                if aerolink:
                    self.planner.clear_off_route(aid)
                self.off_route_by.pop(aid, None)
                out.append(await self._set_status(t, row, "active", "back on the planned route"))
        return out

    def traffic_messages(self) -> list[Outbound]:
        """One `traffic` message per recently seen aircraft (ARCHITECTURE §12.2)."""
        now = self.clock()
        t0 = now.timestamp()
        radius = self.planner.settings.nearby_radius_m
        out: list[Outbound] = []
        for track in list(self.live.tracks.values()):
            if not track.online or (now - track.timestamp).total_seconds() > STALE_S:
                continue
            pos = track.last.position
            nearby = [
                NearbyAircraft(
                    id=o.aircraft_id,
                    vehicle_type=o.last.vehicle_type,
                    lat=o.last.position.lat,
                    lon=o.last.position.lon,
                    alt_msl_m=o.last.position.alt_msl_m,
                    course_deg=o.last.motion.course_deg,
                    off_route=o.off_route,
                )
                for o in self.live.nearby(pos.lat, pos.lon, radius, exclude=track.aircraft_id)[:50]
            ]
            edges = []
            if row := self.flights.get(track.aircraft_id):
                path = row["path"]
                for u, v in list(zip(path, path[1:], strict=False))[:100]:
                    if (u, v) not in self.planner.network.edges:
                        continue
                    lv = [self.planner.edge_levels(self.forecast, u, v, t0 + m * 60) for m in (0, 15, 30)]
                    edges.append(EdgeTraffic(edge=f"{u}->{v}", now=lv[0], in_15_min=lv[1], in_30_min=lv[2]))
            msg = Traffic(
                schema_version=SCHEMA_VERSION,
                message_id=uuid4(),
                aircraft_id=track.aircraft_id,
                forecast_time=now,
                aircraft=nearby,
                edges=edges,
            )
            out.append((str(Topic(track.source, track.aircraft_id, "traffic")), encode(msg)))
        return out
