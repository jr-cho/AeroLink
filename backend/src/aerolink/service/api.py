"""HTTP API (ARCHITECTURE §11.1), served behind Caddy at /api. Response shapes (the web map reads these):

``GET /healthz``  ``{ok, mqtt, stats}``
``GET /api/state``  ``{aircraft: [{id, callsign (a unit's registry callsign; SIM-P03 for sim-p03; else the id),
    source: "aircraft"|"sim", vehicle_type, lat, lon, alt_msl_m,
    ground_speed_mps, course_deg, timestamp, online, off_route, flight_status|null,
    last_fix_age_s, stale (no fix for more than 5 s),
    on_ground (parked on a pad: stationary, level and no active flight; not part of the telemetry schema)}], method}``
``GET /api/flights?active=``  planned flights: ``[{flight_id, aircraft_id, source, request_id, route_id, vehicle_type,
    path: [node], destination, departure_s, eta_by_node: {node: unix s}, status, updated_at,
    eta_p10, eta_p50, eta_p90 (epoch s: predicted arrival at the destination, waits included, as last recorded),
    prediction_history: [{t, eta_p50, eta_p90, reason}] (max 20)}]``. An entry is appended when a forecast refresh
    moves the destination ETA by more than 30 s, or when a new flight appears on nodes this flight shares (reason
    then names it, e.g. "medical flight sim-m00 planned on shared nodes"); the first entry is the initial estimate.
``GET /api/airspace``  FeatureCollection: the corridor network (nodes, edges) plus the blocking no-go volumes
    (Polygon/MultiPolygon features of ``constraints-fl.geojson`` with ``blocks`` and ``in_focus_region``; their own
    properties: id, name, type, floor_m, ceiling_m, floor_ft, ceiling_ft, ...; coordinates rounded to 5 decimals)
    marked ``properties.layer == "no_go"``. Built once at startup (~0.2 MB).
``POST /api/plan``  body ``{vehicle_type, destination_node, origin_node? | lat+lon}`` -> a contract ``route-response``
    (never recorded as a flight). Uses the active method and forecaster.
``GET|PUT /api/method``  ``{method: "classical"|"aerolink"}``
``GET|PUT /api/forecaster``  ``{forecaster: "cv"|"chronos"|"aerolink-gbm"}``; 503 if that model cannot be loaded.
``GET /api/models``  ``{default_method, default_forecaster, method, forecaster, models: [{name, version, loaded, seed,
    sha256, load_time_s, loaded_at, error, eval: {...}}]}``
``GET /api/eval``  ``{scenarios: <M4 runs>, ablations: {summary: [...], runs: [...]}|null}`` (``m5_ablations`` is the
    same object under its old name).
``GET /api/traffic``  ``{forecast_time, steps_min: [0, 5, ... 45], nodes: {id: {levels: [low|moderate|heavy x 10],
    by_minute: {"0", "15", "30", "45"}, now, in_15_min, in_30_min, in_45_min}}, edges: {"u->v": same},
    cones: GeoJSON FeatureCollection, cone_steps: [...]}``. An edge is as busy as the busier of its ends.
    ``cones`` has one Polygon per planned/airborne flight, properties ``{flight_id, aircraft_id, status,
    vehicle_type, horizon_min: 45}``: a ribbon along the route through the p50 positions at +0..+45 min, as wide as
    the p10-p90 spread (at least 100 m). ``cone_steps`` is the data behind it: ``[{aircraft_id, flight_id, status,
    vehicle_type, path, steps: [{min, p10: [lat, lon], p50, p90}]}]``; p10/p90 are the least/most progressed
    position along the route (the uncertainty of the planned ETAs).
``GET /api/devices/{id}/traffic``  pilot session; the same body as ``/api/traffic`` (U-API §5).
``GET /api/scenarios``  ``{scenarios: [name...], current: name|null}`` from ``backend/scenarios/*.yaml``.
``GET|PUT /api/scenario``  ``{scenario, available: [...]}``; PUT validates against ``backend/scenarios/*.yaml`` (422).
``GET /api/devices``, ``/api/route-requests/{id}``, ``/api/aircraft/{id}/telemetry``: as before, plus (E6.9, additive)
    per device ``status`` (healthy|degraded|offline), ``status_reasons`` ([no_gnss_fix|weak_signal|health_stale]),
    ``health`` (latest v4.1 health message or null), ``health_age_s``, ``age_s`` (since the unit's last message),
    ``stale`` (no message for 10 s). Telemetry rows carry ``segment_id`` (unix ms; trails never join across it).
``GET /healthz``  ``{ok, mqtt, state: ok|warn|fail|starting, stats}``: cheap; ``?deep=1`` adds ``watchdogs``:
    ``[{name, state, value, threshold, detail, since, checked_at}]``,
    one each for mqtt, forecast, simulator, adsb, database, models.
``GET /api/alerts?since=ISO&kind=a,b&unacked=1&limit=`` events newest first; ``POST /api/alerts/{id}/ack`` (operator
    = the logged-in operator); ``WS /ws/ops`` pushes ``alert``, ``ack``, ``watchdog`` (ops.py).
``POST /api/auth/login|logout``, ``GET /api/auth/me``  operator login (auth.py). An operator session is required on
    ``/api/ops/*``, ``/api/alerts*``, ``/ws/ops`` and ``PUT /api/method|forecaster|scenario``
    (401 ``session_required``).
    ``GET /api/devices`` rows carry the registry ``callsign`` (null for units registered before G2); ``/api/state``
    aircraft carry the resolved one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from aerolink.contracts import NodeId, VehicleType
from aerolink.service.auth import require_operator
from aerolink.service.auth import router as auth_router
from aerolink.service.devices import callsign_map, callsign_of
from aerolink.service.planner import CONE_STEPS_MIN, flight_plan

router = APIRouter()
router.include_router(auth_router)  # /api/auth/* (G2)
STALE_FIX_S = 5.0
log = logging.getLogger("aerolink.api")


def svc(request: Request):
    return request.app.state.service


@router.get("/healthz")
async def healthz(request: Request, deep: int = 0) -> dict:
    """Process alive plus the overall watchdog state (cached). ``deep=1`` re-runs every watchdog and lists them."""
    link, wd = request.app.state.link, getattr(request.app.state, "watchdogs", None)
    out: dict[str, Any] = {
        "ok": True,
        "mqtt": bool(link and link.connected),
        "state": wd.overall() if wd else "starting",
        "stats": dict(svc(request).stats),
    }
    if deep and wd is not None:
        out["watchdogs"] = await wd.evaluate()
        out["state"] = wd.overall()
    return out


@router.get("/api/state")
async def state(request: Request) -> dict:
    s = svc(request)
    out = []
    now = s.clock()
    names = await callsign_map(request.app.state)
    for t in s.live.tracks.values():
        m = t.last
        age = max(0.0, (now - m.timestamp).total_seconds())
        flight = s.flights.get(t.aircraft_id)
        out.append(
            {
                "id": t.aircraft_id,
                "callsign": callsign_of(t.aircraft_id, t.source, names),
                "source": t.source,
                "vehicle_type": m.vehicle_type,
                "lat": m.position.lat,
                "lon": m.position.lon,
                "alt_msl_m": m.position.alt_msl_m,
                "ground_speed_mps": m.motion.ground_speed_mps,
                "course_deg": m.motion.course_deg,
                "timestamp": m.timestamp,
                "online": t.online,
                "off_route": t.off_route,
                "on_ground": t.on_ground,
                "flight_status": flight["status"] if flight else None,
                "last_fix_age_s": round(age, 1),
                "stale": age > STALE_FIX_S,
            }
        )
    return {"aircraft": out, "method": s.planner.method}


@router.get("/api/devices")
async def devices(request: Request) -> list[dict]:
    s = svc(request)
    now = s.clock()
    return [{**d, **s.units.view(d["id"], bool(d["online"]), now)} for d in await s.store.list_devices()]


@router.get("/api/flights")
async def flights(request: Request, active: bool = False) -> list[dict]:
    s = svc(request)
    rows = await s.store.flights(active_only=active)
    for r in rows:
        st = s.eta_state.get(r["flight_id"])
        if st is not None:
            r.update(eta_p10=st["eta_p10"], eta_p50=st["eta_p50"], eta_p90=st["eta_p90"])
            r["prediction_history"] = list(st["history"])
        else:  # ended flights, or none estimated yet: the planned ETA with the forecast's minimum spread
            eta = r["eta_by_node"].get(r["destination"])
            r.update(eta_p10=eta and eta - 30.0, eta_p50=eta, eta_p90=eta and eta + 30.0, prediction_history=[])
    return rows


@router.get("/api/route-requests/{request_id}")
async def route_request(request: Request, request_id: str) -> dict:
    row = await svc(request).store.get_request(request_id)
    if row is None:
        raise HTTPException(404, "unknown request_id")
    return row


@router.get("/api/aircraft/{aircraft_id}/telemetry")
async def telemetry(request: Request, aircraft_id: str, limit: int = 600) -> list[dict]:
    return await svc(request).store.telemetry_history(aircraft_id, min(limit, 3600))


def _round(c: Any) -> Any:
    return [round(c[0], 5), round(c[1], 5)] if isinstance(c[0], int | float) else [_round(x) for x in c]


def build_airspace(settings) -> bytes:
    """Corridor network + the blocking no-go volumes of the focus region, as one GeoJSON document."""
    net = json.loads(Path(settings.network_file).read_text())
    try:
        constraints = json.loads(Path(settings.constraints_file).read_text())["features"]
    except (OSError, ValueError, KeyError):
        constraints = []
    for f in constraints:
        p = f.get("properties", {})
        if p.get("blocks") and p.get("in_focus_region") and f["geometry"]["type"] in ("Polygon", "MultiPolygon"):
            geom = {"type": f["geometry"]["type"], "coordinates": _round(f["geometry"]["coordinates"])}
            net["features"].append({"type": "Feature", "properties": {**p, "layer": "no_go"}, "geometry": geom})
    return json.dumps(net, separators=(",", ":")).encode()


@router.get("/api/airspace")
async def airspace(request: Request) -> Response:
    cached = getattr(request.app.state, "airspace", None)
    if cached is None:
        cached = request.app.state.airspace = await asyncio.to_thread(build_airspace, svc(request).planner.settings)
    return Response(cached, media_type="application/geo+json")


class PlanBody(BaseModel):
    vehicle_type: VehicleType
    destination_node: NodeId
    origin_node: NodeId | None = None
    lat: float | None = None
    lon: float | None = None


@router.post("/api/plan")
async def plan(request: Request, body: PlanBody) -> dict:
    """Route options for the web page. Never recorded as a planned flight."""
    s = svc(request)
    origin = body.origin_node
    if origin is None and body.lat is not None and body.lon is not None:
        origin = s.planner.snap(body.lat, body.lon, body.vehicle_type)
    resp = await asyncio.to_thread(
        s.planner.plan,
        request_id=uuid4(),
        aircraft_id="web",
        vehicle_type=body.vehicle_type,
        origin=origin,
        destination=body.destination_node,
        now=s.clock(),
        planned=[flight_plan(r) for r in s.flights.values()],
    )
    return resp.model_dump(mode="json")


class MethodBody(BaseModel):
    method: Literal["classical", "aerolink"]


@router.get("/api/method")
async def get_method(request: Request) -> dict:
    return {"method": svc(request).planner.method}


@router.put("/api/method")
async def set_method(request: Request, body: MethodBody, _op: str = Depends(require_operator)) -> dict:
    svc(request).planner.method = body.method
    return {"method": body.method}


class ForecasterBody(BaseModel):
    forecaster: Literal["cv", "chronos", "aerolink-gbm"]


@router.get("/api/forecaster")
async def get_forecaster(request: Request) -> dict:
    return {"forecaster": svc(request).planner.forecaster.name}


@router.put("/api/forecaster")
async def set_forecaster(request: Request, body: ForecasterBody, _op: str = Depends(require_operator)) -> dict:
    s = svc(request)
    try:  # a worker thread: loading a model the first time must not block the event loop
        await asyncio.to_thread(s.planner.set_forecaster, body.forecaster)
    except (FileNotFoundError, OSError, ImportError) as e:
        raise HTTPException(503, f"{body.forecaster} cannot be loaded: {e}"[:300]) from e
    s.stats["api_forecaster_set"] += 1
    return {"forecaster": body.forecaster}


@router.get("/api/models")
async def models(request: Request) -> dict:
    s = svc(request)
    return {
        "default_method": s.planner.settings.method,
        "default_forecaster": s.planner.settings.forecaster,
        "method": s.planner.method,
        "forecaster": s.planner.forecaster.name,
        "models": s.planner.loaded_models,
    }


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


@router.get("/api/eval")
async def eval_results(request: Request) -> dict[str, Any]:
    """Offline evaluation: the M4 runs and the M5 ablation table, exactly as the eval scripts wrote them."""
    d = svc(request).planner.settings.eval_dir
    m4, m5 = _read_json(d / "m4-results.json"), _read_json(d / "m5-ablations.json")
    if m4 is None and m5 is None:
        raise HTTPException(404, "eval results not available")
    return {
        "scenarios": m4 if isinstance(m4, list) else (m4 or {}).get("scenarios", []),
        "ablations": m5,
        "m5_ablations": m5,
    }


@router.get("/api/traffic")
async def traffic(request: Request) -> dict[str, Any]:
    """Traffic levels +0..+45 min in 5-minute steps for every node and edge, and prediction cones."""
    return traffic_payload(svc(request))


def traffic_payload(s) -> dict[str, Any]:
    """The ``/api/traffic`` body. Shared with ``GET /api/devices/{id}/traffic`` (pilot phones, U-API §5)."""
    t0 = s.clock().timestamp()
    plan = s.planner
    steps = [t0 + m * 60 for m in CONE_STEPS_MIN]

    def entry(levels: list[str]) -> dict[str, Any]:
        return {
            "levels": levels,  # every 5 min (the web map reads this list)
            "by_minute": {"0": levels[0], "15": levels[3], "30": levels[6], "45": levels[9]},  # U-API §1
            "now": levels[0],
            "in_15_min": levels[3],
            "in_30_min": levels[6],
            "in_45_min": levels[9],
        }

    node_levels = {n: [plan.level(s.forecast, n, t) for t in steps] for n in plan.network.nodes}
    order = {"low": 0, "moderate": 1, "heavy": 2}
    edges = {
        f"{u}->{v}": entry(
            [max(a, b, key=order.__getitem__) for a, b in zip(node_levels[u], node_levels[v], strict=True)]
        )
        for (u, v) in plan.network.edges
    }
    flights = [
        (r["aircraft_id"], r["status"], r["vehicle_type"], flight_plan(r))
        for r in s.flights.values()
        if r["status"] in ("planned", "active", "off_route")
    ]
    cone_steps = plan.cones(flights, t0)
    age = round((s.clock() - s.forecast_at).total_seconds(), 1) if s.forecast_at else None
    return {
        "forecast_time": s.clock().isoformat(),
        "forecast_age_s": age,  # seconds since the forecast loop last refreshed; stale if it fell behind
        "stale": age is None or age > 3 * s.planner.settings.forecast_interval_s,
        "steps_min": list(CONE_STEPS_MIN),
        "nodes": {n: entry(lv) for n, lv in node_levels.items()},
        "edges": edges,
        "cones": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "flight_id": c["flight_id"],
                        "aircraft_id": c["aircraft_id"],
                        "status": c["status"],
                        "vehicle_type": c["vehicle_type"],
                        "horizon_min": CONE_STEPS_MIN[-1],
                    },
                    "geometry": {"type": "Polygon", "coordinates": [plan.cone_polygon(c["steps"])]},
                }
                for c in cone_steps
            ],
        },
        "cone_steps": cone_steps,
    }


@router.get("/api/scenarios")
async def scenarios(request: Request) -> dict:
    cur = svc(request).scenario
    names = scenarios_available(request)
    return {"scenarios": names, "current": cur if cur in names else None}


class ScenarioBody(BaseModel):
    scenario: str


def scenarios_available(request: Request) -> list[str]:
    return sorted(p.stem for p in svc(request).planner.settings.scenarios_dir.glob("*.yaml"))


@router.get("/api/scenario")
async def get_scenario(request: Request) -> dict:
    return {"scenario": svc(request).scenario, "available": scenarios_available(request)}


@router.put("/api/scenario")
async def set_scenario(request: Request, body: ScenarioBody, _op: str = Depends(require_operator)) -> dict:
    """Switch the live simulator: old simulated aircraft vanish now, the sim re-runs and replays the new one."""
    s = svc(request)
    available = scenarios_available(request)
    if body.scenario not in available:
        raise HTTPException(422, f"unknown scenario {body.scenario!r}; choose from {available}")
    link = request.app.state.link
    if link is None:
        raise HTTPException(503, "mqtt link not enabled")
    s.scenario = body.scenario
    gone = await s.reset_sim()
    control = ("aerolink/sim/control/scenario", json.dumps({"scenario": body.scenario}).encode())
    await link.publish([*gone, control])
    s.stats["api_scenario_set"] += 1
    return {"scenario": body.scenario, "available": available}
