"""E2: forecasters, classical/AeroLink parity, M5 components live, API, ingest under load (PLAN E6.1, E6.3)."""

from __future__ import annotations

import asyncio
import json
import math
import statistics
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from conftest import portal_of
from fastapi.testclient import TestClient
from test_g2_auth import operator_login
from test_service import Clock, make_service, node, route_request, selection, telemetry

from aerolink.contracts import FlightStatus, RouteResponse
from aerolink.navigation.b1 import B1Advisor
from aerolink.network import Network
from aerolink.offroute.detector import route_polyline
from aerolink.routing.aerolink import ABLATIONS, AeroLinkAdvisor
from aerolink.service.app import create_app
from aerolink.service.models import ModelRegistry
from aerolink.service.planner import Planner, flight_plan, ts
from aerolink.service.settings import Settings
from aerolink.service.store import Store
from aerolink.sim.state import FlightPlan

BACKEND = Path(__file__).resolve().parents[1]
HAVE_GBM = (BACKEND / "models" / "aerolink-gbm" / "model.joblib").exists()
HAVE_OFFROUTE = (BACKEND / "models" / "offroute" / "offroute.joblib").exists()
NOON = datetime(2026, 11, 20, 12, 0, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)


@pytest.fixture(scope="module")
def registry(fl_network: Network) -> ModelRegistry:
    """Models loaded once for the module (never Chronos weights: no network in tests)."""
    reg = ModelRegistry(fl_network, Settings())
    reg.get("cv")
    if HAVE_GBM:
        reg.get("aerolink-gbm")
    reg.load_demand()
    if HAVE_OFFROUTE:
        reg.load_offroute()
    return reg


def planner_for(net: Network, registry: ModelRegistry, forecaster: str, method: str = "aerolink") -> Planner:
    return Planner(net, Settings(forecaster=forecaster, method=method), registry=registry)


def planned_flights(net: Network, t0: float, n: int = 6) -> list[FlightPlan]:
    """A busy picture: n flights into Orlando Executive from different origins, a minute apart."""
    out = []
    origins = [o for o in net.nodes_for("passenger") if o != "ev-orl" and net.is_reachable(o, "ev-orl")][:n]
    for i, o in enumerate(origins):
        path = net.shortest_paths(o, "ev-orl", vehicle_type="passenger", k=1)[0]
        t, etas = t0 + 60.0 * i, {}
        for a, b in zip(path, path[1:], strict=False):
            t += net.edges[(a, b)].length_m / 40.0
            etas[b] = t
        out.append(FlightPlan(uuid4(), uuid4(), uuid4(), path, "ev-orl", t0 + 60.0 * i, etas))
    return out


# --- 1. forecasters, /api/models -------------------------------------------------------------------------
def test_forecaster_switching_never_reloads_weights(registry: ModelRegistry, fl_network: Network):
    p = planner_for(fl_network, registry, "cv")
    names = ["cv", "chronos"] + (["aerolink-gbm"] if HAVE_GBM else [])
    objs = {}
    for name in names:
        p.set_forecaster(name)
        assert p.forecaster.name == name
        objs[name] = p.forecaster
    for name in names:  # a second round hands back the very same objects
        p.set_forecaster(name)
        assert p.forecaster is objs[name]
    with pytest.raises(ValueError):
        p.set_forecaster("nope")


@pytest.mark.skipif(not HAVE_GBM, reason="models/aerolink-gbm not trained")
def test_api_models_and_forecaster_roundtrip(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"
    asyncio.run(_create_db(url))
    with TestClient(create_app(Settings(database_url=url), mqtt=False)) as c:
        operator_login(c)  # PUT /api/forecaster needs an operator session (G2)
        for name in ("aerolink-gbm", "cv", "chronos", "aerolink-gbm"):
            assert c.put("/api/forecaster", json={"forecaster": name}).json() == {"forecaster": name}
            assert c.get("/api/forecaster").json() == {"forecaster": name}
        assert c.put("/api/forecaster", json={"forecaster": "gpt"}).status_code == 422
        models = {m["name"]: m for m in c.get("/api/models").json()["models"]}
        assert set(models) == {"cv", "chronos", "aerolink-gbm"}
        gbm = models["aerolink-gbm"]
        assert gbm["loaded"] and gbm["seed"] == 42 and gbm["load_time_s"] > 0
        assert gbm["sha256"] and gbm["sha256_matches_file"] is True  # manifest hash is the file's hash
        assert gbm["eval"]["position_real_held_out"][0]["horizon_s"] == 30  # last eval scores from eval-results/
        assert "position_sim_held_out" in models["cv"]["eval"]


async def _create_db(url: str) -> None:
    store = Store.connect(url)
    await store.create_all()
    await store.close()


# --- 2. classical and AeroLink parity with the offline engines -------------------------------------------
def option_view(resp: RouteResponse):
    return [
        (
            [r.eta[-1].node],
            r.eta[-1].eta_p50.timestamp(),
            round(r.expected_wait_s, 6),
            round(r.bunching_risk, 9),
            r.cost_terms.mu,
        )
        for r in resp.routes
    ]


@pytest.mark.parametrize("forecaster", ["cv"] + (["aerolink-gbm"] if HAVE_GBM else []))
def test_live_aerolink_options_equal_the_offline_engine(registry: ModelRegistry, fl_network: Network, forecaster: str):
    """Same snapshot (network, planned flights, time) -> the ablations' A5 engine and the live planner agree."""
    t = NOON.timestamp()
    planned = planned_flights(fl_network, t)
    live = planner_for(fl_network, registry, forecaster)
    resp = live.plan(
        request_id=uuid4(), aircraft_id="dev-001", vehicle_type="passenger", origin="ev-lal",
        destination="ev-orl", now=ts(t), planned=planned,
    )  # fmt: skip
    offline = AeroLinkAdvisor(fl_network, registry.get(forecaster), ABLATIONS["A5"], demand=registry.demand)
    offline.start_local_hour = (12.0 - t / 3600.0) % 24.0  # the simulator would bind the scenario's clock; here noon
    want = offline.request_routes("ev-lal", "ev-orl", "passenger", t, planned)
    assert resp.status == "ok" and len(resp.routes) == min(3, len(want))
    for route, o in zip(resp.routes, want, strict=False):
        assert ["ev-lal", *(e.node for e in route.eta)] == o.path
        assert route.eta[-1].eta_p50.timestamp() == pytest.approx(o.eta_s, abs=1e-3)
        assert route.expected_wait_s == pytest.approx(o.predicted_pad_wait_s + o.predicted_junction_wait_s)
        assert route.bunching_risk == pytest.approx(o.bunching_risk)
        assert route.cost_terms.mu == o.cost_terms["mu"] == 600.0  # risk ranking on, as in A5
        assert route.traffic == o.traffic
    assert resp.forecaster == forecaster and resp.method == "aerolink"


def test_live_classical_options_equal_b1_with_the_planned_flights(registry: ModelRegistry, fl_network: Network):
    t = NOON.timestamp()
    planned = planned_flights(fl_network, t)
    live = planner_for(fl_network, registry, "cv", method="classical")
    resp = live.plan(
        request_id=uuid4(), aircraft_id="dev-001", vehicle_type="passenger", origin="ev-lal",
        destination="ev-orl", now=ts(t), planned=planned,
    )  # fmt: skip
    want = B1Advisor(fl_network).request_routes("ev-lal", "ev-orl", "passenger", t, planned=planned)
    assert resp.method == "classical" and resp.forecaster == "none"
    assert [["ev-lal", *(e.node for e in r.eta)] for r in resp.routes] == [o.path for o in want]
    for r, o in zip(resp.routes, want, strict=False):
        assert r.expected_wait_s == pytest.approx(o.predicted_pad_wait_s + o.predicted_junction_wait_s)
        assert r.bunching_risk == 0.0 and r.cost_terms.mu == 0.0
    # and the planned flights really matter: without them the waits differ for a congested destination
    free = B1Advisor(fl_network).request_routes("ev-lal", "ev-orl", "passenger", t)
    assert sum(o.predicted_pad_wait_s + o.predicted_junction_wait_s for o in want) >= sum(
        o.predicted_pad_wait_s + o.predicted_junction_wait_s for o in free
    )


# --- 3. M5 components live -------------------------------------------------------------------------------
def total_arrivals(forecast) -> float:
    return sum(sum(nt.p50_arrivals.values()) for nt in forecast.bins.values())


def test_demand_forecast_adds_expected_flights_to_the_traffic_forecast(registry: ModelRegistry, fl_network: Network):
    if registry.demand is None:
        pytest.skip("synthetic demand history not generated (backend/data/synthetic-demand)")
    t = NOON.timestamp()
    with_demand = planner_for(fl_network, registry, "cv").traffic([], t)
    without = planner_for(fl_network, Settings_no_demand(fl_network), "cv").traffic([], t)
    assert total_arrivals(without) == 0.0 and total_arrivals(with_demand) > 0.0


def Settings_no_demand(net: Network) -> ModelRegistry:
    return ModelRegistry(net, Settings())  # nothing loaded: no demand model


def test_off_route_aircraft_spreads_mass_over_reachable_nodes(registry: ModelRegistry, fl_network: Network):
    p = planner_for(fl_network, ModelRegistry(fl_network, Settings()), "cv")
    t = NOON.timestamp()
    assert total_arrivals(p.traffic([], t)) == 0.0
    lat, lon = node(fl_network, "wp-orlando")
    p.mark_off_route("dev-001", lat, lon, 30.0)
    assert total_arrivals(p.traffic([], t)) == pytest.approx(1.0, abs=0.05)  # one aircraft's worth, spread out
    p.clear_off_route("dev-001")
    assert total_arrivals(p.traffic([], t)) == 0.0


async def one_flight(tmp_path: Path, net: Network, registry: ModelRegistry | None = None, dest: str = "ev-orl"):
    """A service with dev-001 planned from ev-lal to ``dest`` (best option), clock at T0."""
    from test_service import T0

    clock = Clock()
    s = await make_service(tmp_path, net, clock)
    if registry is not None:
        s.planner = Planner(net, Settings(forecaster="cv"), registry=registry)
    await s.store.register_device("dev-001", "passenger", demo=True)
    lat, lon = node(net, "ev-lal")
    base = "aerolink/aircraft/dev-001"
    await s.handle(f"{base}/telemetry", telemetry("dev-001", lat, lon, T0))
    rid = str(uuid4())
    out = await s.handle(f"{base}/route-request", route_request("dev-001", dest, request_id=rid))
    resp = RouteResponse.model_validate_json(out[0][1])
    return s, clock, base, rid, resp


def test_llm_summary_is_a_later_revision_and_never_delays_the_options(tmp_path: Path, fl_network: Network):
    gate = threading.Event()

    def llm(facts) -> str:
        gate.wait(5)
        minutes = facts.lines[0].split(": ")[1].split(" min")[0]
        return f"Fastest option is about {minutes} min."

    async def go():
        s, _, base, rid, resp = await one_flight_llm(tmp_path, fl_network, llm)
        assert resp.revision == 0 and resp.summary and resp.summary.startswith("Recommended route")  # template, at once
        assert s.unsent == []  # the LLM has not answered yet
        gate.set()
        await asyncio.gather(*list(s._bg))
        ((topic, payload),) = s.unsent
        rev = RouteResponse.model_validate_json(payload)
        assert topic == f"{base}/route-response" and rev.revision == 1
        assert rev.summary and rev.summary.startswith("Fastest option is about")
        assert [r.route_id for r in rev.routes] == [r.route_id for r in resp.routes]  # same options, new words
        # the pilot may still select from the original response
        out = await s.handle(f"{base}/route-selection", selection("dev-001", rid, str(resp.routes[0].route_id)))
        assert FlightStatus.model_validate_json(out[-1][1]).status == "planned"
        await s.store.close()

    asyncio.run(go())


def test_llm_summary_failing_the_number_check_is_dropped(tmp_path: Path, fl_network: Network):
    async def go():
        s, *_ = await one_flight_llm(tmp_path, fl_network, lambda facts: "It takes 987 minutes.")
        await asyncio.gather(*list(s._bg))
        assert s.unsent == [] and s.stats["summary_llm_rejected"] == 1
        await s.store.close()

    asyncio.run(go())


async def one_flight_llm(tmp_path: Path, net: Network, llm):
    from test_service import T0

    clock = Clock()
    s = await make_service(tmp_path, net, clock)
    s.planner = Planner(net, Settings(), llm=llm)
    await s.store.register_device("dev-001", "passenger")
    lat, lon = node(net, "ev-lal")
    base = "aerolink/aircraft/dev-001"
    await s.handle(f"{base}/telemetry", telemetry("dev-001", lat, lon, T0))
    rid = str(uuid4())
    out = await s.handle(f"{base}/route-request", route_request("dev-001", "ev-orl", request_id=rid))
    return s, clock, base, rid, RouteResponse.model_validate_json(out[0][1])


def test_no_llm_unless_configured(fl_network: Network):
    assert Planner(fl_network, Settings()).llm is None
    assert Planner(fl_network, Settings(llm_base_url="http://x", llm_model="m")).llm is not None  # never called here


# --- 5. en-route suggestions -----------------------------------------------------------------------------
def test_en_route_suggestion_revises_a_flight_on_a_clearly_worse_route(tmp_path: Path, fl_network: Network):
    async def go():
        from test_service import T0

        s, clock, base, rid, resp = await one_flight(tmp_path, fl_network)
        worst = resp.routes[-1]
        assert worst.est_time_s > 1.3 * resp.routes[0].est_time_s  # the pilot picked a much slower route
        out = await s.handle(f"{base}/route-selection", selection("dev-001", rid, str(worst.route_id)))
        assert FlightStatus.model_validate_json(out[-1][1]).status == "planned"
        lat, lon = node(fl_network, "ev-lal")
        clock.t = T0 + timedelta(seconds=10)
        await s.handle(f"{base}/telemetry", telemetry("dev-001", lat, lon, clock.t, speed=40.0))
        assert s.flights["dev-001"]["status"] == "active"

        out = await s.suggest()
        assert [t for t, _ in out] == [f"{base}/route-response"]
        rev = RouteResponse.model_validate_json(out[0][1])
        assert rev.status == "ok" and rev.revision == 1 and rev.request_id == resp.request_id
        assert rev.routes[0].est_time_s < 0.9 * (worst.est_time_s - 1)  # clearly better than what is left
        assert rev.summary and rev.summary.startswith("Better route found.")
        assert await s.suggest() != [] and s.stats["suggestion_revision_1"] >= 1

        # the pilot can take it: the flight is replaced by one on the new path, from the next node on
        out = await s.handle(f"{base}/route-selection", selection("dev-001", rid, str(rev.routes[0].route_id)))
        assert [FlightStatus.model_validate_json(p).status for _, p in out] == ["replaced", "planned"]
        assert s.flights["dev-001"]["path"][0] == "wp-lakeland"
        assert s.flights["dev-001"]["path"][-1] == "ev-orl"
        await s.store.close()

    asyncio.run(go())


def test_no_suggestion_when_the_route_is_already_best_and_classical_never_suggests(tmp_path: Path, fl_network: Network):
    async def go():
        from test_service import T0

        s, clock, base, rid, resp = await one_flight(tmp_path, fl_network)
        await s.handle(f"{base}/route-selection", selection("dev-001", rid, str(resp.routes[0].route_id)))
        lat, lon = node(fl_network, "ev-lal")
        clock.t = T0 + timedelta(seconds=10)
        await s.handle(f"{base}/telemetry", telemetry("dev-001", lat, lon, clock.t, speed=40.0))
        assert await s.suggest() == []
        # worst route + classical navigation: still nothing (ARCHITECTURE §6.7)
        s.planner.method = "classical"
        assert await s.suggest() == []
        await s.store.close()

    asyncio.run(go())


# --- 3b. off-route detection on live telemetry -----------------------------------------------------------
def fly(net: Network, path: list[str], seconds: int, drift_from: int | None = None, speed: float = 30.0):
    """1 Hz (lat, lon) along ``path`` at ``speed``; from ``drift_from`` s the aircraft veers off sideways."""
    poly = route_polyline(net, path)
    pts = np.array(poly)
    seg = np.hypot(np.diff(pts[:, 0]) * 111_320.0, np.diff(pts[:, 1]) * 111_320.0 * math.cos(math.radians(pts[0, 0])))
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    out = []
    for k in range(seconds):
        d = min(k * speed, cum[-1] - 1)
        lat = float(np.interp(d, cum, pts[:, 0]))
        lon = float(np.interp(d, cum, pts[:, 1]))
        if drift_from is not None and k > drift_from:
            lon += (k - drift_from) * 25.0 / (111_320.0 * math.cos(math.radians(lat)))  # 25 m/s sideways
        out.append((lat, lon))
    return out


@pytest.mark.skipif(not (HAVE_GBM and HAVE_OFFROUTE), reason="aerolink-gbm and off-route models not trained")
def test_off_route_detection_flags_the_flight_and_publishes_fresh_options(
    tmp_path: Path, fl_network: Network, registry: ModelRegistry
):
    async def go(drift: bool):
        from test_service import T0

        s, clock, base, rid, resp = await one_flight(tmp_path, fl_network, registry, dest="ev-orl")
        s.planner.registry.get("aerolink-gbm")
        await s.handle(f"{base}/route-selection", selection("dev-001", rid, str(resp.routes[0].route_id)))
        path = s.flights["dev-001"]["path"]
        track = fly(fl_network, path, 240, drift_from=130 if drift else None)
        flagged_at, published = None, []
        for k, (lat, lon) in enumerate(track, start=1):
            clock.t = T0 + timedelta(seconds=k)
            await s.handle(f"{base}/telemetry", telemetry("dev-001", lat, lon, clock.t, speed=30.0))
            if k % 10 == 0 and k >= 60:
                out = await s.scan_off_route()
                published += out
                if flagged_at is None and s.flights["dev-001"]["status"] == "off_route":
                    flagged_at = k
        await s.store.close()
        return s, flagged_at, published

    s, flagged_at, out = asyncio.run(go(drift=False))
    assert flagged_at is None, "a clean flight must not be flagged"
    s, flagged_at, out = asyncio.run(go(drift=True))
    assert flagged_at is not None and flagged_at > 130, "drifting off the corridor must be flagged after it starts"
    assert s.live.tracks["dev-001"].off_route is True
    statuses = [FlightStatus.model_validate_json(p).status for t, p in out if t.endswith("/flight-status")]
    assert statuses == ["off_route"]
    responses = [RouteResponse.model_validate_json(p) for t, p in out if t.endswith("/route-response")]
    assert len(responses) == 1 and responses[0].status == "ok" and responses[0].revision == 1
    assert responses[0].summary and responses[0].summary.startswith("Off route:")
    assert "dev-001" in s.planner.engine.off_route  # its possible positions now count in the traffic forecast


# --- API -------------------------------------------------------------------------------------------------
def test_traffic_endpoint_has_steps_edges_and_cones(tmp_path: Path, fl_network: Network):
    url = f"sqlite+aiosqlite:///{tmp_path / 't.db'}"
    asyncio.run(_create_db(url))
    app = create_app(Settings(database_url=url), mqtt=False)
    with TestClient(app) as c:
        service = app.state.service
        t0 = service.clock().timestamp()
        for p in planned_flights(fl_network, t0, n=2):
            row = dict(
                flight_id=str(p.flight_id), aircraft_id=f"sim-{len(service.flights):03d}", source="sim",
                request_id=str(p.request_id), route_id=str(p.route_id), vehicle_type="passenger", path=p.path,
                destination=p.destination_node, departure_s=p.departure_time, eta_by_node=p.eta_by_node,
                status="active",
            )  # fmt: skip
            service.flights[row["aircraft_id"]] = row
        portal_of(c).call(service.refresh_forecast)
        body = c.get("/api/traffic").json()
        assert body["steps_min"] == list(range(0, 50, 5))
        node_entry = body["nodes"]["ev-orl"]
        assert len(node_entry["levels"]) == 10 and node_entry["now"] == node_entry["levels"][0]
        assert node_entry["in_15_min"] == node_entry["levels"][3] and node_entry["in_30_min"] == node_entry["levels"][6]
        assert set(body["nodes"]) == set(fl_network.nodes) and len(body["edges"]) == 2 * len(fl_network.graph.edges)
        assert all(len(e["levels"]) == 10 for e in body["edges"].values())
        fc = body["cones"]
        assert fc["type"] == "FeatureCollection" and len(fc["features"]) == 2
        for f in fc["features"]:
            assert (
                f["geometry"]["type"] == "Polygon"
                and f["geometry"]["coordinates"][0][0] == f["geometry"]["coordinates"][0][-1]
            )
            assert {"flight_id", "aircraft_id", "horizon_min"} <= set(f["properties"]) and f["properties"][
                "horizon_min"
            ] == 45
        assert node_entry["in_45_min"] == node_entry["levels"][9]
        cones = body["cone_steps"]
        assert len(cones) == 2 and all(len(k["steps"]) == 10 for k in cones)
        first = cones[0]
        pos = first["steps"][0]
        assert pos["p50"] == pytest.approx(
            [fl_network.nodes[first["path"][0]]["lat"], fl_network.nodes[first["path"][0]]["lon"]], abs=1e-4
        )
        for k in cones:  # progress along the route: at the destination once the planned ETA has passed
            row = service.flights[k["aircraft_id"]]
            end = fl_network.nodes[k["path"][-1]]
            last = k["steps"][-1]["p50"]
            if max(row["eta_by_node"].values()) <= t0 + 2700 - 60:
                assert last == pytest.approx([end["lat"], end["lon"]], abs=1e-4)
            else:
                assert last != k["steps"][0]["p50"]
        for s_ in first["steps"]:
            assert all(len(s_[q]) == 2 for q in ("p10", "p50", "p90"))
        assert len(c.get("/api/traffic").content) < 60_000  # compact


def test_airspace_state_flights_and_scenarios_endpoints(tmp_path: Path, fl_network: Network):
    url = f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"
    asyncio.run(_create_db(url))
    app = create_app(Settings(database_url=url), mqtt=False)
    with TestClient(app) as c:
        air = c.get("/api/airspace")
        geo = air.json()
        nogo = [f for f in geo["features"] if f["properties"].get("layer") == "no_go"]
        assert nogo and all(
            f["properties"]["blocks"] and f["geometry"]["type"] in ("Polygon", "MultiPolygon") for f in nogo
        )
        assert {"type", "floor_m", "ceiling_m"} <= set(nogo[0]["properties"])
        assert len(geo["features"]) > len(nogo) and len(air.content) < 1_500_000
        sc = c.get("/api/scenarios").json()
        assert "nominal_day" in sc["scenarios"] and sc["current"] == "nominal_day"
        ev = c.get("/api/eval").json()
        assert ev["ablations"] == ev["m5_ablations"] and ev["ablations"]["summary"]

        service = app.state.service
        lat, lon = node(fl_network, "ev-sef")
        now = service.clock()
        portal_of(c).call(
            service.handle,
            "aerolink/sim/sim-001/telemetry",
            telemetry("sim-001", lat, lon, now - timedelta(seconds=12)),
        )
        ac = c.get("/api/state").json()["aircraft"][0]
        assert ac["stale"] is True and ac["last_fix_age_s"] >= 12
        portal_of(c).call(service.handle, "aerolink/sim/sim-001/telemetry", telemetry("sim-001", lat, lon, now))
        assert c.get("/api/state").json()["aircraft"][0]["stale"] is False


def test_medical_flight_shows_up_in_other_flights_prediction_history(tmp_path: Path, fl_network: Network):
    """M6 exit criterion: planning a medical flight on shared nodes changes the others' predictions, and says why."""

    async def go():
        s, clock, base, rid, resp = await one_flight(tmp_path, fl_network)
        await s.handle(f"{base}/route-selection", selection("dev-001", rid, str(resp.routes[0].route_id)))
        (before,) = [st for st in s.eta_state.values()]
        assert [h["reason"] for h in before["history"]] == ["initial estimate"]
        # a medical flight planned over the same corridor nodes (its own helicopter, as the sim would report)
        shared = s.flights["dev-001"]["path"]
        path = next(
            [hp, nb, *shared[shared.index(nb) + 1 :]]
            for hp in fl_network.nodes_for("medical")
            for nb in fl_network.graph.neighbors(hp)
            if nb in shared[1:-1]
        )
        body = json.dumps(
            {"schema_version": "4.0", "message_id": str(uuid4()), "aircraft_id": "sim-m00",
             "vehicle_type": "medical", "path": path}
        ).encode()  # fmt: skip
        await s.handle("aerolink/sim/sim-m00/flight-plan", body)
        row = s.flights["dev-001"]
        hist = s.eta_state[row["flight_id"]]["history"]
        assert len(hist) == 2 and "medical flight sim-m00 planned on shared nodes" in hist[-1]["reason"]
        flights = {f["aircraft_id"]: f for f in await s.store.flights()}
        assert flights["dev-001"]["status"] == "planned" and "sim-m00" in flights
        await s.store.close()

    asyncio.run(go())


def test_eval_and_scenario_endpoints(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'e.db'}"
    asyncio.run(_create_db(url))
    with TestClient(create_app(Settings(database_url=url), mqtt=False)) as c:
        operator_login(c)  # PUT /api/scenario needs an operator session (G2)
        ev = c.get("/api/eval").json()
        assert ev["scenarios"] and ev["m5_ablations"]["summary"][0]["config"] == "B0"
        sc = c.get("/api/scenario").json()
        assert "nominal_day" in sc["available"] and sc["scenario"] == "nominal_day"
        assert c.put("/api/scenario", json={"scenario": "../etc/passwd"}).status_code == 422
        assert c.get("/api/method").json() == {"method": "aerolink"}


# --- ingest under load and plan latency ------------------------------------------------------------------
def test_ingest_keeps_up_at_1hz_while_forecasts_run(tmp_path: Path, fl_network: Network, registry: ModelRegistry):
    """11 aircraft x 1 Hz for 60 simulated seconds, with plans and forecast refreshes running in worker threads."""

    async def go():
        from test_service import T0

        clock = Clock()
        s = await make_service(tmp_path, fl_network, clock)
        s.planner = Planner(fl_network, Settings(forecaster="aerolink-gbm" if HAVE_GBM else "cv"), registry=registry)
        starts = [n for n, pr in fl_network.nodes.items() if "lat" in pr][:11]
        planned = planned_flights(fl_network, T0.timestamp())
        lags, tick_times, stop = [], [], asyncio.Event()

        async def heartbeat():  # how long the event loop is blocked
            while not stop.is_set():
                t = time.perf_counter()
                await asyncio.sleep(0.01)
                lags.append(time.perf_counter() - t - 0.01)

        async def inference():  # forecasts and plans running the whole time, in worker threads
            while not stop.is_set():
                await asyncio.gather(
                    s.refresh_forecast(),
                    asyncio.to_thread(
                        s.planner.plan, request_id=uuid4(), aircraft_id="web", vehicle_type="passenger",
                        origin="ev-lal", destination="ev-orl", now=T0, planned=planned,
                    ),
                )  # fmt: skip

        hb, inf = asyncio.create_task(heartbeat()), asyncio.create_task(inference())
        for sec in range(60):
            clock.t = T0 + timedelta(seconds=sec)
            t = time.perf_counter()
            for i, nid in enumerate(starts):
                lat, lon = node(fl_network, nid)
                await s.handle(
                    f"aerolink/sim/sim-{i:03d}/telemetry",
                    telemetry(f"sim-{i:03d}", lat + 0.0003 * sec, lon, clock.t, speed=30.0),
                )
            tick_times.append(time.perf_counter() - t)
        stop.set()
        await asyncio.gather(hb, inf)
        assert s.stats["telemetry_ok"] == 11 * 60 and len(s.live.tracks) == 11
        assert max(tick_times) < 1.0, f"a one-second batch of 11 messages took {max(tick_times):.2f} s: backlog"
        assert statistics.mean(tick_times) < 0.5
        assert max(lags) < 0.5, f"event loop blocked for {max(lags):.2f} s"
        await s.store.close()

    asyncio.run(go())


@pytest.mark.parametrize("forecaster", ["cv", "chronos"] + (["aerolink-gbm"] if HAVE_GBM else []))
def test_plan_latency_p95_under_two_seconds(registry: ModelRegistry, fl_network: Network, forecaster: str, capsys):
    p = planner_for(fl_network, registry, forecaster)
    t = NOON.timestamp()
    planned = planned_flights(fl_network, t, n=10)  # a full simulated fleet in the air
    pairs = [("ev-lal", "ev-orl"), ("ev-orl", "ev-lal"), ("ev-sef", "ev-orl"), ("ev-parramore", "ev-lal")]
    times = []
    for i in range(40):
        o, d = pairs[i % len(pairs)]
        if not fl_network.is_reachable(o, d):
            continue
        t0 = time.perf_counter()
        r = p.plan(request_id=uuid4(), aircraft_id="web", vehicle_type="passenger", origin=o, destination=d,
                   now=ts(t + i), planned=planned)  # fmt: skip
        times.append(time.perf_counter() - t0)
        assert r.status == "ok"
    p95 = float(np.quantile(times, 0.95))
    with capsys.disabled():
        print(f"\n/api/plan planner time, {forecaster}: p50 {np.median(times) * 1000:.0f} ms, p95 {p95 * 1000:.0f} ms")
    assert p95 < 2.0


def test_flight_plan_roundtrip_helper(fl_network: Network):
    row = dict(
        flight_id=str(uuid4()), request_id=str(uuid4()), route_id=str(uuid4()), path=["a", "b"], destination="b",
        departure_s=1.0, eta_by_node={"b": 2.0}, vehicle_type="medical",
    )  # fmt: skip
    assert flight_plan(row).purpose == "medical_dispatch" and json.dumps(row)


def test_put_scenario_publishes_the_control_message_and_clears_simulated_aircraft(tmp_path: Path, fl_network: Network):
    url = f"sqlite+aiosqlite:///{tmp_path / 's.db'}"
    asyncio.run(_create_db(url))
    app = create_app(Settings(database_url=url), mqtt=False)

    class FakeLink:
        connected = True

        def __init__(self):
            self.sent: list[tuple[str, bytes]] = []

        async def publish(self, messages):
            self.sent += messages

    with TestClient(app) as c:
        operator_login(c)  # PUT /api/scenario needs an operator session (G2)
        service, link = app.state.service, FakeLink()
        app.state.link = link
        lat, lon = node(fl_network, "ev-sef")
        from test_service import T0

        portal_of(c).call(service.handle, "aerolink/sim/sim-001/telemetry", telemetry("sim-001", lat, lon, T0))
        assert "sim-001" in service.live.tracks
        r = c.put("/api/scenario", json={"scenario": "medical_dispatch"})
        assert r.status_code == 200 and r.json()["scenario"] == "medical_dispatch"
        assert ("aerolink/sim/control/scenario", b'{"scenario": "medical_dispatch"}') in link.sent
        assert "sim-001" not in service.live.tracks  # gone at once, not when the sim gets round to it
        assert c.get("/api/scenario").json()["scenario"] == "medical_dispatch"


def test_flight_plan_message_is_sim_only(tmp_path: Path, fl_network: Network):
    async def go():
        s = await make_service(tmp_path, fl_network, Clock())
        await s.store.register_device("dev-001", "passenger")
        body = json.dumps(
            {"schema_version": "4.0", "message_id": str(uuid4()), "aircraft_id": "dev-001",
             "vehicle_type": "passenger", "path": ["ev-lal", "wp-lakeland"]}
        ).encode()  # fmt: skip
        assert await s.handle("aerolink/aircraft/dev-001/flight-plan", body) == []  # not a topic devices use
        assert s.flights == {}
        sim_body = body.replace(b"dev-001", b"sim-001")
        out = await s.handle("aerolink/sim/sim-001/flight-plan", sim_body)
        assert FlightStatus.model_validate_json(out[-1][1]).status == "planned"
        assert s.flights["sim-001"]["path"] == ["ev-lal", "wp-lakeland"]
        out = await s.handle("aerolink/sim/sim-001/status", b"offline")  # the aircraft landed: its flight ends
        assert FlightStatus.model_validate_json(out[0][1]).status == "ended" and s.flights == {}
        await s.store.close()

    asyncio.run(go())
