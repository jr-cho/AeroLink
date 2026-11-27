"""B1 (contract A and B): the airspace-requirement change on a reroute, the 180 s lifetime, simulated answers tagged.

The proposal builder is the real one (``reroute._insert``) on the committed network, as in ``test_u1_reroute``. The
ATC requirement of a path comes from the clearance feature (``requirement_for_path``); where a test needs two paths
with different requirements it replaces that one function, so the rule being tested is "from / to / equal -> null".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import app_state, need, portal_of
from fastapi.testclient import TestClient
from test_g2_auth import make_app, operator_login
from test_service import T0
from test_u1_reroute import DEV, Setup, aio, build, drain, hub_queues, propose
from test_u1_sim_pilot import SIM, answer, approved_proposal, sim_flight

from aerolink.network import Network
from aerolink.service import reroute
from aerolink.service.reroute import SIM_PILOT, TTL_S, requirement_change

REQS = ("none", "contact_tower", "contact_approach", "clearance")


def _row(before: str | None, after: str) -> dict[str, Any]:
    return {"current": {"atc": {"requirement": before}} if before else {}, "proposed": {"atc": {"requirement": after}}}


# --- A. requirement change ---------------------------------------------------------------------------------------
def test_requirement_change_none_to_clearance_clearance_to_none_and_equal():
    assert requirement_change(_row("none", "clearance")) == {"from": "none", "to": "clearance"}
    assert requirement_change(_row("clearance", "none")) == {"from": "clearance", "to": "none"}
    for r in REQS:
        assert requirement_change(_row(r, r)) is None  # equal: nothing to show
    assert requirement_change(_row(None, "clearance")) is None  # a row stored before the field existed


def _with_requirements(monkeypatch: pytest.MonkeyPatch, s: Setup, current: str, proposed: str) -> None:
    """The clearance rule says ``current`` for the flight's own path and ``proposed`` for any other path."""
    flight_path = list(s.flight["path"])

    def fake(path: list[str], legs: Any) -> dict[str, Any]:
        return {"requirement": current if list(path) == flight_path else proposed, "airspaces": [], "legs": []}

    monkeypatch.setattr(reroute, "requirement_for_path", fake)


@aio
async def test_the_proposal_carries_current_atc_and_the_change_everywhere(
    tmp_path: Path, fl_network: Network, monkeypatch: pytest.MonkeyPatch
):
    s = await build(tmp_path, fl_network)
    _with_requirements(monkeypatch, s, "none", "clearance")
    _, ops_q, dev_q = hub_queues(s.svc)
    row = await propose(s)
    assert row["current"]["atc"] == {"requirement": "none"}  # symmetry with proposed.atc
    assert row["proposed"]["atc"] == {"requirement": "clearance"}
    assert row["requirement_change"] == {"from": "none", "to": "clearance"}
    # REST /api/ops/reroutes and the /ws/ops state message are both built by open_list
    assert (await reroute.open_list(s.svc))[0]["requirement_change"] == {"from": "none", "to": "clearance"}
    pushed = [m["payload"] for m in drain(ops_q) if m["type"] == "reroute"]  # /ws/ops reroute push
    assert pushed and pushed[-1]["requirement_change"] == {"from": "none", "to": "clearance"}
    await reroute.approve(s.svc, row["id"], "joshua")
    device = [m["payload"] for m in drain(dev_q) if m["type"] == "reroute"]  # /ws/devices/{id} reroute push
    assert device[-1]["requirement_change"] == {"from": "none", "to": "clearance"}
    pending = await reroute.pending_for(s.svc, DEV)  # the pilot's pending_reroute
    assert pending is not None and pending["requirement_change"] == {"from": "none", "to": "clearance"}


@aio
async def test_the_reverse_and_the_equal_case_through_the_builder(
    tmp_path: Path, fl_network: Network, monkeypatch: pytest.MonkeyPatch
):
    s = await build(tmp_path, fl_network)
    _with_requirements(monkeypatch, s, "clearance", "none")
    assert (await propose(s))["requirement_change"] == {"from": "clearance", "to": "none"}
    (tmp_path / "b").mkdir()
    s2 = await build(tmp_path / "b", fl_network)
    _with_requirements(monkeypatch, s2, "contact_tower", "contact_tower")
    assert (await propose(s2))["requirement_change"] is None


def test_rest_returns_requirement_change(tmp_path: Path):
    _, app = make_app(tmp_path)
    now = datetime.now(UTC)
    row = dict(
        id="rr_a", flight_id="f1", aircraft_id=DEV, source="aircraft", callsign="N999AL", destination="ev-parramore",
        status="proposed", trigger="faster", reason="x", gain_s=60.0,
        current={"route_id": "r0", "path": [], "waypoints": [], "atc": {"requirement": "contact_tower"}},
        proposed={"route_id": "r1", "request_id": "q1", "path": [], "waypoints": [],
                  "atc": {"requirement": "clearance"}},
        created_at=now, expires_at=now + timedelta(seconds=TTL_S), approved_by=None, approved_at=None,
        responded_at=None, events=[],
    )  # fmt: skip
    with TestClient(app) as c:
        portal_of(c).call(app_state(c).service.store.add_reroute, row)
        operator_login(c)
        got = c.get("/api/ops/reroutes").json()["reroutes"][0]
        assert got["requirement_change"] == {"from": "contact_tower", "to": "clearance"}
        assert got["simulated"] is False
        with c.websocket_connect("/ws/ops") as ws:
            state = ws.receive_json()
        assert state["payload"]["reroutes"][0]["requirement_change"] == {"from": "contact_tower", "to": "clearance"}


# --- B. safeguards ---------------------------------------------------------------------------------------------
def test_the_lifetime_is_180_seconds():
    assert TTL_S == 180.0


@aio
async def test_a_proposal_lapses_at_180_s_by_the_service_clock(tmp_path: Path, fl_network: Network):
    s = await build(tmp_path, fl_network)
    row = await propose(s)
    assert datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["created_at"]) == timedelta(
        seconds=180
    )
    s.clock.t = T0 + timedelta(seconds=179)
    assert await reroute.expire_stale(s.svc) == []  # still open
    s.clock.t = T0 + timedelta(seconds=180)
    assert [r["status"] for r in await reroute.expire_stale(s.svc)] == ["expired"]


@aio
async def test_an_approval_gets_its_own_180_s_for_the_pilot(tmp_path: Path, fl_network: Network):
    s = await build(tmp_path, fl_network)
    row = await propose(s)
    s.clock.t = T0 + timedelta(seconds=100)
    approved = await reroute.approve(s.svc, row["id"], "joshua")
    assert datetime.fromisoformat(approved["expires_at"]) == T0 + timedelta(seconds=280)
    s.clock.t = T0 + timedelta(seconds=279)
    assert await reroute.expire_stale(s.svc) == []
    s.clock.t = T0 + timedelta(seconds=281)
    assert [r["status"] for r in await reroute.expire_stale(s.svc)] == ["expired"]


@aio
async def test_a_simulated_pilots_answers_are_tagged_in_events_and_records(tmp_path: Path, fl_network: Network):
    for response, status in (("acknowledge", "acknowledged"), ("unable", "unable")):
        sub = tmp_path / response
        sub.mkdir()
        svc = await sim_flight(sub, fl_network)
        out = await approved_proposal(svc)
        assert out["simulated"] is True
        await answer(svc, out["id"], response)
        row = need(await svc.store.get_reroute(out["id"]))
        last = row["events"][-1]
        assert last["kind"] == status and last["by"] == SIM_PILOT == "simulated pilot" and last["simulated"] is True
        recs = [e for e in await svc.store.events(kinds=["reroute"], limit=50) if e["detail"].get("status") == status]
        assert recs and recs[0]["detail"]["simulated"] is True and recs[0]["detail"]["by"] == "simulated pilot"
        assert recs[0]["message"].startswith("simulated pilot: reroute")
        assert reroute.public(row)["simulated"] is True
        assert [e["kind"] for e in row["events"] if e.get("simulated")] == [status]  # only the answer is tagged
        published = [json.loads(p) for t, p in svc.unsent if t == f"aerolink/sim/{SIM}/reroute"]
        assert published[-1]["simulated"] is True


@aio
async def test_a_real_aircrafts_answers_are_not_tagged(tmp_path: Path, fl_network: Network):
    for answerer, status in ((reroute.acknowledge, "acknowledged"), (reroute.unable, "unable")):
        sub = tmp_path / status
        sub.mkdir()
        s = await build(sub, fl_network)
        row = await propose(s)
        await reroute.approve(s.svc, row["id"], "joshua")
        await answerer(s.svc, row["id"], DEV)
        stored = need(await s.svc.store.get_reroute(row["id"]))
        last = stored["events"][-1]
        assert last["by"] == f"pilot {DEV}" and "simulated" not in last
        recs = [e for e in await s.svc.store.events(kinds=["reroute"], limit=50) if e["detail"]["status"] == status]
        assert recs and "simulated" not in recs[0]["detail"] and recs[0]["detail"]["by"] == f"pilot {DEV}"
        assert reroute.public(stored)["simulated"] is False


@aio
async def test_a_flight_past_class_b_gets_no_false_change(tmp_path: Path, fl_network: Network, monkeypatch):
    """The current side covers only the path still ahead of the proposal's start node: Class B already behind the
    flight (the first leg) does not count, so rerouting around nothing shows no change."""
    s = await build(tmp_path, fl_network)
    path = list(s.flight["path"])
    assert len(path) > 2
    start = path[1]  # the flight has passed path[0]; the proposal starts here

    def rule(p: list[str], legs: Any) -> dict[str, Any]:
        needs = path[0] in p  # only the stretch before `start` is "Class B"
        return {"requirement": "clearance" if needs else "none", "airspaces": [], "legs": []}

    monkeypatch.setattr(reroute, "requirement_for_path", rule)
    row = await reroute._insert(s.svc, s.flight, start, s.alt, trigger="faster", conflict=None, min_gain_s=-1e18)
    assert row is not None
    assert row["current"]["atc"] == {"requirement": "none"} and row["requirement_change"] is None
