"""AI reroute proposals: the AI proposes, ATC approves, the pilot acknowledges or refuses (UI-AI-PLAN §B, revised).

Lifecycle of one proposal (``reroutes`` table, contract §2)::

    proposed --approve--> approved --acknowledge--> acknowledged   (the flight switches to the proposed route)
       |                     \\--unable------------> unable          (the flight keeps its route; ATC is told)
       \\--dismiss / expire--> dismissed | expired    (approved ones can also be dismissed or expire)

Proposals come from the existing route engine (no new routing):

* ``faster``: the suggestion engine's own en-route answer for an active flight (A1's >=10 % rule, unchanged),
  made by ``Service.suggest``;
* ``conflict``: a predicted conflict between two planned flights (``ops_traffic.route_conflicts``) that a reroute of
  one active flight avoids, found by ``scan_conflicts``;
* ``medical``: the same two, for a medical flight, which ATC always sees first.

One open proposal per flight. A proposal expires after ``TTL_S`` without a decision (an approval gets the same time
for the pilot), when its flight ends or changes route, when its suggestion or conflict is no longer predicted.
Every transition is an event-log entry (kind ``reroute``) and an audit line.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request

from aerolink.contracts import SCHEMA_VERSION, Route, RouteResponse, RouteSelection, VehicleType
from aerolink.service.auth import require_operator
from aerolink.service.clearance import requirement_for_path
from aerolink.service.devices import callsign_of
from aerolink.service.ops_traffic import Flight, node_windows, rank, route_conflicts
from aerolink.service.planner import FL_TZ, flight_plan
from aerolink.service.topics import Topic
from aerolink.sim.state import FlightPlan

if TYPE_CHECKING:
    from aerolink.service.core import Outbound, Service

router = APIRouter()
audit = logging.getLogger("aerolink.audit")

TTL_S = 180.0  # a proposal, or an approval awaiting the pilot, lapses after this long without a decision (3 min)
CONFLICT_HORIZON_S = 2700.0  # the same 45-minute horizon ATC's conflict list uses
LIVE = ("planned", "active", "off_route")
OPEN = ("proposed", "approved")
STATUSES = ("proposed", "approved", "acknowledged", "unable", "dismissed", "expired")
SYSTEM = "aerolink"
SIM_PILOT = "simulated pilot"  # who a simulated pilot's answer is recorded as (contract B)
ANSWERS = ("acknowledged", "unable")  # a pilot's accept / decline


# --- shape and small helpers ------------------------------------------------------------------------------------
def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def requirement_change(row: dict[str, Any]) -> dict[str, str] | None:
    """``{from, to}`` when the proposed route needs a different airspace requirement than the current one, else None.

    Both sides use the clearance feature's rule (``clearance.requirement_for_path``): ``from`` is the flight's own
    ``atc.requirement``, ``to`` is ``proposed.atc.requirement``. Rows stored before this existed have no ``from``."""
    before = (row["current"].get("atc") or {}).get("requirement")
    after = (row["proposed"].get("atc") or {}).get("requirement")
    if before is None or after is None or before == after:
        return None
    return {"from": before, "to": after}


def public(row: dict[str, Any]) -> dict[str, Any]:
    """The contract's ``Reroute`` object for one stored row."""
    return {
        "id": row["id"],
        "flight_id": row["flight_id"],
        "aircraft_id": row["aircraft_id"],
        "source": row["source"],
        "simulated": row["source"] == "sim",
        "callsign": row["callsign"],
        "destination": row["destination"],
        "status": row["status"],
        "reason": row["reason"],
        "trigger": row["trigger"],
        "gain_s": row["gain_s"],
        "current": row["current"],
        "proposed": row["proposed"],
        "requirement_change": requirement_change(row),
        "created_at": _aware(row["created_at"]).isoformat(),
        "expires_at": _aware(row["expires_at"]).isoformat(),
        "approved_by": row["approved_by"],
        "approved_at": _aware(row["approved_at"]).isoformat() if row["approved_at"] else None,
        "responded_at": _aware(row["responded_at"]).isoformat() if row["responded_at"] else None,
        "events": row["events"],
    }


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"error": code, "message": message})


def _clock_text(t_s: float) -> str:
    d = datetime.fromtimestamp(t_s, FL_TZ)
    return f"{d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def clock_range(a_s: float, b_s: float) -> str:
    """``7:42 to 7:50 PM`` (or ``11:58 AM to 12:05 PM``) in the network's local time."""
    da = datetime.fromtimestamp(a_s, FL_TZ)
    if (da.hour < 12) == (datetime.fromtimestamp(b_s, FL_TZ).hour < 12):
        return f"{da.hour % 12 or 12}:{da.minute:02d} to {_clock_text(b_s)}"
    return f"{_clock_text(a_s)} to {_clock_text(b_s)}"


def _live_flights(svc: Service) -> list[Flight]:
    return [
        Flight(r["flight_id"], r["aircraft_id"], r["vehicle_type"], r.get("source", "aircraft"), r["status"],
               flight_plan(r))
        for r in svc.flights.values() if r["status"] in LIVE
    ]  # fmt: skip


def _flight_row(svc: Service, flight_id: str) -> dict[str, Any] | None:
    return next((r for r in svc.flights.values() if r["flight_id"] == flight_id and r["status"] in LIVE), None)


def _stale_reason(svc: Service, row: dict[str, Any], now: datetime) -> str | None:
    """Why an open proposal no longer stands, or None."""
    if now >= _aware(row["expires_at"]):
        return "no decision in time"
    fl = _flight_row(svc, row["flight_id"])
    if fl is None:
        now_flying = svc.flights.get(row["aircraft_id"])  # a new route selection replaces the flight (new flight id)
        return (
            "route changed"
            if now_flying is not None and now_flying["flight_id"] != row["flight_id"]
            else "flight ended"
        )
    if fl["route_id"] != row["current"]["route_id"]:
        return "route changed"
    return None


async def _callsign(svc: Service, aircraft_id: str, source: str) -> str:
    dev = await svc.store.get_device(aircraft_id)
    return callsign_of(aircraft_id, source, {aircraft_id: dev.get("callsign") if dev else None})


def congestion_ahead(svc: Service, plan: FlightPlan, from_node: str, t0: float) -> tuple[str, str, float, float] | None:
    """The forecast's worst traffic ahead on the route: the first heavy node, else the first moderate one.

    Returns ``(level, node, p10, p90)`` with the node's arrival window; None when the route ahead is quiet.
    """
    path = plan.path[plan.path.index(from_node) :] if from_node in plan.path else plan.path
    windows = node_windows(plan, t0)
    found: dict[str, tuple[str, float, float]] = {}
    for n in path:
        w = windows.get(n)
        if w is None:
            continue
        level = svc.planner.level(svc.forecast, n, w[1])
        if level in ("heavy", "moderate") and level not in found:
            found[level] = (n, w[0], w[2])
    for level in ("heavy", "moderate"):
        if level in found:
            node, p10, p90 = found[level]
            return level, node, p10, p90
    return None


def _route_path(from_node: str, route: Route) -> list[str]:
    return [from_node, *[e.node for e in route.eta]]


def _remaining(path: list[str], from_node: str) -> list[str]:
    """The part of the current route still ahead, from the node the proposal starts at (the whole path if the node
    is not on it). Both sides of ``requirement_change`` must cover the same stretch, or a flight that already
    passed Class B would show a false change."""
    return path[path.index(from_node) :] if from_node in path else path


def _route_end(route: Route) -> float:
    """Predicted arrival at the destination incl. expected waits: the same basis as a flight's ETA window."""
    return route.eta[-1].eta_p50.timestamp() + route.expected_wait_s


def _avoids(path: list[str], node: str | None, edge: list[str] | None) -> bool:
    """Whether a route keeps clear of a conflict's place (a junction node, or the edge of an opposing pair)."""
    if node is not None:
        return node not in path
    if edge is not None:
        return not any({a, b} == set(edge) for a, b in zip(path, path[1:], strict=False))
    return False


async def _current_end(svc: Service, row: dict[str, Any], from_node: str) -> float:
    """The flight's predicted arrival on its current route (what ATC sees as its ETA)."""
    st = svc.eta_state.get(row["flight_id"])
    if st is not None:
        return float(st["eta_p50"])
    others = [flight_plan(r) for r in svc.flights.values() if r["aircraft_id"] != row["aircraft_id"]]
    t0 = svc.clock().timestamp()
    _, p50, _ = await asyncio.to_thread(
        svc.planner.predict_arrival, flight_plan(row), row["vehicle_type"], others, from_node, t0
    )
    return p50


async def _reason(
    svc: Service,
    plan: FlightPlan,
    own_flight_id: str,
    from_node: str,
    t0: float,
    conflict: dict | None,
    gain_s: float,
) -> str:
    """Plain words from the forecast: the named congestion place and window, the named conflicting aircraft."""
    parts: list[str] = []
    cong = congestion_ahead(svc, plan, from_node, t0)
    if cong is not None:
        level, node, p10, p90 = cong
        name = svc.planner.names.get(node, node)
        if level == "heavy":
            parts.append(f"Predicted congestion at {name}, {clock_range(p10, p90)}")
        else:
            parts.append(f"moderate traffic near {name}, {clock_range(p10, p90)}")
    if conflict is not None:
        other = next((f for f in conflict["flights"] if f["flight_id"] != own_flight_id), None)
        if other is not None:
            parts.append(f"possible conflict with {await _callsign(svc, other['aircraft_id'], other['source'])}")
    mins = round(gain_s / 60)
    text = " and ".join(parts)
    if mins >= 1:
        text = f"{text}. Saves {mins} min" if text else f"{mins} min faster on predicted times"
    return (text or "Alternative route on predicted times")[:400]


# --- store and announce ----------------------------------------------------------------------------------------
async def _open_for(svc: Service, flight_id: str) -> dict[str, Any] | None:
    rows = await svc.store.reroutes_where(list(OPEN), flight_id=flight_id)
    return rows[0] if rows else None


def _ops_push(svc: Service, row: dict[str, Any]) -> None:
    if svc.events.hub is not None:
        svc.events.hub.publish_ops("reroute", public(row))


def _pilot_push(svc: Service, row: dict[str, Any]) -> None:
    """The unit's screens hear about approval and about an approved proposal being withdrawn or lapsing."""
    if svc.events.hub is not None:
        svc.events.hub.publish(row["aircraft_id"], "reroute", public(row))


# How a record reads (L-FIX): the status values stay as the API and the screens use them; the words in the text differ.
RECORD_WORD = {"acknowledged": "accepted", "unable": "declined"}


def _simulated_answer(row: dict[str, Any], status: str) -> bool:
    """An accept or decline from a simulated pilot: tagged so it is never read as a real pilot's answer."""
    return row.get("source") == "sim" and status in ANSWERS


def _answerer(row: dict[str, Any], device_id: str) -> str:
    return SIM_PILOT if row.get("source") == "sim" else f"pilot {device_id}"


async def _log(svc: Service, row: dict[str, Any], status: str, by: str, note: str | None = None) -> None:
    word = RECORD_WORD.get(status, status)
    text = f"{by}: reroute {row['id']} {word} ({row['callsign'] or row['aircraft_id']})"
    detail: dict[str, Any] = {"reroute_id": row["id"], "flight_id": row["flight_id"], "status": status, "by": by}
    if _simulated_answer(row, status):
        detail["simulated"] = True
    if note:
        detail["note"] = note
    audit.info("reroute %s (%s, %s) %s by %s%s", row["id"], row["flight_id"], row["aircraft_id"], status, by,
               f" ({note})" if note else "")  # fmt: skip
    await svc.events.record(
        "reroute", aircraft_id=row["aircraft_id"], message=text, detail=detail, acked_by=by[:64]
    )  # acknowledged on write: a routine transition, not an alert


async def _transition(
    svc: Service, row: dict[str, Any], status: str, by: str, note: str | None = None, **values: Any
) -> dict[str, Any]:
    """Store one status change with its event-history entry, log it and push it to ATC."""
    now = svc.clock()
    entry: dict[str, Any] = {"at": now.isoformat(), "kind": status, "by": by}
    if _simulated_answer(row, status):
        entry["simulated"] = True
    if note:
        entry["note"] = note
    values.update(status=status, events=[*row["events"], entry])
    await svc.store.update_reroute(row["id"], **values)
    row.update(values)
    await _log(svc, row, status, by, note)
    _ops_push(svc, row)
    return public(row)


async def _sim_publish(svc: Service, row: dict[str, Any]) -> None:
    """A simulated pilot hears every change to an approved reroute on MQTT, the full Reroute (contract §8)."""
    if row["source"] == "sim":
        await svc._emit([(f"aerolink/sim/{row['aircraft_id']}/reroute", json.dumps(public(row), default=str).encode())])


async def _expire(svc: Service, row: dict[str, Any], why: str) -> dict[str, Any]:
    was_approved = row["status"] == "approved"
    out = await _transition(svc, row, "expired", SYSTEM, note=why, responded_at=svc.clock())
    if was_approved:
        _pilot_push(svc, row)  # the pilot's card must go too
        await _sim_publish(svc, row)  # and a simulated pilot's card (withdrawn: status expired)
    return out


# --- proposing -------------------------------------------------------------------------------------------------
async def _insert(
    svc: Service,
    flight: dict[str, Any],
    from_node: str,
    route: Route,
    *,
    trigger: str,
    conflict: dict | None,
    min_gain_s: float,
) -> dict[str, Any] | None:
    """Create the open proposal for a flight, unless it has one or the route does not beat the current ETA."""
    if await _open_for(svc, flight["flight_id"]) is not None:
        return None
    now, t0 = svc.clock(), svc.clock().timestamp()
    cur_end = await _current_end(svc, flight, from_node)
    gain = cur_end - _route_end(route)
    if gain < min_gain_s:
        return None
    plan = flight_plan(flight)
    path = _route_path(from_node, route)
    legs = requirement_for_path(path, svc.atc_legs)
    last = route.eta[-1]
    row = {
        "id": f"rr_{uuid4().hex[:12]}",
        "flight_id": flight["flight_id"],
        "aircraft_id": flight["aircraft_id"],
        "source": flight.get("source", "aircraft"),
        "callsign": await _callsign(svc, flight["aircraft_id"], flight.get("source", "aircraft")),
        "destination": flight["destination"],
        "status": "proposed",
        "trigger": trigger,
        "reason": await _reason(svc, plan, flight["flight_id"], from_node, t0, conflict, gain),
        "gain_s": round(gain, 1),
        "current": {
            "route_id": flight["route_id"],
            "path": flight["path"],
            "waypoints": [list(w) for w in svc.planner._waypoints(flight["path"])],
            "eta_s": cur_end,
            "atc": {
                "requirement": requirement_for_path(_remaining(flight["path"], from_node), svc.atc_legs)["requirement"]
            },
        },
        "proposed": {
            "route_id": str(route.route_id),
            "request_id": flight["request_id"],
            "path": path,
            "waypoints": [list(w) for w in route.waypoints],
            "eta_s": last.eta_p50.timestamp(),
            "eta_window_s": [last.eta_p10.timestamp(), last.eta_p90.timestamp()],
            "segments": route.model_dump(mode="json")["segments"],
            "atc": {"requirement": legs["requirement"]},
        },
        "created_at": now,
        "expires_at": now + timedelta(seconds=TTL_S),
        "approved_by": None,
        "approved_at": None,
        "responded_at": None,
        "events": [{"at": now.isoformat(), "kind": "proposed", "by": SYSTEM}],
    }
    await svc.store.add_reroute(row)
    await _log(svc, row, "proposed", SYSTEM)
    _ops_push(svc, row)
    return public(row)


async def from_suggestion(svc: Service, flight: dict[str, Any], resp: RouteResponse, from_node: str) -> dict | None:
    """The suggestion engine found a faster route for an active flight: propose it to ATC (decision 1: the pilot keeps
    his own suggestions too, so this changes nothing on the pilot's side)."""
    if flight["status"] != "active" or not resp.routes:
        return None
    trigger = "medical" if flight["vehicle_type"] == "medical" else "faster"
    return await _insert(svc, flight, from_node, resp.routes[0], trigger=trigger, conflict=None, min_gain_s=1e-9)


async def faster_gone(svc: Service, flight_id: str) -> None:
    """The engine no longer finds a better route for this flight: its open suggestion-based proposal is stale."""
    for row in await svc.store.reroutes_where(["proposed"], flight_id=flight_id):
        if row["trigger"] != "conflict":
            await _expire(svc, row, "no longer better on predicted times")


async def _conflict_proposal(svc: Service, f: Flight, conflict: dict, t0: float) -> tuple[dict | None, str]:
    """A reroute of one active flight that keeps clear of the conflict and is no later than its current route.

    Returns the proposal (or None) and the outcome, recorded in the service stats so the reasons are visible.
    """
    row_flight = _flight_row(svc, f.flight_id)
    track = svc._fresh_track(f.aircraft_id)
    if row_flight is None or track is None:
        return None, "no_fresh_fix"
    pos = track.last.position
    from_node = svc.planner.next_node(f.plan.path, pos.lat, pos.lon)
    if from_node == f.plan.destination_node:
        return None, "already_at_destination"
    others = [flight_plan(r) for r in svc.flights.values() if r["aircraft_id"] != f.aircraft_id]
    resp = await asyncio.to_thread(
        svc.planner.alternatives,
        flight=f.plan,
        aircraft_id=f.aircraft_id,
        vehicle_type=cast(VehicleType, f.vehicle_type),
        from_node=from_node,
        planned=others,
        revision=await svc._next_revision(row_flight["request_id"]),
        now=svc.clock(),
    )
    if resp is None or resp.status != "ok":
        return None, "no_route_from_here"
    cur_end = await _current_end(svc, row_flight, from_node)
    usable = [
        r for r in resp.routes
        if _avoids(_route_path(from_node, r), conflict.get("node"), conflict.get("edge"))
        and _route_end(r) <= cur_end
    ]  # fmt: skip
    if not usable:
        return None, "no_route_avoids_it_no_later"
    await svc._remember(resp.model_dump(mode="json"), from_node)  # the pilot can select it: it is a stored option
    trigger = "medical" if f.vehicle_type == "medical" else "conflict"
    row = await _insert(svc, row_flight, from_node, usable[0], trigger=trigger, conflict=conflict, min_gain_s=0.0)
    return (row, "proposed") if row is not None else (None, "already_proposed_or_not_faster")


async def scan_conflicts(svc: Service) -> list[dict]:
    """Propose reroutes for predicted conflicts that a reroute of an active flight avoids (medical first)."""
    t0 = svc.clock().timestamp()
    flights = _live_flights(svc)
    conflicts = await asyncio.to_thread(route_conflicts, flights, t0, CONFLICT_HORIZON_S)
    involved = {ref["flight_id"] for c in conflicts for ref in c["flights"]}
    for row in await svc.store.reroutes_where(["proposed"]):
        if row["trigger"] == "conflict" and row["flight_id"] not in involved:
            await _expire(svc, row, "conflict no longer predicted")
    order = {c["id"]: i for i, c in enumerate(rank(conflicts))}
    by_id = {f.flight_id: f for f in flights}
    made: list[dict] = []
    for c in sorted(conflicts, key=lambda c: (not c["medical"], order[c["id"]])):
        svc.stats[f"reroute_conflict_{c['kind']}"] += 1
        outcome = "no_active_flight"  # only an airborne flight can be rerouted; planned ones are not
        for ref in sorted(c["flights"], key=lambda r: r["vehicle_type"] == "medical"):  # passengers before medical
            f = by_id[ref["flight_id"]]
            if f.status != "active":
                continue
            if await _open_for(svc, f.flight_id) is not None:
                outcome = "already_proposed"  # this flight already has its one proposal
                break
            row, outcome = await _conflict_proposal(svc, f, c, t0)
            if row is not None:
                made.append(row)
                break
        svc.stats[f"reroute_outcome_{outcome}"] += 1
    return made


async def expire_stale(svc: Service) -> list[dict]:
    """Expire open proposals whose time ran out or whose flight ended or changed route."""
    now, out = svc.clock(), []
    for row in await svc.store.reroutes_where(list(OPEN)):
        if (why := _stale_reason(svc, row, now)) is not None:
            out.append(await _expire(svc, row, why))
    return out


# --- ATC decisions and pilot responses -------------------------------------------------------------------------
async def _get(svc: Service, rid: str) -> dict[str, Any]:
    row = await svc.store.get_reroute(rid)
    if row is None:
        raise _err(404, "unknown_reroute", "no such reroute proposal")
    return row


async def approve(svc: Service, rid: str, operator: str) -> dict[str, Any]:
    row = await _get(svc, rid)
    if row["status"] != "proposed":
        raise _err(409, "not_proposed", f"this proposal is {row['status']}")
    now = svc.clock()
    if (why := _stale_reason(svc, row, now)) is not None:
        await _expire(svc, row, why)
        raise _err(409, "stale", f"this proposal has expired: {why}")
    out = await _transition(
        svc, row, "approved", operator, approved_by=operator, approved_at=now, expires_at=now + timedelta(seconds=TTL_S)
    )
    _pilot_push(svc, row)
    await _sim_publish(svc, row)  # the simulated pilot answers on MQTT (contract §8)
    return out


async def dismiss(svc: Service, rid: str, operator: str) -> dict[str, Any]:
    row = await _get(svc, rid)
    if row["status"] == "dismissed":
        return public(row)  # idempotent
    if row["status"] not in OPEN:
        raise _err(409, "not_open", f"this proposal is {row['status']}")
    was = row["status"]
    out = await _transition(svc, row, "dismissed", operator, responded_at=svc.clock())
    if was == "approved":
        _pilot_push(svc, row)  # withdrawn from the pilot's screen
        await _sim_publish(svc, row)  # and from a simulated pilot's (status dismissed)
    return out


async def _for_device(svc: Service, rid: str, device_id: str) -> dict[str, Any]:
    row = await _get(svc, rid)
    if row["aircraft_id"] != device_id:
        raise _err(404, "unknown_reroute", "no such reroute proposal for this unit")
    return row


def flight_status(svc: Service, device_id: str) -> str | None:
    fl = svc.flights.get(device_id)
    return fl["status"] if fl else None


async def acknowledge(svc: Service, rid: str, device_id: str) -> tuple[dict[str, Any], str | None, list[Outbound]]:
    """The pilot accepts the approved reroute: the flight switches to the proposed route, through the same route
    selection a pilot's own choice goes through. Returns the proposal, the flight status and the messages to publish."""
    row = await _for_device(svc, rid, device_id)
    if row["status"] == "acknowledged":
        return public(row), flight_status(svc, device_id), []  # idempotent
    if row["status"] != "approved":
        raise _err(409, "not_approved", f"this proposal is {row['status']}, not approved")
    if (why := _stale_reason(svc, row, svc.clock())) is not None:
        await _expire(svc, row, why)
        raise _err(409, "stale", f"this proposal has expired: {why}")
    msg = RouteSelection(
        schema_version=SCHEMA_VERSION,
        message_id=uuid4(),
        request_id=UUID(row["proposed"]["request_id"]),
        aircraft_id=device_id,
        route_id=UUID(row["proposed"]["route_id"]),
        timestamp=svc.clock(),
    )
    # the proposal's own namespace: a simulated pilot's route selection is recorded as sim (contract §8)
    out = await svc.handle(str(Topic(row["source"], device_id, "route-selection")), msg.model_dump_json().encode())
    planned = next((json.loads(p) for _, p in reversed(out) if json.loads(p).get("status") == "planned"), None)
    if planned is None:
        raise _err(409, "not_switched", "the flight could not switch to the proposed route")
    done = await _transition(svc, row, "acknowledged", _answerer(row, device_id), responded_at=svc.clock())
    return done, planned["status"], out


async def unable(svc: Service, rid: str, device_id: str) -> dict[str, Any]:
    """The pilot does not accept it (PIC authority): the route stays, and ATC is told at once."""
    row = await _for_device(svc, rid, device_id)
    if row["status"] == "unable":
        return public(row)  # idempotent
    if row["status"] != "approved":
        raise _err(409, "not_approved", f"this proposal is {row['status']}, not approved")
    return await _transition(svc, row, "unable", _answerer(row, device_id), responded_at=svc.clock())


async def pending_for(svc: Service, device_id: str) -> dict[str, Any] | None:
    """The unit's approved proposal awaiting its pilot, if any (``pending_reroute`` on the device state)."""
    rows = [r for r in await svc.store.reroutes_where(["approved"]) if r["aircraft_id"] == device_id]
    return public(rows[-1]) if rows else None


async def flight_history(svc: Service, flight_id: str) -> list[dict[str, Any]]:
    """Every proposal of one flight, any status, newest first (ATC's record for that flight)."""
    rows = await svc.store.reroutes_where(None, flight_id=flight_id)
    return [public(r) for r in reversed(rows)]


async def open_list(svc: Service) -> list[dict[str, Any]]:
    """Open proposals for ATC, medical first (the ``state`` message of /ws/ops and ``GET /api/ops/reroutes``)."""
    rows = await svc.store.reroutes_where(list(OPEN))
    rows.sort(key=lambda r: r["trigger"] != "medical")
    return [public(r) for r in rows]


# --- HTTP (operator session; the pilot's endpoints live in pilot.py) ----------------------------------------------
@router.get("/api/ops/reroutes", dependencies=[Depends(require_operator)])
async def list_reroutes(
    request: Request, status: str | None = None, flight_id: str | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Open proposals by default (``status=open``), or a comma list of statuses.

    ``flight_id`` lists every proposal of that flight in any status, newest first, each with its event history (the
    ATC flight detail's record); ``status`` still narrows it when given.
    """
    svc = request.app.state.service
    if flight_id is not None:
        statuses = None
        if status not in (None, ""):
            statuses = [x for x in status.split(",") if x]
            if bad := [x for x in statuses if x not in STATUSES]:
                raise HTTPException(422, f"unknown status {bad[0]!r}; choose from {list(STATUSES)}")
        rows = await svc.store.reroutes_where(statuses, flight_id=flight_id)
        return {"reroutes": [public(r) for r in reversed(rows)]}  # stored oldest first; the record reads newest first
    if status in (None, "", "open"):
        return {"reroutes": await open_list(svc)}
    wanted = [x for x in status.split(",") if x]
    if bad := [x for x in wanted if x not in STATUSES]:
        raise HTTPException(422, f"unknown status {bad[0]!r}; choose from {[*STATUSES, 'open']}")
    rows = await svc.store.reroutes_where(wanted)
    return {"reroutes": [public(r) for r in rows]}


@router.post("/api/ops/reroutes/{rid}/approve")
async def approve_route(request: Request, rid: str, operator: str = Depends(require_operator)) -> dict[str, Any]:
    return await approve(request.app.state.service, rid, operator)


@router.post("/api/ops/reroutes/{rid}/dismiss")
async def dismiss_route(request: Request, rid: str, operator: str = Depends(require_operator)) -> dict[str, Any]:
    return await dismiss(request.app.state.service, rid, operator)
