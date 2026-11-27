"""Pilot-screen API: pairing, device state, route request/selection, and the WebSocket.

This module is the contract for the pilot page (`/device/<id>`, ARCHITECTURE §13.2). A screen
is never an MQTT client and never chooses the vehicle type: every route request is built here
with the unit's **registered** type and run through ``Service.handle`` exactly like the MQTT
topic, then published to the unit (MQTT) and to every paired screen (WebSocket) alike.

Errors
------
Every error is ``{"detail": {"error": <code>, "message": <text>}}`` with these codes:

====== ================== ====================================================================
status error              meaning
====== ================== ====================================================================
401    session_required   no valid session cookie for this unit (pair again)
403    invalid_code       wrong pairing code (or unknown / unprovisioned unit)
404    unknown_device     the unit is not registered
404    unknown_route      selection names a request/route this unit never received
409    unit_offline       the unit is offline: routing is locked until it is back
409    request_id_conflict  ``request_id`` already belongs to another unit
422    invalid_request    body failed validation (also FastAPI's own 422 for malformed JSON)
429    too_many_attempts  pairing rate limit (5 wrong codes per 60 s per unit); header Retry-After
====== ================== ====================================================================

Session
-------
``POST /api/devices/{id}/pair``  body ``{"code": "K7QF-2XM4TB"}`` (case, spaces and dashes ignored)
  -> 200 ``{"device_id": "dev-001", "expires_at": "<ISO 8601>"}`` and the cookie
  ``aerolink_session_<id>`` (HTTP-only, Secure, SameSite=Lax, Path=/, 7 days). One cookie per
  unit, so one browser can be paired to several units. Several screens may pair to one unit.
  Set ``AEROLINK_COOKIE_SECURE=0`` to drop ``Secure`` for plain-HTTP local development.
  Rotating the code (``devices rotate``) ends every session; open WebSockets close within one
  heartbeat (code 4401).

Device state
------------
``GET /api/devices/{id}`` (session required) -> 200::

    {"id": "dev-001", "callsign": "N123AL"|null (set by the registry, never by the pilot),
     "vehicle_type": "passenger"|"medical", "demo": false,
     "online": true, "last_seen": "<ISO>"|null,
     "latest_fix": null | {"lat", "lon", "alt_msl_m", "ground_speed_mps", "course_deg",
                           "timestamp", "fix": {"type", "hdop", "satellites", "replayed"}},
     "current_flight": null | {"flight_id", "status", "route_id", "request_id", "destination",
                               "path": [node ids], "departure_s", "eta_by_node": {node: unix s},
                               "atc": {requirement: none|contact_tower|contact_approach|clearance, airspaces,
                                       legs, status: not_needed|pending|done, action: cleared|contacted|null,
                                       by_role: "ops"|null, at}},   (E6.14: informational, never blocks anything)
     "latest_route_response": null | <RouteResponse as in ARCHITECTURE §12>}

``GET /api/devices/{id}/atc-requirement?path=n1,n2,...`` (session required) -> 200
  ``{"requirement", "airspaces", "legs"}`` for a candidate route's node path, so route cards can say "Clearance
  needed (Class B)" before the pilot goes. 422 ``invalid_request`` for fewer than two nodes. Informational only.

Routing (both locked with 409 ``unit_offline`` while the unit is offline)
-------------------------------------------------------------------------
``POST /api/devices/{id}/route-request``  body ``{"destination": "ev-orl", "request_id"?: uuid}``
  Extra fields (e.g. a ``vehicle_type``) are ignored. -> 200 the ``route-response`` message
  (``status`` ``ok`` with 1-3 routes, or ``no_route`` with a ``reason``, e.g. a destination that
  doesn't serve the unit's type). Idempotent on ``request_id``: the same answer, not recomputed.
  A repeat is not re-published; a new request is published to MQTT and the WebSocket.

``POST /api/devices/{id}/route-selection``  body ``{"request_id": uuid, "route_id": uuid}``
  -> 200 the ``flight-status`` message (``planned``) of the new flight; a previous flight is
  ``replaced`` (that status goes out on the WebSocket). Selecting the route that is already the
  unit's current flight again returns its current status without re-planning.

``POST /api/devices/{id}/flight/end``  no body (session required; 409 ``unit_offline`` like routing)
  -> 200 the ``flight-status`` message (``ended``, reason "ended by pilot") of the unit's active or
  planned flight; it goes out on MQTT and the WebSocket like any status, and the flight drops out of
  ``/api/flights?active=true`` and ``/api/ops/routes``. Idempotent: with no flight it returns 200
  ``{"status": "ended", "flight_id": null, ...}`` and publishes nothing.

WebSocket
---------
``WS /ws/devices/{id}`` (session cookie required; otherwise the handshake is refused, HTTP 403).
Server -> screen JSON ``{"type", "payload"}``; the screen sends nothing (anything it sends is ignored).

====================== =====================================================================
type                   payload
====================== =====================================================================
``state``              the ``GET /api/devices/{id}`` body; first message, also on reconnect
``route-response``     RouteResponse (also revised options: higher ``revision``)
``flight-status``      FlightStatus (``planned`` / ``active`` / ``off_route`` / ``replaced`` / ``ended``)
``traffic``            Traffic (every ~5 s while the unit is online and flying)
``online``             ``{"at": ISO}`` the unit is back
``offline``            ``{"at": ISO, "reason"?: "no_signal"}`` Last Will or silence; routing locked
``vehicle-type``       ``{"vehicle_type", "demo"}`` a demo unit's button changed the type
``health``             ``{status: healthy|degraded|offline, status_reasons, health, health_age_s, age_s, stale}``: on
                       each health message and whenever the status changes
``clearance``          ``{flight_id, requirement, airspaces, status: pending|done|not_needed,
                       action: cleared|contacted|null, by_role: "ops", at}``: the Ops team noted it handled by
                       radio (or undid it). Informational; never sent over MQTT (the unit has no screen)
``heartbeat``          ``{"t": ISO, "online": bool}`` every WS_HEARTBEAT_SECONDS (5 s); use it to detect a dead socket
====================== =====================================================================

If the socket drops, poll ``GET /api/devices/{id}`` and reconnect; ``state`` repairs any gap.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from aerolink.contracts import SCHEMA_VERSION, NodeId, RouteRequest, RouteSelection
from aerolink.service import pairing, reroute
from aerolink.service.ai import unit_adsb_view
from aerolink.service.ai_api import ack_version, record_ack
from aerolink.service.api import traffic_payload
from aerolink.service.clearance import atc_for, for_pilot, legs_of, requirement_for_path
from aerolink.service.core import Outbound
from aerolink.service.topics import Topic

router = APIRouter()
log = logging.getLogger("aerolink.pilot")
HEARTBEAT_S = 5.0


class PairBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    code: str


class RouteRequestBody(BaseModel):
    model_config = ConfigDict(extra="ignore")  # a screen-supplied vehicle_type is dropped here
    destination: NodeId
    request_id: UUID | None = None


class RouteSelectionBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    request_id: UUID
    route_id: UUID


def err(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(status, {"error": code, "message": message}, headers=headers)


def cookie_name(device_id: str) -> str:
    return f"aerolink_session_{device_id}"


def _secure_cookie() -> bool:
    return os.environ.get("AEROLINK_COOKIE_SECURE", "1").lower() not in ("0", "false", "no")


def _service(request: Request | WebSocket):
    return request.app.state.service


def _hub(request: Request | WebSocket):
    return request.app.state.hub


def _limiter(request: Request) -> pairing.RateLimiter:
    st = request.app.state
    if getattr(st, "pair_limiter", None) is None:
        st.pair_limiter = pairing.RateLimiter()
    return st.pair_limiter


async def _session_ok(app_state: Any, device_id: str, token: str | None) -> bool:
    return bool(token) and await app_state.service.store.session_valid(pairing.token_hash(token), device_id)


async def require_session(request: Request, device_id: str) -> dict[str, Any]:
    """The registered device row, or 401/404."""
    if not await _session_ok(request.app.state, device_id, request.cookies.get(cookie_name(device_id))):
        raise err(401, "session_required", "pair this screen with the unit first")
    dev = await _service(request).store.get_device(device_id)
    if dev is None:
        raise err(404, "unknown_device", f"{device_id} is not registered")
    return dev


# --- state ------------------------------------------------------------------------------------
def _iso(t: datetime | None) -> str | None:
    if t is None:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).isoformat()


async def device_view(app_state: Any, dev: dict[str, Any]) -> dict[str, Any]:
    """The body of ``GET /api/devices/{id}`` and of the WebSocket ``state`` message."""
    service, did = app_state.service, dev["id"]
    app_state.hub.vehicle_types[did] = dev["vehicle_type"]  # so the next type change is noticed
    fix = None
    if track := service.live.tracks.get(did):
        m = track.last
        fix = {
            "lat": m.position.lat,
            "lon": m.position.lon,
            "alt_msl_m": m.position.alt_msl_m,
            "ground_speed_mps": m.motion.ground_speed_mps,
            "course_deg": m.motion.course_deg,
            "timestamp": _iso(m.timestamp),
            "fix": m.fix.model_dump(mode="json"),
        }
    flight = None
    if row := service.flights.get(did):
        keys = ("flight_id", "status", "route_id", "request_id", "destination", "path", "departure_s", "eta_by_node")
        flight = {k: row[k] for k in keys}
        flight["detected_by"] = service.off_route_by.get(did) if row["status"] == "off_route" else None  # contract §7
        atc = await atc_for(app_state, row)
        flight["atc"] = {**for_pilot(row["flight_id"], atc), "legs": atc["legs"]}
        del flight["atc"]["flight_id"]
    latest = await service.store.latest_request(did)
    return {
        "id": did,
        "callsign": dev.get("callsign"),
        "vehicle_type": dev["vehicle_type"],
        "demo": dev["demo"],
        "online": dev["online"],
        "last_seen": _iso(dev["last_seen"]),
        "latest_fix": fix,
        "current_flight": flight,
        "latest_route_response": latest["response"] if latest else None,
        "pending_reroute": await reroute.pending_for(service, did),  # only while an ATC reroute awaits the pilot
        "ai_ack_version": await ack_version(service.store, aircraft_id=did),  # U-API §3: the one-time AI notice
        **unit_adsb_view(app_state, did),  # U-API §4/§9: live ADS-B within 10 NM (advisory) and the feed's state
        **service.units.view(did, bool(dev["online"]), service.clock()),  # status, status_reasons, health, ... (E6.9)
    }


# --- pairing ----------------------------------------------------------------------------------
@router.post("/api/devices/{device_id}/pair")
async def pair(request: Request, response: Response, device_id: str, body: PairBody) -> dict:
    store, limiter = _service(request).store, _limiter(request)
    if limiter.blocked(device_id):
        raise err(429, "too_many_attempts", "too many wrong codes; wait a minute", {"Retry-After": "60"})
    stored = await store.get_pairing(device_id)
    # scrypt is deliberately slow: keep it off the event loop. Unknown units get the same work and answer.
    salt = stored[1] if stored else "00" * 16
    good = await asyncio.to_thread(
        pairing.verify, body.code, stored[0] if stored else pairing.hash_code("-", bytes(16)).hex(), salt
    )
    if not (good and stored):
        limiter.fail(device_id)
        raise err(403, "invalid_code", "wrong pairing code")
    limiter.reset(device_id)
    token = pairing.new_token()
    expires = datetime.now(UTC) + timedelta(seconds=pairing.SESSION_TTL_S)
    await store.add_session(pairing.token_hash(token), device_id, expires)
    response.set_cookie(
        cookie_name(device_id),
        token,
        max_age=pairing.SESSION_TTL_S,
        httponly=True,
        secure=_secure_cookie(),
        samesite="lax",
        path="/",
    )
    return {"device_id": device_id, "expires_at": expires.isoformat()}


@router.get("/api/devices/{device_id}")
async def get_device(request: Request, device_id: str) -> dict:
    dev = await require_session(request, device_id)
    return await device_view(request.app.state, dev)


class AiAckBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: str = Field(min_length=1, max_length=32)


@router.post("/api/devices/{device_id}/ai-ack", status_code=204)
async def device_ai_ack(request: Request, device_id: str, body: AiAckBody) -> Response:
    """The pilot accepts the AI advisory once (recorded as an ``ai_ack`` event for the unit)."""
    await require_session(request, device_id)
    await record_ack(_service(request), body.version, by=f"pilot {device_id}", aircraft_id=device_id)
    return Response(status_code=204)


@router.get("/api/devices/{device_id}/traffic")
async def device_traffic(request: Request, device_id: str) -> dict:
    """The ``/api/traffic`` body for a paired screen (U-API §5): the same builder, behind the pilot's session."""
    await require_session(request, device_id)
    return traffic_payload(_service(request))


@router.get("/api/devices/{device_id}/atc-requirement")
async def atc_requirement(request: Request, device_id: str, path: str = "") -> dict:
    """What a candidate route would need from ATC (informational; route selection never looks at it)."""
    await require_session(request, device_id)
    nodes = [n for n in path.split(",") if n]
    if len(nodes) < 2:
        raise err(422, "invalid_request", "path needs at least two comma-separated node ids")
    return requirement_for_path(nodes, legs_of(request.app.state))


# --- routing ----------------------------------------------------------------------------------
async def publish(request: Request, messages: list[Outbound]) -> None:
    """To the unit over MQTT and to paired screens. ``MqttLink.publish`` already feeds the hub."""
    link = request.app.state.link
    if link is None:
        _hub(request).publish_messages(messages)
        return
    try:
        await link.publish(messages)
    except Exception:  # the broker being down must not fail the pilot's request
        log.exception("mqtt publish failed; screens still get the messages")
        _hub(request).publish_messages(messages)


def _require_online(dev: dict[str, Any]) -> None:
    if not dev["online"]:
        raise err(409, "unit_offline", "Unit not connected: routing is locked until it is back")


@router.post("/api/devices/{device_id}/route-request")
async def route_request(request: Request, device_id: str, body: RouteRequestBody) -> dict:
    dev = await require_session(request, device_id)
    _require_online(dev)
    service = _service(request)
    request_id = body.request_id or uuid4()
    stored = await service.store.get_request(str(request_id))
    if stored is not None and stored["aircraft_id"] != device_id:
        raise err(409, "request_id_conflict", "request_id belongs to another unit")
    msg = RouteRequest(
        schema_version=SCHEMA_VERSION,
        message_id=uuid4(),
        request_id=request_id,
        aircraft_id=device_id,
        vehicle_type=dev["vehicle_type"],  # the registered type, never the screen's
        timestamp=service.clock(),
        destination_node=body.destination,
    )
    out = await service.handle(str(Topic("aircraft", device_id, "route-request")), msg.model_dump_json().encode())
    if not out:
        raise err(422, "invalid_request", "the route request was rejected")
    if stored is None:
        await publish(request, out)
    return json.loads(out[0][1])


@router.post("/api/devices/{device_id}/route-selection")
async def route_selection(request: Request, device_id: str, body: RouteSelectionBody) -> dict:
    dev = await require_session(request, device_id)
    _require_online(dev)
    service = _service(request)
    current = service.flights.get(device_id)
    if current and current["request_id"] == str(body.request_id) and current["route_id"] == str(body.route_id):
        return {  # idempotent: this route is already the unit's flight
            "aircraft_id": device_id,
            "flight_id": current["flight_id"],
            "status": current["status"],
            "reason": None,
            "timestamp": _iso(service.clock()),
        }
    msg = RouteSelection(
        schema_version=SCHEMA_VERSION,
        message_id=uuid4(),
        request_id=body.request_id,
        aircraft_id=device_id,
        route_id=body.route_id,
        timestamp=service.clock(),
    )
    out = await service.handle(str(Topic("aircraft", device_id, "route-selection")), msg.model_dump_json().encode())
    planned = next((json.loads(p) for _, p in reversed(out) if json.loads(p).get("status") == "planned"), None)
    if planned is None:
        raise err(404, "unknown_route", "that request or route was not offered to this unit")
    await publish(request, out)
    return planned


@router.post("/api/devices/{device_id}/reroutes/{reroute_id}/acknowledge")
async def reroute_acknowledge(request: Request, device_id: str, reroute_id: str) -> dict:
    """The pilot accepts ATC's reroute: the flight switches to the proposed route (as a route selection would)."""
    dev = await require_session(request, device_id)
    service = _service(request)
    prior = await service.store.get_reroute(reroute_id)
    if prior is None or prior["status"] != "acknowledged":  # a repeat of a done acknowledgement is always 200
        _require_online(dev)  # the first one switches the route: routing is locked while the unit is offline
    done, flight_status, out = await reroute.acknowledge(service, reroute_id, device_id)
    if out:
        await publish(request, out)
    return {"reroute": done, "flight_status": flight_status}


@router.post("/api/devices/{device_id}/reroutes/{reroute_id}/unable")
async def reroute_unable(request: Request, device_id: str, reroute_id: str) -> dict:
    """The pilot cannot take the reroute (PIC authority): the route stays and ATC is told."""
    await require_session(request, device_id)
    service = _service(request)
    done = await reroute.unable(service, reroute_id, device_id)
    return {"reroute": done, "flight_status": reroute.flight_status(service, device_id)}


@router.post("/api/devices/{device_id}/flight/end")
async def flight_end(request: Request, device_id: str) -> dict:
    """The pilot ends the flight (the unit is not told to do anything: this only closes the shared picture)."""
    dev = await require_session(request, device_id)
    _require_online(dev)
    service = _service(request)
    row = service.flights.get(device_id)
    if row is None or row["status"] in ("ended", "replaced"):
        return {
            "aircraft_id": device_id,
            "flight_id": None,
            "status": "ended",
            "reason": "ended by pilot",
            "timestamp": _iso(service.clock()),
        }
    out = await service._set_status(Topic("aircraft", device_id, "flight-status"), row, "ended", "ended by pilot")
    await service.refresh_forecast()
    await publish(request, [out])
    return json.loads(out[1])


# --- WebSocket --------------------------------------------------------------------------------
@router.websocket("/ws/devices/{device_id}")
async def ws_device(ws: WebSocket, device_id: str) -> None:
    token = ws.cookies.get(cookie_name(device_id))
    if not await _session_ok(ws.app.state, device_id, token):
        await ws.close(code=4401)  # before accept: the handshake is refused
        return
    store, hub = ws.app.state.service.store, ws.app.state.hub
    dev = await store.get_device(device_id)
    if dev is None:
        await ws.close(code=4404)
        return
    await ws.accept()
    feed = getattr(ws.app.state, "adsb", None)
    if hasattr(feed, "touch"):
        feed.touch()  # a connected screen with a flight counts as someone looking: poll at the active rate now
    queue = hub.subscribe(device_id)  # subscribe first so nothing between state and loop is lost
    heartbeat_s = getattr(ws.app.state, "ws_heartbeat_s", HEARTBEAT_S)
    gone = asyncio.create_task(_until_disconnect(ws))
    try:
        await ws.send_json({"type": "state", "payload": await device_view(ws.app.state, dev)})
        while not gone.done():
            nxt = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({nxt, gone}, timeout=heartbeat_s, return_when=asyncio.FIRST_COMPLETED)
            if nxt in done:
                await ws.send_json(nxt.result())
                continue
            nxt.cancel()
            if gone.done():
                break
            if not await _session_ok(ws.app.state, device_id, token):  # code rotated: end this screen
                await ws.close(code=4401)
                break
            dev = await store.get_device(device_id)
            online = bool(dev and dev["online"])
            await ws.send_json({"type": "heartbeat", "payload": {"t": datetime.now(UTC).isoformat(), "online": online}})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.unsubscribe(device_id, queue)
        gone.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await gone


async def _until_disconnect(ws: WebSocket) -> None:
    """Return when the screen goes away; whatever it sends is ignored."""
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        while True:
            await ws.receive_text()
