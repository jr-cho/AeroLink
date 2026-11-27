"""ATC requirement of a route and the controller's "handled" mark (PLAN E6.14).

What the route crosses decides what, if anything, ATC must be told by radio. The corridor network already flags
each leg (``build_network.py``): ``coordination_airspace`` (Class D) and ``clearance_airspace`` (Class B). Nothing
here invents geometry; a leg's airspace id (``faa-class_d-lakeland-class-d-876``) gives the class letter and name.

====================  ==========================  ===========================================
requirement           airspace crossed            controller's mark
====================  ==========================  ===========================================
``none``              Class G only                (nothing to do; marking is refused, 409)
``contact_tower``     Class D: two-way radio      ``contacted``
``contact_approach``  Class C: contact approach   ``contacted``
``clearance``         Class B: ATC clearance      ``cleared``
====================  ==========================  ===========================================

A route's requirement is the strongest of its legs (``none < contact_tower < contact_approach < clearance``).
A landing site's own airspace (``exempt_airspace`` on its connector legs) COUNTS: the corridor builder exempts it
from the no-go rule so the site is reachable, but landing at or departing from a site inside Class B still needs a
clearance, and inside Class D still needs two-way radio contact (orchestrator ruling, 2026-11-26).

``POST /api/ops/flights/{flight_id}/clearance`` (operator session; 401 without)  body
``{"action": "cleared" | "contacted" | "revoke"}``; ``flight_id`` may also be the aircraft id. The action must match
the requirement (422 ``wrong_action``); a route that needs nothing refuses (409 ``not_needed``); an unknown or
finished flight is 404. Idempotent: repeating a mark changes nothing (the first operator stays); revoking an
unmarked flight is harmless. Returns the flight's ``atc`` object::

    {requirement, airspaces: ["Tampa Class B", ...], legs: [[from, to], ...] (legs that need ATC),
     status: "not_needed" | "pending" | "done", action: "cleared" | "contacted" | null,
     by: username | null, by_name, initials, at: ISO | null}

The same object is ``atc`` on every ``GET /api/ops/routes`` row. Each mark is an audit event of kind ``clearance``
(logger ``aerolink.audit`` and the event log; already acknowledged, so it is not an alert).

The pilot sees it (informational, never a gate): ``GET /api/devices/{id}`` has ``current_flight.atc`` and the screens'
WebSocket gets ``{"type": "clearance", "payload": {flight_id, requirement, airspaces, status, action, by_role: "ops",
at}}`` when it changes. It is not sent over MQTT (the unit has no screen) and the operator's identity stays out of it.
A mark only counts while its requirement equals the flight's current one, so a revised route that crosses other
airspace starts pending again, and a new flight has no mark.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from aerolink.service.auth import require_operator

router = APIRouter()
audit = logging.getLogger("aerolink.audit")

REQUIREMENTS = ("none", "contact_tower", "contact_approach", "clearance")
BY_CLASS = {"B": "clearance", "C": "contact_approach", "D": "contact_tower"}
ACTION_FOR = {"clearance": "cleared", "contact_tower": "contacted", "contact_approach": "contacted"}
# What the record says (L-FIX): the Ops team notes what ATC did by radio; it does not claim to be ATC.
NOTED = {"cleared": "ATC clearance obtained", "contacted": "tower contacted", "revoke": "Ops note removed"}
_ID = re.compile(r"^faa-class_(?P<cls>[a-e])-(?P<name>.+?)-class-[a-e](?:-\d+)?$")

Legs = dict[frozenset[str], tuple[str, ...]]  # leg endpoints -> airspace ids it crosses (coordination + clearance)


def load_legs(network_file: Path) -> Legs:
    """Airspace ids per corridor/connector leg, from the network GeoJSON's own flags."""
    legs: Legs = {}
    for f in json.loads(network_file.read_text())["features"]:
        p = f["properties"]
        if p.get("kind") not in ("corridor", "connector"):
            continue
        ids = (
            *p.get("coordination_airspace", []),
            *p.get("clearance_airspace", []),
            *p.get("exempt_airspace", []),  # a site inside Class B/D still needs ATC for its own airspace
        )
        if ids:
            legs[frozenset((p["from"], p["to"]))] = tuple(dict.fromkeys(ids))
    return legs


def classify(airspace_id: str) -> tuple[str, str] | None:
    """``(requirement, "Name Class X")`` of an airspace id; None for classes that need nothing (E, unknown)."""
    m = _ID.match(airspace_id)
    if m is None:
        return None
    cls = m["cls"].upper()
    req = BY_CLASS.get(cls)
    if req is None:
        return None
    name = " ".join(w.capitalize() for w in m["name"].split("-"))
    return req, f"{name} Class {cls}"


def requirement_for_path(path: list[str], legs: Legs) -> dict[str, Any]:
    """The strongest requirement along ``path`` with the airspaces and legs behind it (pure)."""
    best, names, crossed = "none", [], []
    for u, v in zip(path, path[1:], strict=False):
        leg_req = "none"
        for aid in legs.get(frozenset((u, v)), ()):
            c = classify(aid)
            if c is None:
                continue
            req, name = c
            if name not in names:
                names.append(name)
            if REQUIREMENTS.index(req) > REQUIREMENTS.index(leg_req):
                leg_req = req
        if leg_req != "none":
            crossed.append([u, v])
            if REQUIREMENTS.index(leg_req) > REQUIREMENTS.index(best):
                best = leg_req
    return {"requirement": best, "airspaces": names, "legs": crossed}


def atc_state(
    path: list[str], legs: Legs, mark: dict[str, Any] | None, who: dict[str, str] | None = None
) -> dict[str, Any]:
    """The ``atc`` object of a flight. ``mark`` is its stored row; it only counts for the same requirement."""
    out = requirement_for_path(path, legs)
    out.update(status="not_needed", action=None, by=None, by_name=None, initials=None, at=None)
    if out["requirement"] == "none":
        return out
    out["status"] = "pending"
    if mark is not None and mark["requirement"] == out["requirement"]:
        name = (who or {}).get(mark["operator"], mark["operator"])
        at = mark["at"]
        out.update(
            status="done",
            action=mark["status"],
            by=mark["operator"],
            by_name=name,
            initials=initials(name),
            at=(at if at.tzinfo else at.replace(tzinfo=UTC)).isoformat(),
        )
    return out


def initials(name: str) -> str:
    parts = [p for p in re.split(r"[\s._-]+", name.strip()) if p]
    return ("".join(p[0] for p in parts[:2]) or "?").upper()


def for_pilot(flight_id: str, atc: dict[str, Any]) -> dict[str, Any]:
    """What a pilot's screen may see: no operator identity, just that the Ops team noted it (not an ATC action)."""
    return {
        "flight_id": flight_id,
        "requirement": atc["requirement"],
        "airspaces": atc["airspaces"],
        "status": atc["status"],
        "action": atc["action"],
        "by_role": "ops",  # who noted it: the AeroLink Ops team, never ATC (L-FIX decision 1)
        "at": atc["at"],
    }


def legs_of(app_state: Any) -> Legs:
    return getattr(app_state, "atc_legs", None) or {}


async def operators_by_name(store) -> dict[str, str]:
    return {o["username"]: o["display_name"] for o in await store.list_operators()}


async def atc_for(app_state: Any, row: dict[str, Any]) -> dict[str, Any]:
    """The ``atc`` object of one flight row (device state)."""
    store = app_state.service.store
    mark = (await store.get_clearances()).get(row["flight_id"])
    return atc_state(row["path"], legs_of(app_state), mark, await operators_by_name(store) if mark else None)


class ClearanceBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: str


def _find(svc, flight_id: str) -> dict[str, Any] | None:
    live = ("planned", "active", "off_route")
    return next(
        (
            r
            for r in svc.flights.values()
            if (r["flight_id"] == flight_id or r["aircraft_id"] == flight_id) and r["status"] in live
        ),
        None,
    )


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"error": code, "message": message})


@router.post("/api/ops/flights/{flight_id}/clearance")
async def mark(
    request: Request, flight_id: str, body: ClearanceBody, operator: str = Depends(require_operator)
) -> dict[str, Any]:
    """ATC records that it handled the requirement by radio. Informational: nothing reads it to stop a flight."""
    state = request.app.state
    svc = state.service
    row = _find(svc, flight_id)
    if row is None:
        raise _err(404, "unknown_flight", "no such planned or active flight")
    if body.action not in ("cleared", "contacted", "revoke"):
        raise _err(422, "invalid_request", "action must be cleared, contacted or revoke")
    store, fid = svc.store, row["flight_id"]
    legs = legs_of(state)
    required = requirement_for_path(row["path"], legs)["requirement"]
    current = (await store.get_clearances()).get(fid)
    changed = False
    if body.action == "revoke":
        if current is not None:
            await store.delete_clearance(fid)
            changed = True
    else:
        if required == "none":
            raise _err(409, "not_needed", "this route crosses no airspace that needs ATC action")
        if ACTION_FOR[required] != body.action:
            raise _err(422, "wrong_action", f"this route needs {ACTION_FOR[required]!r}, not {body.action!r}")
        if current is None or current["requirement"] != required:
            await store.set_clearance(fid, required, body.action, operator, svc.clock())
            changed = True
    who = await operators_by_name(store)
    mark_row = (await store.get_clearances()).get(fid)
    atc = atc_state(row["path"], legs, mark_row, who)
    if changed:
        what = NOTED[body.action]
        audit.info("flight %s (%s): %s by %s (%s)", fid, row["aircraft_id"], what, operator, required)
        await svc.events.record(
            "clearance",
            aircraft_id=row["aircraft_id"],
            message=f"{row['aircraft_id']}: {what} (noted by {operator}, {required})",
            detail={"flight_id": fid, "requirement": required, "action": body.action, "operator": operator},
            acked_by=operator,
        )
        hub = state.hub
        hub.publish(row["aircraft_id"], "clearance", for_pilot(fid, atc))
        hub.publish_ops("clearance", {"flight_id": fid, "aircraft_id": row["aircraft_id"], "atc": atc})
    return atc
