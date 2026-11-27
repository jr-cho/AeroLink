"""U1 story 7 (contract §8): simulated pilots follow AeroLink's rules.

A simulated flight is made the sim way (sim-namespace telemetry, route request and selection). ATC approval publishes
the reroute to ``aerolink/sim/{id}/reroute``; the simulated pilot answers on ``aerolink/sim/{id}/reroute-response``
and the backend runs the same acknowledge / unable logic as for a real unit. The disclaimer comes over
``aerolink/sim/{id}/ai-ack``. The same kinds in the aircraft namespace are ignored.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from conftest import need
from test_service import T0, Clock, route_request, selection, telemetry

from aerolink.contracts import RouteResponse
from aerolink.network import Network
from aerolink.service import reroute
from aerolink.service.ai_api import DISCLAIMER_VERSION
from aerolink.service.core import Service
from aerolink.service.planner import Planner
from aerolink.service.reroute import TTL_S
from aerolink.service.settings import Settings
from aerolink.service.store import Store
from aerolink.service.topics import SUBSCRIPTIONS, Topic, parse

SIM = "sim-001"
ORIGIN, DEST = "ev-sef", "ev-parramore"


async def sim_flight(tmp_path: Path, network: Network):
    clock = Clock()
    store = Store.connect(f"sqlite+aiosqlite:///{tmp_path / 'simpilot.db'}")
    await store.create_all()
    svc = Service(store, Planner(network, Settings(method="classical", forecaster="cv")), clock=clock)
    await svc.start()
    lat, lon = network.nodes[ORIGIN]["lat"], network.nodes[ORIGIN]["lon"]
    await svc.handle(str(Topic("sim", SIM, "telemetry")), telemetry(SIM, lat, lon, T0))
    out = await svc.handle(str(Topic("sim", SIM, "route-request")), route_request(SIM, DEST))
    resp = RouteResponse.model_validate_json(out[0][1])
    assert resp.status == "ok" and len(resp.routes) == 2
    await svc.handle(
        str(Topic("sim", SIM, "route-selection")),
        selection(SIM, str(resp.request_id), str(resp.routes[0].route_id)),
    )
    assert svc.flights[SIM]["source"] == "sim"
    return svc


async def approved_proposal(svc: Service) -> dict[str, Any]:
    row = await reroute._insert(
        svc, svc.flights[SIM], ORIGIN, await _alt(svc), trigger="faster", conflict=None, min_gain_s=-1e18
    )
    assert row is not None and row["source"] == "sim"
    return await reroute.approve(svc, row["id"], "joshua")


async def _alt(svc: Service):
    """The other route of the request (the alternative the proposal offers)."""
    stored = need(await svc.store.get_request(svc.flights[SIM]["request_id"]))
    routes = RouteResponse.model_validate_json(json.dumps(stored["response"])).routes
    return routes[1]


def answer(svc: Service, rid: str, response: str, aircraft: str = SIM):
    payload = {"aircraft_id": aircraft, "reroute_id": rid, "response": response, "timestamp": T0.isoformat()}
    return svc.handle(str(Topic("sim", SIM, "reroute-response")), json.dumps(payload).encode())


def aio(fn):
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **k):
        return asyncio.run(fn(*a, **k))

    return wrapper


@aio
async def test_approval_publishes_the_full_reroute_to_the_sim_pilot(tmp_path: Path, fl_network: Network):
    svc = await sim_flight(tmp_path, fl_network)
    out = await approved_proposal(svc)  # no 409 any more for simulated aircraft (contract §8)
    assert out["status"] == "approved"
    sent = [(t, json.loads(p)) for t, p in svc.unsent if t == f"aerolink/sim/{SIM}/reroute"]
    assert len(sent) == 1
    body = sent[0][1]
    assert body["status"] == "approved" and body["id"] == out["id"] and body["source"] == "sim"
    assert body["proposed"]["path"][-1] == DEST and body["proposed"]["waypoints"]


@aio
async def test_sim_acknowledge_switches_the_route_and_records_like_a_pilot(tmp_path: Path, fl_network: Network):
    svc = await sim_flight(tmp_path, fl_network)
    out = await approved_proposal(svc)
    proposed_route = out["proposed"]["route_id"]
    msgs = await answer(svc, out["id"], "acknowledge")
    row = await svc.store.get_reroute(out["id"])
    assert row["status"] == "acknowledged" and row["responded_at"] is not None
    assert svc.flights[SIM]["route_id"] == proposed_route  # the flight now flies the proposed route
    assert any(t == f"aerolink/sim/{SIM}/flight-status" for t, _ in msgs)  # published like any sim status
    assert row["events"][-1]["kind"] == "acknowledged" and row["events"][-1]["by"] == "simulated pilot"  # tagged (B1)
    again = await answer(svc, out["id"], "acknowledge")  # idempotent: no second switch
    assert again == [] and (await svc.store.get_reroute(out["id"]))["status"] == "acknowledged"


@aio
async def test_sim_unable_keeps_the_route_and_is_idempotent(tmp_path: Path, fl_network: Network):
    svc = await sim_flight(tmp_path, fl_network)
    out = await approved_proposal(svc)
    before = svc.flights[SIM]["route_id"]
    assert await answer(svc, out["id"], "unable") == []
    row = await svc.store.get_reroute(out["id"])
    assert row["status"] == "unable" and svc.flights[SIM]["route_id"] == before
    assert await answer(svc, out["id"], "unable") == []  # repeat: same final state
    assert (await svc.store.get_reroute(out["id"]))["status"] == "unable"


@aio
async def test_a_proposal_not_yet_approved_cannot_be_answered(tmp_path: Path, fl_network: Network):
    svc = await sim_flight(tmp_path, fl_network)
    row = await reroute._insert(
        svc, svc.flights[SIM], ORIGIN, await _alt(svc), trigger="faster", conflict=None, min_gain_s=-1e18
    )
    assert row is not None
    assert await answer(svc, row["id"], "acknowledge") == []  # refused: not approved
    assert need(await svc.store.get_reroute(row["id"]))["status"] == "proposed"


@aio
async def test_disclaimer_ack_is_recorded_at_the_current_version(tmp_path: Path, fl_network: Network):
    svc = await sim_flight(tmp_path, fl_network)
    payload = {"aircraft_id": SIM, "version": "current", "timestamp": T0.isoformat()}
    assert await svc.handle(str(Topic("sim", SIM, "ai-ack")), json.dumps(payload).encode()) == []
    rows = await svc.store.events(kinds=["ai_ack"], limit=10)
    assert rows and rows[0]["aircraft_id"] == SIM and rows[0]["detail"]["version"] == DISCLAIMER_VERSION
    assert rows[0]["acked_at"] is not None  # a record, not an alert


@aio
async def test_the_aircraft_namespace_ignores_the_sim_only_kinds(tmp_path: Path, fl_network: Network):
    svc = await sim_flight(tmp_path, fl_network)
    out = await approved_proposal(svc)
    before = (await svc.store.get_reroute(out["id"]))["status"]
    route_before = svc.flights[SIM]["route_id"]
    ack = {"aircraft_id": "dev-001", "version": "current", "timestamp": T0.isoformat()}
    assert await svc.handle(str(Topic("aircraft", "dev-001", "ai-ack")), json.dumps(ack).encode()) == []
    rr = {"aircraft_id": SIM, "reroute_id": out["id"], "response": "acknowledge", "timestamp": T0.isoformat()}
    assert await svc.handle(str(Topic("aircraft", SIM, "reroute-response")), json.dumps(rr).encode()) == []
    assert (await svc.store.get_reroute(out["id"]))["status"] == before == "approved"
    assert svc.flights[SIM]["route_id"] == route_before
    assert await svc.store.events(kinds=["ai_ack"], limit=10) == []
    assert svc.stats["ai-ack_ignored"] == 1 and svc.stats["reroute-response_ignored"] == 1


@aio
async def test_bad_sim_payloads_are_logged_and_ignored(tmp_path: Path, fl_network: Network):
    svc = await sim_flight(tmp_path, fl_network)
    out = await approved_proposal(svc)
    assert await answer(svc, out["id"], "acknowledge", aircraft="sim-999") == []  # the payload names another unit
    assert await answer(svc, out["id"], "maybe") == []  # not a known answer
    assert await svc.handle(str(Topic("sim", SIM, "reroute-response")), b"not json") == []
    assert await svc.handle(str(Topic("sim", SIM, "ai-ack")), b'{"aircraft_id": "sim-999"}') == []
    assert (await svc.store.get_reroute(out["id"]))["status"] == "approved"  # untouched
    assert await svc.store.events(kinds=["ai_ack"], limit=10) == []
    assert svc.stats["reroute_response_rejected"] == 3  # other unit, unknown answer, not JSON
    assert svc.stats["ai_ack_rejected"] == 1


def test_the_new_kinds_are_subscribed_in_the_sim_namespace_only():
    assert "aerolink/sim/+/ai-ack" in SUBSCRIPTIONS
    assert "aerolink/sim/+/reroute-response" in SUBSCRIPTIONS
    assert not any(
        s.startswith("aerolink/aircraft/") and s.endswith(("ai-ack", "reroute-response")) for s in SUBSCRIPTIONS
    )
    topic = need(parse(f"aerolink/sim/{SIM}/reroute"))
    assert topic.kind == "reroute"  # backend -> sim, parsed for completeness


def _sim_reroute_msgs(svc: Service) -> list[dict[str, Any]]:
    return [json.loads(p) for t, p in svc.unsent if t == f"aerolink/sim/{SIM}/reroute"]


@aio
async def test_dismissing_an_approved_sim_reroute_publishes_the_withdrawal(tmp_path: Path, fl_network: Network):
    svc = await sim_flight(tmp_path, fl_network)
    out = await approved_proposal(svc)
    assert [m["status"] for m in _sim_reroute_msgs(svc)] == ["approved"]
    await reroute.dismiss(svc, out["id"], "joshua")
    msgs = _sim_reroute_msgs(svc)
    assert [m["status"] for m in msgs] == ["approved", "dismissed"]  # the simulated pilot drops its pending answer
    assert msgs[-1]["id"] == out["id"]


@aio
async def test_an_approved_sim_reroute_that_lapses_publishes_expired(tmp_path: Path, fl_network: Network):
    svc = await sim_flight(tmp_path, fl_network)
    out = await approved_proposal(svc)
    svc.clock.t = T0 + timedelta(seconds=TTL_S + 60)  # the simulated pilot never answered
    await reroute.expire_stale(svc)
    assert [m["status"] for m in _sim_reroute_msgs(svc)] == ["approved", "expired"]
    assert _sim_reroute_msgs(svc)[-1]["id"] == out["id"]


@aio
async def test_dismissing_a_merely_proposed_sim_reroute_publishes_nothing_to_the_pilot(
    tmp_path: Path, fl_network: Network
):
    svc = await sim_flight(tmp_path, fl_network)
    row = await reroute._insert(
        svc, svc.flights[SIM], ORIGIN, await _alt(svc), trigger="faster", conflict=None, min_gain_s=-1e18
    )
    assert row is not None
    await reroute.dismiss(svc, row["id"], "joshua")
    assert _sim_reroute_msgs(svc) == []  # the pilot never saw a proposal, so there is nothing to withdraw
