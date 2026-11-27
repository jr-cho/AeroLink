"""E6.10: the ATC dashboard's read-only endpoints and the conflict maths behind them.

Pure cases build flight plans on the committed network; API cases seed a ``TestClient`` app's service with planned
flights (the cv forecaster, so no model files are needed).
"""

from __future__ import annotations

import copy
import inspect
import math
import re
import time
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import app_state, portal_of
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from test_g2_auth import operator_login
from test_service import run

from aerolink.contracts import VehicleType
from aerolink.forecast.cv import CVForecaster
from aerolink.network import Network
from aerolink.service import ops, ops_traffic
from aerolink.service.app import create_app
from aerolink.service.auth import require_operator
from aerolink.service.ops_traffic import Flight, adsb_conflicts, crowded_nodes, rank, route_conflicts
from aerolink.service.planner import Planner, flight_plan
from aerolink.service.settings import Settings
from aerolink.service.store import Store
from aerolink.sim.state import FlightPlan

T0 = 1_800_000_000.0


def api_routes(router) -> list[APIRoute]:
    """The router's HTTP routes (``.routes`` is typed as the base class without path/methods/dependant)."""
    return [r for r in router.routes if isinstance(r, APIRoute)]


def plan_on(net: Network, path: list[str], depart: float, vt: VehicleType = "passenger") -> FlightPlan:
    etas, t = {path[0]: depart}, depart
    for a, b in zip(path, path[1:], strict=False):
        t += net.est_time_s([a, b], vt)
        etas[b] = t
    return FlightPlan(
        flight_id=uuid4(), request_id=uuid4(), route_id=uuid4(), path=path, destination_node=path[-1],
        departure_time=depart, eta_by_node=etas, purpose="medical_dispatch" if vt == "medical" else "passenger",
    )  # fmt: skip


def meet(net: Network, path: list[str], node: str, t: float, vt: VehicleType = "passenger") -> FlightPlan:
    """A plan on ``path`` shifted in time so that it reaches ``node`` at ``t``."""
    base = plan_on(net, path, 0.0, vt)
    return plan_on(net, path, t - base.eta_by_node[node], vt)


def flight(aid: str, plan: FlightPlan, vt: VehicleType = "passenger", status: str = "active", source: str = "aircraft"):
    return Flight(str(plan.flight_id), aid, vt, source, status, plan)


# --- pure conflict maths ---------------------------------------------------------------------------------
def test_two_flights_meeting_at_a_junction_are_a_high_conflict(fl_network: Network):
    a = flight("A", meet(fl_network, ["ev-lal", "wp-lakeland", "wp-auburndale", "ev-suntrax"], "wp-lakeland", T0 + 300))
    b = flight("B", meet(fl_network, ["wp-plant-city", "wp-lakeland", "wp-bartow"], "wp-lakeland", T0 + 320))
    found = route_conflicts([a, b], T0, 2700)
    assert len(found) == 1
    c = found[0]
    assert c["kind"] == "junction" and c["node"] == "wp-lakeland" and c["severity"] == "high"
    assert {f["aircraft_id"] for f in c["flights"]} == {"A", "B"} and c["medical"] is False
    assert 280 <= c["time_to_conflict_s"] <= 340 and c["gap_s"] == pytest.approx(20, abs=1)


def test_flights_well_apart_in_time_do_not_conflict(fl_network: Network):
    a = flight("A", meet(fl_network, ["ev-lal", "wp-lakeland", "wp-auburndale"], "wp-lakeland", T0 + 300))
    b = flight("B", meet(fl_network, ["wp-plant-city", "wp-lakeland", "wp-bartow"], "wp-lakeland", T0 + 1500))
    assert route_conflicts([a, b], T0, 2700) == []


def test_horizon_hides_a_conflict_that_is_too_far_ahead(fl_network: Network):
    a = flight("A", meet(fl_network, ["wp-plant-city", "wp-lakeland"], "wp-lakeland", T0 + 1800), status="planned")
    b = flight("B", meet(fl_network, ["wp-bartow", "wp-lakeland"], "wp-lakeland", T0 + 1810), status="planned")
    assert route_conflicts([a, b], T0, 600) == []
    assert len(route_conflicts([a, b], T0, 3600)) == 1


def test_a_shared_landing_site_is_a_pad_conflict_and_medical_is_flagged(fl_network: Network):
    a = flight("A", meet(fl_network, ["hp-fd28", "wp-orlando", "hp-37fa"], "hp-37fa", T0 + 400, "medical"), "medical")
    b = flight("B", meet(fl_network, ["wp-kissimmee", "wp-orlando", "hp-37fa"], "hp-37fa", T0 + 430))
    (c,) = route_conflicts([a, b], T0, 2700)
    assert c["medical"] is True and c["severity"] == "high"
    assert c["kind"] in ("junction", "pad")
    assert any(loc["node"] == "hp-37fa" and loc["kind"] == "pad" for loc in c["locations"])


def test_opposing_flights_on_one_corridor_conflict(fl_network: Network):
    a = flight("A", plan_on(fl_network, ["wp-lakeland", "wp-auburndale"], T0 + 100))
    b = flight("B", plan_on(fl_network, ["wp-auburndale", "wp-lakeland"], T0 + 100))
    kinds = {loc["kind"] for loc in route_conflicts([a, b], T0, 2700)[0]["locations"]}
    assert "opposing" in kinds


def test_three_flights_at_one_node_make_a_group(fl_network: Network):
    fs = [
        flight(n, meet(fl_network, [src, "wp-lakeland", "wp-bartow"], "wp-lakeland", T0 + 300 + d))
        for n, src, d in (("A", "wp-plant-city", 0), ("B", "wp-auburndale", 10), ("C", "ev-lal", 20))
    ]
    groups = crowded_nodes(fs, T0, 2700)
    assert {g["node"]: g["count"] for g in groups}["wp-lakeland"] == 3


def test_ranking_puts_high_before_medium_then_soonest():
    rows = [
        {"id": "a", "severity": "medium", "time_to_conflict_s": 10},
        {"id": "b", "severity": "high", "time_to_conflict_s": 500},
        {"id": "c", "severity": "high", "time_to_conflict_s": 60},
    ]
    assert [r["id"] for r in rank(rows)] == ["c", "b", "a"]


def intercept(lat: float, lon: float, after_s: float, gs_kt: float = 40.0) -> tuple[float, float]:
    """Where an eastbound aircraft starts so that it is at (lat, lon) ``after_s`` seconds from now."""
    return lat, lon - gs_kt * 0.514444 * after_s / (111_320.0 * math.cos(math.radians(lat)))


def adsb(lat: float, lon: float, track: float = 0.0, alt_ft: float | None = 2800.0, gs_kt: float = 90.0, seen=1.0):
    return {"icao": "abc123", "callsign": "N123", "category": "A7", "lat": lat, "lon": lon, "alt_ft": alt_ft,
            "gs_kt": gs_kt, "track_deg": track, "seen_s": seen, "source": "adsb"}  # fmt: skip


def test_adsb_on_a_collision_course_is_flagged_and_others_are_not(fl_network: Network):
    planner = Planner(fl_network, Settings(forecaster="cv"), CVForecaster(fl_network))
    f = flight("A", plan_on(fl_network, ["wp-plant-city", "wp-lakeland", "wp-bartow", "hp-1fl7"], T0), "medical")
    times = Planner._leg_times(f.plan)
    lat, lon, floor_m, ceil_m = ops_traffic.route_state_at(planner, f.plan, times, T0 + 120)
    mid_ft = (floor_m + ceil_m) / 2 / ops_traffic.FT_TO_M
    lat, lon = intercept(lat, lon, 120)
    hit = adsb(lat, lon, 90.0, mid_ft, gs_kt=40)  # sits on the route at +120 s
    (c,) = adsb_conflicts(planner, [f], [hit], T0)
    assert c["kind"] == "adsb" and c["severity"] == "high" and c["medical"] is True and c["time_to_conflict_s"] <= 150
    assert adsb_conflicts(planner, [f], [adsb(lat, lon, 90.0, 20000.0, gs_kt=40)], T0) == []  # far above the band
    assert adsb_conflicts(planner, [f], [adsb(lat + 0.5, lon, 0.0, mid_ft)], T0) == []  # 55 km away, flying away
    assert adsb_conflicts(planner, [f], [adsb(lat, lon, 90.0, mid_ft, seen=300.0)], T0) == []  # stale aircraft
    # no altitude: possibly in the band, so it still counts
    assert len(adsb_conflicts(planner, [f], [adsb(lat, lon, 90.0, None, gs_kt=40)], T0)) == 1


# --- the API ---------------------------------------------------------------------------------------------
def row(
    net: Network,
    aid: str,
    path: list[str],
    depart: float,
    vt: VehicleType = "passenger",
    status="active",
    source="aircraft",
):
    p = plan_on(net, path, depart, vt)
    return dict(
        flight_id=str(p.flight_id), aircraft_id=aid, source=source, request_id=str(p.request_id),
        route_id=str(p.route_id), vehicle_type=vt, path=path, destination=path[-1], departure_s=depart,
        eta_by_node=p.eta_by_node, status=status,
    )  # fmt: skip


@pytest.fixture
def client(tmp_path: Path, fl_network: Network):
    url = f"sqlite+aiosqlite:///{tmp_path / 'ops.db'}"

    async def prepare():
        st = Store.connect(url)
        await st.create_all()
        await st.close()

    run(prepare())
    app = create_app(Settings(database_url=url, forecaster="cv"), mqtt=False)
    with TestClient(app) as c:
        operator_login(c)  # /api/ops/* needs an operator session (G2)
        s = app_state(c).service
        now = time.time()
        p1 = fl_network.shortest_paths("ev-water-st", "ev-suntrax", vehicle_type="passenger", k=1)[0]
        p2 = ["hp-1fl7", "wp-bartow", "wp-lakeland", "wp-auburndale", "ev-suntrax"]
        assert "wp-lakeland" in p1 and "wp-auburndale" in p1, p1
        t_meet = plan_on(fl_network, p1, now).eta_by_node["wp-lakeland"]
        r2 = row(fl_network, "AL-02", p2, 0.0, source="sim")
        shift = t_meet + 25 - r2["eta_by_node"]["wp-lakeland"]
        r2["departure_s"] += shift
        r2["eta_by_node"] = {k: v + shift for k, v in r2["eta_by_node"].items()}
        for r in (
            row(fl_network, "AL-01", p1, now),
            r2,
            row(fl_network, "AL-03", ["hp-fd28", "wp-orlando", "hp-37fa"], now, "medical", "planned", "sim"),
        ):
            s.flights[r["aircraft_id"]] = r
        yield c


def test_routes_lists_every_active_flight_with_windows_and_cones(client: TestClient):
    rows = {r["aircraft_id"]: r for r in client.get("/api/ops/routes").json()}
    assert set(rows) == {"AL-01", "AL-02", "AL-03"}
    r = rows["AL-01"]
    assert r["source"] == "aircraft" and rows["AL-02"]["source"] == "sim" and rows["AL-03"]["medical"] is True
    assert r["path_names"][0].startswith("Water Street") and r["destination"] == "ev-suntrax"
    assert all(n["eta_p10"] <= n["eta_p50"] <= n["eta_p90"] for n in r["nodes"])
    assert len(r["cone"]) == 10 and r["waypoints"] and len(r["waypoints"][0]) == 3
    assert r["eta_p50"] is not None and r["prediction_history"] == [] and r["position"] is None


def test_conflicts_endpoint_finds_the_lakeland_meeting_and_ranks_it(client: TestClient):
    body = client.get("/api/ops/conflicts?horizon_min=60").json()
    assert body["adsb_state"] == "disabled" and body["horizon_min"] == 60
    ids = [{f["aircraft_id"] for f in c["flights"]} for c in body["conflicts"]]
    assert {"AL-01", "AL-02"} in ids
    assert [c["severity"] for c in body["conflicts"]] == sorted(c["severity"] for c in body["conflicts"])  # high first
    assert body["node_names"]["wp-lakeland"] == "Lakeland (I-4)"
    assert client.get("/api/ops/conflicts?horizon_min=0.1").json()["horizon_min"] == 5.0  # clamped


def test_conflicts_include_live_adsb_aircraft(client: TestClient):
    s = app_state(client).service
    f = flight_plan(s.flights["AL-01"])
    planner = s.planner
    times = Planner._leg_times(f)
    lat, lon, floor_m, ceil_m = ops_traffic.route_state_at(planner, f, times, time.time() + 90)
    lat, lon = intercept(lat, lon, 90, 30.0)

    class Feed:
        def snapshot(self):
            return {
                "feed": {"state": "ok"},
                "aircraft": [adsb(lat, lon, 90.0, (floor_m + ceil_m) / 2 / 0.3048, gs_kt=30)],
            }

    app_state(client).adsb = Feed()
    body = client.get("/api/ops/conflicts").json()
    assert body["adsb_state"] == "ok"
    assert any(c["kind"] == "adsb" and c["flights"][0]["aircraft_id"] == "AL-01" for c in body["conflicts"])


def test_whatif_offers_alternatives_and_changes_nothing(client: TestClient):
    s = app_state(client).service
    before = (copy.deepcopy(s.flights), copy.deepcopy(s.eta_state), dict(s.stats), list(s.unsent))
    stored_before = run_store(client, s)
    published: list = []
    s.publish = lambda msgs: published.append(msgs)
    fid = s.flights["AL-01"]["flight_id"]
    body = client.get(f"/api/ops/whatif/{fid}").json()
    assert body["aircraft_id"] == "AL-01" and body["from_node"] == "ev-water-st" and body["destination"] == "ev-suntrax"
    assert body["current"]["path"][-1] == "ev-suntrax"
    assert body["alternatives"], "the network has more than one way from Tampa to SunTrax"
    alt = body["alternatives"][0]
    assert (
        alt["path"][0] == "ev-water-st" and alt["path"][-1] == "ev-suntrax" and alt["path"] != body["current"]["path"]
    )
    assert alt["time_saved_s"] == pytest.approx(body["current"]["total_s"] - alt["total_s"], abs=0.2)
    assert client.get("/api/ops/whatif/AL-01").status_code == 200  # by aircraft id too
    assert client.get("/api/ops/whatif/nope").status_code == 404
    after = (s.flights, s.eta_state, dict(s.stats), list(s.unsent))
    assert after[0] == before[0] and after[1] == before[1] and after[2] == before[2] and after[3] == before[3]
    assert s.flights["AL-01"]["status"] == "active" and s.live.tracks == {} and published == []
    assert run_store(client, s) == stored_before


def run_store(client: TestClient, s):
    return portal_of(client).call(s.store.flights)


def test_congestion_summarises_sites_and_edges(client: TestClient):
    body = client.get("/api/ops/congestion").json()
    assert body["steps_min"][-1] == 45
    sites = {r["node"]: r for r in body["sites"]}
    assert len(sites["hp-37fa"]["levels"]) == 10 and sites["hp-37fa"]["name"].startswith("AdventHealth")
    assert sites["ev-lal"]["pads"] >= 1
    assert sites["hp-37fa"]["arrivals_45"] == 1 and sites["ev-suntrax"]["arrivals_45"] == 2
    assert all("peak" in r for r in body["sites"])


# --- nothing here may reach an aircraft ------------------------------------------------------------------
def test_ops_endpoints_are_read_only_apart_from_the_alert_ack():
    methods = {(r.path, tuple(sorted(r.methods or ()))) for r in api_routes(ops.router)}
    writers = {p for p, m in methods if set(m) - {"GET", "HEAD"}}
    assert writers == {"/api/alerts/{alert_id}/ack"}
    assert {p for p, _ in methods if p.startswith("/api/ops/")} == {
        "/api/ops/routes", "/api/ops/conflicts", "/api/ops/whatif/{flight_id}", "/api/ops/congestion",
    }  # fmt: skip


def test_ops_code_never_publishes_or_changes_a_flight():
    text = inspect.getsource(ops_traffic) + inspect.getsource(ops)
    text = re.sub(r"[A-Za-z]link\.", "", text)  # "AeroLink." in prose
    for forbidden in (
        "publish(", "link.", "mark_off_route", "clear_off_route", "set_flight_status", "add_flight", "recalculate(",
        "_set_status", "handle(", "reset_sim",
    ):  # fmt: skip
        assert forbidden not in text.replace("hub.", ""), forbidden


def test_ops_picture_is_behind_the_operator_login():
    """G2: Caddy no longer asks the operator for the basic-auth password on the ops paths; the app's own login
    (a session cookie checked by the backend on every ops endpoint and on /ws/ops) is the gate instead."""
    caddy = (Path(__file__).resolve().parents[2] / "caddy" / "Caddyfile").read_text()
    exempt = next(line for line in caddy.splitlines() if line.strip().startswith("not path"))
    assert "/api/ops/*" in exempt and "/ws/ops" in exempt and "/api/alerts*" in exempt
    guarded = [r.path for r in api_routes(ops.router) if r.path.startswith(("/api/ops/", "/api/alerts"))]
    assert len(guarded) == 6  # four ops reads, the alert list and the ack
    for r in api_routes(ops.router):
        if r.path in guarded:
            assert require_operator in [d.call for d in r.dependant.dependencies], r.path
