"""Operations endpoints (PLAN E6.9): the alert list, acknowledgements and the ``/ws/ops`` push channel.

``GET /api/alerts?since=ISO&kind=a,b&unacked=1&limit=200``  events, newest first::

    {id, at, kind, severity: info|warn|critical, aircraft_id|null, message, detail, acked_at|null, acked_by|null}

``POST /api/alerts/{id}/ack``  acknowledges (first acknowledgement wins; repeating it is harmless) and returns the row.
The operator is the logged-in one (session cookie, ``auth.py``); without a session: 401.
It is audit-logged (logger ``aerolink.audit``) and stored as an ``alert_ack`` event.

Read-only picture for the ATC dashboard (E6.10; calculations in ``ops_traffic.py``). Nothing here sends anything to
an aircraft or changes a flight: ATC acts by radio, the pilot updates the route in AeroLink.

``GET /api/ops/routes``  every planned/active flight (units and simulated)::

    [{flight_id, aircraft_id, callsign, source: "aircraft"|"sim", vehicle_type, status, path, path_names, origin,
      destination, departure_s,
      nodes: [{node, name, eta_p10, eta_p50, eta_p90}] (epoch s; windows as the prediction cones),
      waypoints: [[lat, lon, alt_m]], segments: [{from, to, eta_s, level}] (U-API §1), eta_p10/p50/p90 (destination,
      as /api/flights), prediction_history,
      cone: [{min, p10, p50, p90}] (+0..+45 min, [lat, lon]), bunching_risk|null (route engine), medical,
      atc: {requirement: none|contact_tower|contact_approach|clearance, airspaces, legs, status:
      not_needed|pending|done, action, by, by_name, initials, at} (E6.14, see ``clearance.py``),
      position: {lat, lon, alt_msl_m, ground_speed_mps, course_deg, age_s, online, off_route, stale}|null}]``

``GET /api/ops/conflicts?horizon_min=45``  ``{generated_at, horizon_min, conflicts, groups, bunching, adsb_state}``;
a conflict: ``{id, kind: junction|pad|opposing|adsb, severity: high|medium, medical, t (epoch s), time_to_conflict_s,
node|null, edge|null, overlap_s, gap_s, flights: [{flight_id, aircraft_id, vehicle_type, source}], bunching_risk|null,
locations, adsb? , min_distance_m?, lat?, lon?}``, most urgent first. ``groups``: nodes where 3+ flights overlap.
``bunching``: flights whose route-engine bunching risk is above 5 %, highest first.

``GET /api/ops/whatif/{flight_id}``  the route engine's options from the flight's current position (next node ahead;
origin if not airborne; nearest node if off route) to its destination, never applied::

    {flight_id, aircraft_id, from_node, from_name, method, forecaster,
     current: {path, via, eta_p50, total_s} , alternatives: [{path, via, names, est_time_s, wait_s, total_s,
     time_saved_s (positive = faster than the current route), bunching_risk, traffic, reason}]}

``GET /api/ops/congestion``  per landing site ``{node, name, pads, now, levels[+0..+45], peak, peak_in_min,
arrivals_45, departures_45}`` and the busiest corridor edges.

``WS /ws/ops``  first ``{"type": "state", "payload": {"unacked": [...], "watchdogs": [...]}}``, then ``alert`` (a new
event), ``ack`` (an event acknowledged), ``watchdog`` (a watchdog changed state) and ``heartbeat`` every
WS_HEARTBEAT_SECONDS. Needs an operator session (cookie ``aerolink_ops``): without one the handshake is refused
(close code 4401). Every endpoint in this module, ``/api/alerts*`` included, needs the session (401 otherwise).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect

from aerolink.service import reroute
from aerolink.service.ai import route_segments
from aerolink.service.api import STALE_FIX_S
from aerolink.service.auth import require_operator, ws_operator
from aerolink.service.clearance import atc_state, legs_of, operators_by_name
from aerolink.service.devices import callsign_map, callsign_of
from aerolink.service.events import EVENT_KINDS, public
from aerolink.service.ops_traffic import (
    BUNCHING_SHOWN,
    Flight,
    adsb_conflicts,
    crowded_nodes,
    node_windows,
    rank,
    route_conflicts,
)
from aerolink.service.planner import CONE_STEPS_MIN, flight_plan

router = APIRouter()


@router.get("/api/alerts", dependencies=[Depends(require_operator)])
async def alerts(
    request: Request, since: str | None = None, kind: str | None = None, unacked: int = 0, limit: int = 200
) -> list[dict[str, Any]]:
    when = None
    if since:
        try:
            when = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(422, "since must be an ISO 8601 time") from None
        when = when if when.tzinfo else when.replace(tzinfo=UTC)
    kinds = [k for k in (kind or "").split(",") if k]
    if bad := [k for k in kinds if k not in EVENT_KINDS]:
        raise HTTPException(422, f"unknown kind {bad[0]!r}; choose from {list(EVENT_KINDS)}")
    rows = await request.app.state.service.store.events(when, kinds or None, bool(unacked), max(1, min(limit, 1000)))
    return [public(r) for r in rows]


@router.post("/api/alerts/{alert_id}/ack")
async def ack(request: Request, alert_id: int, operator: str = Depends(require_operator)) -> dict[str, Any]:
    row = await request.app.state.service.events.ack(alert_id, operator)
    if row is None:
        raise HTTPException(404, "unknown alert")
    return row


@router.websocket("/ws/ops")
async def ws_ops(ws: WebSocket) -> None:
    state, hub = ws.app.state, ws.app.state.hub
    if await ws_operator(ws) is None:
        await ws.close(code=4401)  # before accept: the handshake is refused, the page goes to the login screen
        return
    await ws.accept()
    queue = hub.subscribe_ops()  # first, so nothing between the snapshot and the loop is lost
    heartbeat_s = getattr(state, "ws_heartbeat_s", 5.0)
    gone = asyncio.create_task(_until_disconnect(ws))
    try:
        wd = getattr(state, "watchdogs", None)
        rows = await state.service.store.events(unacked=True)
        open_reroutes = await reroute.open_list(state.service)
        await ws.send_json(
            {
                "type": "state",
                "payload": {
                    "unacked": [public(r) for r in rows],
                    "watchdogs": wd.results if wd else [],
                    "reroutes": open_reroutes,  # U-API §2 (additive): the open AI proposals at connect time
                },
            }
        )
        while not gone.done():
            nxt = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({nxt, gone}, timeout=heartbeat_s, return_when=asyncio.FIRST_COMPLETED)
            if nxt in done:
                await ws.send_json(nxt.result())
                continue
            nxt.cancel()
            if not gone.done():
                await ws.send_json({"type": "heartbeat", "payload": {"t": datetime.now(UTC).isoformat()}})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.unsubscribe_ops(queue)
        gone.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await gone


async def _until_disconnect(ws: WebSocket) -> None:
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        while True:
            await ws.receive_text()


# --- the ATC picture (E6.10): read-only ------------------------------------------------------------------
RISK_TTL_S = 5.0
LIVE_STATUSES = ("planned", "active", "off_route")


def _flights(svc) -> list[Flight]:
    return [
        Flight(
            r["flight_id"],
            r["aircraft_id"],
            r["vehicle_type"],
            r.get("source", "aircraft"),
            r["status"],
            flight_plan(r),
        )
        for r in svc.flights.values()
        if r["status"] in LIVE_STATUSES
    ]


def _bunching(planner, flights: list[Flight], t0: float) -> dict[str, float]:
    """The route engine's bunching risk for each flight's remaining route against everyone else's plans."""
    out: dict[str, float] = {}
    with planner.lock:
        eng = planner.engine
        for f in flights:
            others = [g.plan for g in flights if g.aircraft_id != f.aircraft_id]
            try:
                pred = eng.etas(f.plan.path, t0, f.vehicle_type, others)  # type: ignore[arg-type]
                out[f.flight_id] = float(eng._bunching_risk(f.plan.path, pred, others, f.vehicle_type))  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001 - one flight without a prediction must not blank the dashboard
                continue
    return out


async def risks(request: Request, flights: list[Flight], t0: float) -> dict[str, float]:
    """Bunching risks, recomputed at most every RISK_TTL_S (the dashboard polls routes and conflicts together)."""
    state = request.app.state
    cached = getattr(state, "ops_risk", None)
    key = tuple(sorted(f.flight_id for f in flights))
    if cached and cached[1] == key and t0 - cached[0] < RISK_TTL_S:
        return cached[2]
    value = await asyncio.to_thread(_bunching, request.app.state.service.planner, flights, t0)
    state.ops_risk = (t0, key, value)
    return value


def _adsb(request: Request) -> dict[str, Any]:
    feed = getattr(request.app.state, "adsb", None)
    return feed.snapshot() if feed is not None else {"feed": {"state": "disabled"}, "aircraft": []}


@router.get("/api/ops/routes", dependencies=[Depends(require_operator)])
async def ops_routes(request: Request) -> list[dict[str, Any]]:
    s = request.app.state.service
    now = s.clock()
    t0 = now.timestamp()
    flights = _flights(s)
    risk = await risks(request, flights, t0)
    names = await callsign_map(request.app.state)
    marks = await s.store.get_clearances()
    who = await operators_by_name(s.store) if marks else {}
    legs = legs_of(request.app.state)
    # The rows are built off the event loop: cones, segments and levels are synchronous calculations (F4).
    return await asyncio.to_thread(_routes_rows, s, flights, t0, now, risk, names, marks, who, legs)


def _routes_rows(
    s: Any,
    flights: list[Flight],
    t0: float,
    now: datetime,
    risk: dict[str, float],
    names: dict[str, str | None],
    marks: dict[str, Any],
    who: dict[str, str],
    legs: Any,
) -> list[dict[str, Any]]:
    """The ``/api/ops/routes`` rows (synchronous: run in a thread from the handler)."""
    planner = s.planner
    cones = {
        c["flight_id"]: c["steps"]
        for c in planner.cones([(f.aircraft_id, f.status, f.vehicle_type, f.plan) for f in flights], t0)
    }
    out = []
    for f in flights:
        row = s.flights[f.aircraft_id]
        st = s.eta_state.get(f.flight_id)
        dest_eta = row["eta_by_node"].get(row["destination"])
        nodes = []
        for n in f.plan.path:
            w = node_windows(f.plan, t0 - 3600).get(n)
            if w is None:
                continue
            if n == row["destination"] and st is not None:
                w = (st["eta_p10"], st["eta_p50"], st["eta_p90"])
            nodes.append(
                {"node": n, "name": planner.names.get(n, n), "eta_p10": w[0], "eta_p50": w[1], "eta_p90": w[2]}
            )
        track = s.live.tracks.get(f.aircraft_id)
        position = None
        if track is not None:
            m = track.last
            age = max(0.0, (now - m.timestamp).total_seconds())
            position = {
                "lat": m.position.lat, "lon": m.position.lon, "alt_msl_m": m.position.alt_msl_m,
                "ground_speed_mps": m.motion.ground_speed_mps, "course_deg": m.motion.course_deg,
                "age_s": round(age, 1), "online": track.online, "off_route": track.off_route,
                "on_ground": track.on_ground,
                "stale": age > STALE_FIX_S,
            }  # fmt: skip
        out.append(
            {
                "flight_id": f.flight_id,
                "aircraft_id": f.aircraft_id,
                "callsign": callsign_of(f.aircraft_id, f.source, names),
                "source": f.source,
                "vehicle_type": f.vehicle_type,
                "status": f.status,
                "medical": f.vehicle_type == "medical",
                "path": f.plan.path,
                "path_names": [planner.names.get(n, n) for n in f.plan.path],
                "origin": f.plan.path[0],
                "destination": row["destination"],
                "departure_s": row["departure_s"],
                "nodes": nodes,
                "waypoints": planner._waypoints(f.plan.path),
                "eta_p10": st["eta_p10"] if st else dest_eta and dest_eta - 30.0,
                "eta_p50": st["eta_p50"] if st else dest_eta,
                "eta_p90": st["eta_p90"] if st else dest_eta and dest_eta + 30.0,
                "prediction_history": list(st["history"]) if st else [],
                "cone": cones.get(f.flight_id, []),
                "segments": route_segments(
                    f.plan.path, row["eta_by_node"], lambda u, v, t: planner.edge_levels(s.forecast, u, v, t)
                ),
                "bunching_risk": round(risk[f.flight_id], 3) if f.flight_id in risk else None,
                "position": position,
                "detected_by": s.off_route_by.get(f.aircraft_id) if f.status == "off_route" else None,  # contract §7
                "atc": atc_state(f.plan.path, legs, marks.get(f.flight_id), who),
            }
        )
    return out


@router.get("/api/ops/conflicts", dependencies=[Depends(require_operator)])
async def ops_conflicts(request: Request, horizon_min: float = 45.0) -> dict[str, Any]:
    s = request.app.state.service
    t0 = s.clock().timestamp()
    horizon_s = max(5.0, min(horizon_min, 120.0)) * 60.0
    flights = _flights(s)
    risk = await risks(request, flights, t0)
    adsb = _adsb(request)
    found = await asyncio.to_thread(
        lambda: [
            *route_conflicts(flights, t0, horizon_s, risk),
            *adsb_conflicts(s.planner, flights, adsb["aircraft"], t0),
        ]
    )
    names = s.planner.names
    by_id = {f.flight_id: f for f in flights}
    return {
        "generated_at": s.clock().isoformat(),
        "horizon_min": horizon_s / 60.0,
        "conflicts": rank(found),
        "groups": crowded_nodes(flights, t0, horizon_s),
        "bunching": sorted(
            (
                {"flight_id": fid, "aircraft_id": by_id[fid].aircraft_id, "risk": round(r, 3)}
                for fid, r in risk.items()
                if r > BUNCHING_SHOWN and fid in by_id
            ),
            key=lambda b: -float(b["risk"]),
        ),
        "node_names": {n: names.get(n, n) for f in flights for n in f.plan.path},
        "adsb_state": adsb["feed"]["state"],
    }


def _via(path: list[str], names: dict[str, str]) -> str:
    """The waypoints worth saying over the radio: the named sites on the way (else the last few nodes)."""
    return ", then ".join(names.get(n, n) for n in path)


@router.get("/api/ops/whatif/{flight_id}", dependencies=[Depends(require_operator)])
async def ops_whatif(request: Request, flight_id: str) -> dict[str, Any]:
    """Alternatives for ATC to suggest by radio. A pure calculation: the flight and its status stay as they are."""
    s = request.app.state.service
    planner, t_now = s.planner, s.clock()
    row = next((r for r in s.flights.values() if r["flight_id"] == flight_id or r["aircraft_id"] == flight_id), None)
    if row is None:
        raise HTTPException(404, "no such planned or active flight")
    t0 = t_now.timestamp()
    plan = flight_plan(row)
    vt = row["vehicle_type"]
    others = [flight_plan(r) for r in s.flights.values() if r["aircraft_id"] != row["aircraft_id"]]
    track = s._fresh_track(row["aircraft_id"])
    if row["status"] in ("active", "off_route") and track is not None:
        lat, lon = track.last.position.lat, track.last.position.lon
        from_node = (
            planner.next_node(plan.path, lat, lon) if row["status"] == "active" else planner.nearest_node(lat, lon)
        )
    else:
        from_node = plan.path[0]
    names = planner.names
    dest = plan.destination_node

    def compute() -> dict[str, Any]:
        remaining = [from_node, *plan.path[plan.path.index(from_node) + 1 :]] if from_node in plan.path else None
        cur = None
        if remaining and len(remaining) > 1:
            _, p50, _ = planner.predict_arrival(plan, vt, others, from_node, t0)
            cur = {"path": remaining, "via": _via(remaining, names), "eta_p50": p50, "total_s": p50 - t0}
        if from_node == dest:
            return {"current": cur, "alternatives": []}
        alts = []
        for o in planner._options(from_node, dest, vt, t0, others):
            if cur and o.path == cur["path"]:
                continue
            total = (o.eta_s - t0) + o.wait_s
            alts.append(
                {
                    "path": o.path, "via": _via(o.path, names), "names": [names.get(n, n) for n in o.path],
                    "est_time_s": round(o.eta_s - t0, 1), "wait_s": round(o.wait_s, 1), "total_s": round(total, 1),
                    "time_saved_s": round(cur["total_s"] - total, 1) if cur else None,
                    "bunching_risk": round(o.risk, 3), "traffic": o.traffic, "reason": o.reason,
                }
            )  # fmt: skip
        return {"current": cur, "alternatives": alts}

    out = await asyncio.to_thread(compute)
    return {
        "flight_id": row["flight_id"],
        "aircraft_id": row["aircraft_id"],
        "status": row["status"],
        "from_node": from_node,
        "from_name": names.get(from_node, from_node),
        "destination": dest,
        "method": planner.method,
        "forecaster": planner.forecaster.name,
        **out,
    }


@router.get("/api/ops/congestion", dependencies=[Depends(require_operator)])
async def ops_congestion(request: Request) -> dict[str, Any]:
    """Load per landing site and the busiest corridor edges, now to +45 min (from the same forecast as /api/traffic)."""
    s = request.app.state.service
    t0 = s.clock().timestamp()
    # Synchronous forecast and capacity work: run in a thread so the event loop keeps ticking (F4).
    return await asyncio.to_thread(_congestion_rows, s, t0)


def _congestion_rows(s: Any, t0: float) -> dict[str, Any]:
    """The ``/api/ops/congestion`` body (synchronous: run in a thread from the handler)."""
    planner = s.planner
    steps = [t0 + m * 60 for m in CONE_STEPS_MIN]
    order = {"low": 0, "moderate": 1, "heavy": 2}
    caps = planner.b1_capacities()
    flights = _flights(s)
    sites: list[dict[str, Any]] = []
    for nid, cap in caps.items():
        levels = [planner.level(s.forecast, nid, t) for t in steps]
        peak_i = max(range(len(levels)), key=lambda i: (order[levels[i]], -i))
        arr = sum(1 for f in flights if f.plan.destination_node == nid and f.plan.eta_by_node.get(nid, 0) <= t0 + 2700)
        dep = sum(
            1 for f in flights if f.plan.path[0] == nid and f.plan.departure_time <= t0 + 2700 and f.status == "planned"
        )
        sites.append(
            {
                "node": nid, "name": planner.names.get(nid, nid), "pads": cap, "now": levels[0], "levels": levels,
                "peak": levels[peak_i], "peak_in_min": CONE_STEPS_MIN[peak_i], "arrivals_45": arr, "departures_45": dep,
            }
        )  # fmt: skip
    sites.sort(key=lambda r: (-order[r["peak"]], r["peak_in_min"], -r["arrivals_45"], r["node"]))
    edges: list[dict[str, Any]] = []
    for u, v in planner.network.edges:
        levels = [max((planner.level(s.forecast, n, t) for n in (u, v)), key=lambda lv: order[lv]) for t in steps]
        if max(order[x] for x in levels) > 0:
            edges.append(
                {"edge": [u, v], "now": levels[0], "levels": levels, "peak": max(levels, key=lambda lv: order[lv])}
            )
    edges.sort(key=lambda e: (-order[e["peak"]], e["edge"]))
    return {"steps_min": list(CONE_STEPS_MIN), "sites": sites, "edges": edges[:20]}
