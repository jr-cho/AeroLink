"""U1 story 2-3: AI reroute proposals, ATC decisions and pilot responses (contract §2, UI-AI-PLAN §B revised).

Runs on the committed Central Florida network with the classical planner and cv forecaster (no model files). The
flights are made the real way (telemetry, route request, route selection), so acknowledging a proposal exercises the
same route selection a pilot's own choice goes through.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_service import T0, Clock, route_request, selection, telemetry

from aerolink.contracts import RouteResponse
from aerolink.network import Network
from aerolink.service import reroute
from aerolink.service.core import Service
from aerolink.service.hub import Hub
from aerolink.service.ops_traffic import node_windows
from aerolink.service.planner import Planner, flight_plan
from aerolink.service.reroute import TTL_S, clock_range
from aerolink.service.settings import Settings
from aerolink.service.store import Store
from aerolink.service.topics import Topic
from aerolink.traffic import forecast_traffic

DEV = "dev-001"


class Setup:
    """A service with one unit flying a planned route; ``pick`` returns the two route options (fastest, alternative)."""

    def __init__(self, svc: Service, clock: Clock, origin: str, dest: str, resp: RouteResponse):
        self.svc, self.clock, self.origin, self.dest, self.resp = svc, clock, origin, dest, resp

    @property
    def flight(self) -> dict[str, Any]:
        return self.svc.flights[DEV]

    @property
    def alt(self):
        return self.resp.routes[1] if len(self.resp.routes) > 1 else self.resp.routes[0]


async def build(tmp_path: Path, network: Network) -> Setup:
    clock = Clock()
    store = Store.connect(f"sqlite+aiosqlite:///{tmp_path / 'reroute.db'}")
    await store.create_all()
    svc = Service(store, Planner(network, Settings(method="classical", forecaster="cv")), clock=clock)
    await svc.start()
    await store.register_device(DEV, "passenger", callsign="N999AL")
    await store.register_device("dev-002", "passenger", callsign="N222XY")
    origin = "ev-sef"
    dest = "ev-parramore"  # two route options from ev-sef on the committed network (the alternative tests need two)
    lat, lon = network.nodes[origin]["lat"], network.nodes[origin]["lon"]
    await svc.handle(str(Topic("aircraft", DEV, "telemetry")), telemetry(DEV, lat, lon, T0))
    out = await svc.handle(str(Topic("aircraft", DEV, "route-request")), route_request(DEV, dest))
    resp = RouteResponse.model_validate_json(out[0][1])
    assert resp.status == "ok" and len(resp.routes) == 2, resp.reason
    await svc.handle(
        str(Topic("aircraft", DEV, "route-selection")),
        selection(DEV, str(resp.request_id), str(resp.routes[0].route_id)),
    )
    assert DEV in svc.flights
    return Setup(svc, clock, origin, dest, resp)


async def propose(s: Setup, trigger: str = "faster") -> dict[str, Any]:
    """Create a proposal through the real builder (the gain guard is disabled: these tests are about the lifecycle)."""
    row = await reroute._insert(s.svc, s.flight, s.origin, s.alt, trigger=trigger, conflict=None, min_gain_s=-1e18)
    assert row is not None
    return row


def aio(fn):
    """Run an async test on its own event loop (the repo has no pytest-asyncio; tests use asyncio.run)."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def hub_queues(svc: Service):
    hub = Hub()
    svc.events.hub = hub
    return hub, hub.subscribe_ops(), hub.subscribe(DEV)


def drain(q) -> list[dict[str, Any]]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def code(exc: pytest.ExceptionInfo) -> str:
    return exc.value.detail["error"]


# --- creating -----------------------------------------------------------------------------------------------------
@aio
async def test_proposal_is_created_open_with_the_contract_shape(tmp_path: Path, fl_network: Network):
    s = await build(tmp_path, fl_network)
    hub, ops_q, dev_q = hub_queues(s.svc)
    row = await propose(s)
    assert row["status"] == "proposed" and row["trigger"] == "faster"
    assert row["flight_id"] == s.flight["flight_id"] and row["callsign"] == "N999AL"
    assert row["current"]["route_id"] == s.flight["route_id"]
    prop = row["proposed"]
    assert prop["route_id"] == str(s.alt.route_id)
    assert prop["path"][0] == s.origin and prop["path"][-1] == s.dest
    assert [seg["to"] for seg in prop["segments"]] == prop["path"][1:]
    assert prop["atc"]["requirement"] in ("none", "contact_tower", "contact_approach", "clearance")
    types = [m["type"] for m in drain(ops_q)]
    assert "reroute" in types  # ATC sees the card (the event log also pushes its routine entry as an alert)
    assert drain(dev_q) == []  # the pilot hears nothing until ATC approves


@aio
async def test_one_open_proposal_per_flight(tmp_path: Path, fl_network: Network):
    s = await build(tmp_path, fl_network)
    await propose(s)
    again = await reroute._insert(s.svc, s.flight, s.origin, s.alt, trigger="faster", conflict=None, min_gain_s=-1e18)
    assert again is None


@aio
async def test_gain_guard_refuses_a_route_that_is_not_faster(tmp_path: Path, fl_network: Network):
    s = await build(tmp_path, fl_network)
    row = await reroute._insert(s.svc, s.flight, s.origin, s.alt, trigger="faster", conflict=None, min_gain_s=1e18)
    assert row is None


def test_reason_names_the_congested_place_and_window_from_the_forecast(tmp_path: Path, fl_network: Network):
    async def go() -> None:
        s = await build(tmp_path, fl_network)
        plan = flight_plan(s.flight)
        t0 = s.clock().timestamp()
        node = plan.path[1]
        p10, p50, p90 = node_windows(plan, t0)[node]
        s.svc.forecast = forecast_traffic([], {node: 1}, [(1.0, node, p10, p50, p90)] * 5)  # heavy there then
        own = {"flight_id": str(plan.flight_id), "aircraft_id": DEV, "source": "aircraft"}
        other = {"flight_id": "other-flight", "aircraft_id": "dev-002", "source": "aircraft"}
        text = await reroute._reason(s.svc, plan, str(plan.flight_id), s.origin, t0, {"flights": [own, other]}, 240.0)
        name = s.svc.planner.names[node]
        assert text.startswith(f"Predicted congestion at {name}, {clock_range(p10, p90)}")
        assert "possible conflict with N222XY" in text
        assert text.endswith("Saves 4 min")

    asyncio.run(go())


def test_clock_range_reads_like_the_atc_card():
    from datetime import datetime

    from aerolink.service.planner import FL_TZ

    def at(h: int, m: int) -> float:
        return datetime(2026, 11, 26, h, m, tzinfo=FL_TZ).timestamp()

    assert clock_range(at(19, 42), at(19, 50)) == "7:42 to 7:50 PM"
    assert clock_range(at(11, 58), at(12, 5)) == "11:58 AM to 12:05 PM"
    assert clock_range(at(23, 55), at(0, 3)) == "11:55 PM to 12:03 AM"  # crossing midnight, read as local clock time


# --- ATC decisions -----------------------------------------------------------------------------------------------
@aio
async def test_approve_pushes_to_the_pilot_and_logs_the_decision(tmp_path: Path, fl_network: Network, caplog):
    s = await build(tmp_path, fl_network)
    _, ops_q, dev_q = hub_queues(s.svc)
    row = await propose(s)
    drain(ops_q)
    caplog.set_level(logging.INFO, logger="aerolink.audit")
    out = await reroute.approve(s.svc, row["id"], "joshua")
    assert out["status"] == "approved" and out["approved_by"] == "joshua" and out["approved_at"]
    pushed = drain(dev_q)
    assert [m["type"] for m in pushed] == ["reroute"] and pushed[0]["payload"]["status"] == "approved"
    assert any(m["type"] == "reroute" for m in drain(ops_q))
    assert "approved by joshua" in caplog.text
    events = await s.svc.store.events(kinds=["reroute"], limit=50)
    assert any("approved" in e["message"] for e in events)
    assert [e["kind"] for e in out["events"]] == ["proposed", "approved"]


@aio
async def test_approve_twice_or_after_dismissal_is_refused(tmp_path: Path, fl_network: Network):
    s = await build(tmp_path, fl_network)
    row = await propose(s)
    await reroute.approve(s.svc, row["id"], "joshua")
    with pytest.raises(Exception) as e:
        await reroute.approve(s.svc, row["id"], "joshua")
    assert code(e) == "not_proposed"
    await reroute.dismiss(s.svc, row["id"], "joshua")
    again = await reroute.dismiss(s.svc, row["id"], "joshua")  # idempotent
    assert again["status"] == "dismissed"
    with pytest.raises(Exception) as e2:
        await reroute.approve(s.svc, row["id"], "joshua")
    assert code(e2) == "not_proposed"


@aio
async def test_dismissing_an_approved_proposal_withdraws_it_from_the_pilot(tmp_path: Path, fl_network: Network):
    s = await build(tmp_path, fl_network)
    _, _, dev_q = hub_queues(s.svc)
    row = await propose(s)
    await reroute.approve(s.svc, row["id"], "joshua")
    drain(dev_q)
    await reroute.dismiss(s.svc, row["id"], "joshua")
    withdrawn = drain(dev_q)
    assert withdrawn and withdrawn[-1]["payload"]["status"] == "dismissed"
    assert await reroute.pending_for(s.svc, DEV) is None


# --- pilot responses -------------------------------------------------------------------------------------------------
@aio
async def test_acknowledge_switches_the_flight_through_route_selection_and_is_idempotent(
    tmp_path: Path, fl_network: Network
):
    s = await build(tmp_path, fl_network)
    row = await propose(s)
    await reroute.approve(s.svc, row["id"], "joshua")
    pending = await reroute.pending_for(s.svc, DEV)
    assert pending is not None and pending["id"] == row["id"]
    done, status, out = await reroute.acknowledge(s.svc, row["id"], DEV)
    assert done["status"] == "acknowledged" and status == "planned"
    assert s.flight["route_id"] == str(s.alt.route_id)  # the flight now flies the proposed route
    assert any('"status":"planned"' in p.decode() or '"status": "planned"' in p.decode() for _, p in out)
    assert done["events"][-1]["by"] == f"pilot {DEV}"
    again, _, out2 = await reroute.acknowledge(s.svc, row["id"], DEV)
    assert again["status"] == "acknowledged" and out2 == []  # no second switch
    with pytest.raises(Exception) as e:
        await reroute.unable(s.svc, row["id"], DEV)
    assert code(e) == "not_approved"


@aio
async def test_unable_keeps_the_route_and_tells_atc(tmp_path: Path, fl_network: Network):
    s = await build(tmp_path, fl_network)
    before = s.flight["route_id"]
    row = await propose(s)
    await reroute.approve(s.svc, row["id"], "joshua")
    _, ops_q, _ = hub_queues(s.svc)
    out = await reroute.unable(s.svc, row["id"], DEV)
    assert out["status"] == "unable" and s.flight["route_id"] == before
    assert drain(ops_q)[-1]["payload"]["status"] == "unable"
    assert (await reroute.unable(s.svc, row["id"], DEV))["status"] == "unable"  # idempotent
    with pytest.raises(Exception) as e:
        await reroute.acknowledge(s.svc, row["id"], DEV)
    assert code(e) == "not_approved"


@aio
async def test_pilot_cannot_answer_a_proposal_not_yet_approved_or_another_units(tmp_path: Path, fl_network: Network):
    s = await build(tmp_path, fl_network)
    row = await propose(s)
    with pytest.raises(Exception) as e:
        await reroute.acknowledge(s.svc, row["id"], DEV)
    assert code(e) == "not_approved"
    with pytest.raises(Exception) as e2:
        await reroute.acknowledge(s.svc, row["id"], "dev-002")
    assert code(e2) == "unknown_reroute"


# --- expiry ------------------------------------------------------------------------------------------------------
@aio
async def test_proposal_expires_after_three_minutes_without_a_decision(tmp_path: Path, fl_network: Network):
    s = await build(tmp_path, fl_network)
    row = await propose(s)
    s.clock.t = T0 + timedelta(seconds=TTL_S + 1)
    expired = await reroute.expire_stale(s.svc)
    assert [e["status"] for e in expired] == ["expired"] and expired[0]["id"] == row["id"]
    with pytest.raises(Exception) as e:
        await reroute.approve(s.svc, row["id"], "joshua")
    assert code(e) == "not_proposed"  # the stored state is final


@aio
async def test_approved_proposal_that_the_pilot_never_answers_expires_and_leaves_the_pilot_screen(
    tmp_path: Path, fl_network: Network
):
    s = await build(tmp_path, fl_network)
    row = await propose(s)
    await reroute.approve(s.svc, row["id"], "joshua")
    _, _, dev_q = hub_queues(s.svc)
    s.clock.t = T0 + timedelta(seconds=TTL_S + 5)
    await reroute.expire_stale(s.svc)
    assert drain(dev_q)[-1]["payload"]["status"] == "expired"
    assert await reroute.pending_for(s.svc, DEV) is None


@aio
async def test_route_change_by_the_pilot_expires_the_proposal(tmp_path: Path, fl_network: Network):
    s = await build(tmp_path, fl_network)
    row = await propose(s)
    await s.svc.handle(
        str(Topic("aircraft", DEV, "route-selection")),
        selection(DEV, str(s.resp.request_id), str(s.alt.route_id)),
    )  # the pilot picks the other option: the proposal's "current" route is gone
    assert s.flight["route_id"] != row["current"]["route_id"]
    expired = await reroute.expire_stale(s.svc)
    assert expired and expired[0]["events"][-1]["note"] == "route changed"


@aio
async def test_suggestion_no_longer_better_expires_the_faster_proposal(tmp_path: Path, fl_network: Network):
    s = await build(tmp_path, fl_network)
    await propose(s, trigger="faster")
    await reroute_gone(s)
    rows = await s.svc.store.reroutes_where(["expired"], flight_id=s.flight["flight_id"])
    assert rows and rows[0]["events"][-1]["note"] == "no longer better on predicted times"


async def reroute_gone(s: Setup) -> None:
    await reroute.faster_gone(s.svc, s.flight["flight_id"])


@aio
async def test_pad_conflicts_are_not_rerouted_away(tmp_path: Path, fl_network: Network):
    """Two planned flights from the same origin share its pad: no route avoids it, so ATC gets no proposal."""
    s = await build(tmp_path, fl_network)
    store = s.svc.store
    await store.register_device("dev-003", "passenger")
    lat, lon = fl_network.nodes[s.origin]["lat"], fl_network.nodes[s.origin]["lon"]
    await s.svc.handle(str(Topic("aircraft", "dev-003", "telemetry")), telemetry("dev-003", lat, lon, T0))
    out = await s.svc.handle(str(Topic("aircraft", "dev-003", "route-request")), route_request("dev-003", s.dest))
    resp = RouteResponse.model_validate_json(out[0][1])
    await s.svc.handle(
        str(Topic("aircraft", "dev-003", "route-selection")),
        selection("dev-003", str(resp.request_id), str(resp.routes[0].route_id)),
    )
    from aerolink.service.ops_traffic import route_conflicts
    from aerolink.service.reroute import _live_flights

    assert route_conflicts(_live_flights(s.svc), s.clock().timestamp(), reroute.CONFLICT_HORIZON_S)
    assert await reroute.scan_conflicts(s.svc) == []


# --- HTTP surface (sessions) ----------------------------------------------------------------------------------------
async def _create_tables(url: str) -> None:
    store = Store.connect(url)
    await store.create_all()
    await store.close()


def test_reroute_endpoints_need_their_session(tmp_path: Path):
    from fastapi.testclient import TestClient

    from aerolink.service.app import create_app

    url = f"sqlite+aiosqlite:///{tmp_path / 'http.db'}"
    asyncio.run(_create_tables(url))
    app = create_app(Settings(database_url=url), mqtt=False)
    with TestClient(app) as c:
        assert c.get("/api/ops/reroutes").status_code == 401
        assert c.post("/api/ops/reroutes/rr_x/approve").status_code == 401
        assert c.post("/api/ops/reroutes/rr_x/dismiss").status_code == 401
        assert c.post(f"/api/devices/{DEV}/reroutes/rr_x/acknowledge").status_code == 401
        assert c.post(f"/api/devices/{DEV}/reroutes/rr_x/unable").status_code == 401


def test_reason_falls_back_to_moderate_traffic_when_nothing_is_heavy(tmp_path: Path, fl_network: Network):
    async def go() -> None:
        s = await build(tmp_path, fl_network)
        plan = flight_plan(s.flight)
        t0 = s.clock().timestamp()
        node = plan.path[1]
        p10, p50, p90 = node_windows(plan, t0)[node]
        for n_arrivals in range(1, 6):  # find the forecast that is moderate (not heavy) at this node's window
            s.svc.forecast = forecast_traffic([], {node: 1}, [(1.0, node, p10, p50, p90)] * n_arrivals)
            if s.svc.planner.level(s.svc.forecast, node, p50) == "moderate":
                break
        assert s.svc.planner.level(s.svc.forecast, node, p50) == "moderate"  # precondition: real data, not a stub
        text = await reroute._reason(s.svc, plan, str(plan.flight_id), s.origin, t0, None, 0.0)
        assert text == f"moderate traffic near {s.svc.planner.names[node]}, {clock_range(p10, p90)}"

    asyncio.run(go())


@aio
async def test_list_by_flight_returns_every_status_newest_first_with_events(tmp_path: Path, fl_network: Network):
    s = await build(tmp_path, fl_network)
    first = await propose(s)
    await reroute.dismiss(s.svc, first["id"], "joshua")  # first is now dismissed
    s.clock.t = T0 + timedelta(seconds=60)  # later in time (the test clock is fixed otherwise)
    second = await propose(s)  # a new proposal for the same flight
    rows = await reroute.flight_history(s.svc, s.flight["flight_id"])
    assert [r["id"] for r in rows] == [second["id"], first["id"]]  # newest first, all statuses
    assert [r["status"] for r in rows] == ["proposed", "dismissed"]
    assert rows[1]["events"][-1]["kind"] == "dismissed"  # each carries its history
    assert await reroute.flight_history(s.svc, "no-such-flight") == []
    # the event-log kind of every transition is exactly "reroute"
    kinds = {e["kind"] for e in await s.svc.store.events(kinds=["reroute"], limit=50)}
    assert kinds == {"reroute"}


def test_http_list_by_flight_needs_the_operator_session(tmp_path: Path):
    from fastapi.testclient import TestClient

    from aerolink.service.app import create_app

    url = f"sqlite+aiosqlite:///{tmp_path / 'flight.db'}"
    asyncio.run(_create_tables(url))
    app = create_app(Settings(database_url=url), mqtt=False)
    with TestClient(app) as c:
        assert c.get("/api/ops/reroutes?flight_id=abc").status_code == 401


def test_pilot_acknowledge_while_offline_is_refused_but_a_repeat_is_idempotent(tmp_path: Path):
    """Ruling: the first acknowledgement switches the route, so routing is locked while the unit is offline (409);
    a repeat of an acknowledgement already made is answered 200 with the reroute."""
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from aerolink.service.app import create_app
    from aerolink.service.devices import provision

    url = f"sqlite+aiosqlite:///{tmp_path / 'offline.db'}"

    async def prepare() -> str:
        store = Store.connect(url)
        await store.create_all()
        code = (await provision(store, DEV, "passenger", False, "https://aerolink.test", tmp_path / "labels"))["code"]
        now = datetime.now(UTC)
        base = dict(
            flight_id="f1", aircraft_id=DEV, source="aircraft", callsign="N999AL", destination="ev-parramore",
            trigger="faster", reason="x", gain_s=60.0, current={"route_id": "r0", "path": [], "waypoints": []},
            proposed={"route_id": "r1", "request_id": "q1", "path": [], "waypoints": []}, created_at=now,
            expires_at=now, approved_by="joshua", approved_at=now, responded_at=now, events=[],
        )  # fmt: skip
        await store.add_reroute({**base, "id": "rr_done", "status": "acknowledged"})
        await store.add_reroute({**base, "id": "rr_open", "status": "approved"})
        await store.close()
        return code

    code = asyncio.run(prepare())
    app = create_app(Settings(database_url=url), mqtt=False)
    with TestClient(app, base_url="https://aerolink.test") as c:
        assert c.post(f"/api/devices/{DEV}/pair", json={"code": code}).status_code == 200
        asyncio.run(app.state.service.store.update_device(DEV, online=False))  # the unit is offline
        first = c.post(f"/api/devices/{DEV}/reroutes/rr_open/acknowledge")
        assert first.status_code == 409 and first.json()["detail"]["error"] == "unit_offline"
        repeat = c.post(f"/api/devices/{DEV}/reroutes/rr_done/acknowledge")
        assert repeat.status_code == 200 and repeat.json()["reroute"]["status"] == "acknowledged"
        assert c.post(f"/api/devices/{DEV}/reroutes/rr_done/unable").status_code == 409  # acknowledged, not approved
