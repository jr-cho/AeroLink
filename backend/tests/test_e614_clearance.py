"""E6.14: the ATC requirement of a route, the controller's "cleared / contacted" mark, and the pilot seeing it.

Pure cases use the committed network's leg flags; API cases run the real app on SQLite (``mqtt=False``) with one
unit paired as a pilot screen and one operator session, both carried in the test client's Cookie header.
"""

from __future__ import annotations

import inspect
import sqlite3
import time
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from conftest import app_state, grab
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from test_e3_pilot_api import Env, Rig
from test_e610_ops import row
from test_g2_auth import operator_login

from aerolink.network import Network
from aerolink.service import auth, clearance, ops, pilot
from aerolink.service.clearance import Legs, atc_state, classify, load_legs, requirement_for_path
from aerolink.service.settings import REPO

BACKEND = Path(__file__).resolve().parents[1]
B_PATH = ["wp-brandon", "wp-downtown-tampa", "wp-i4-i75"]  # Tampa Class B on both legs
D_PATH = ["wp-lakeland", "wp-plant-city"]  # Lakeland Class D
G_PATH = ["ev-sef", "wp-sebring", "wp-avon-park"]  # Class G only (ev-lal now counts: Lakeland Class D)


@pytest.fixture(scope="module")
def legs():
    return load_legs(REPO / "airspace" / "central-fl-v1.geojson")


# --- the requirement (pure) -------------------------------------------------------------------------------
def test_classify_reads_class_and_name_from_the_airspace_id():
    assert classify("faa-class_b-tampa-class-b-4828") == ("clearance", "Tampa Class B")
    assert classify("faa-class_d-orlando-class-d-4918") == ("contact_tower", "Orlando Class D")
    assert classify("faa-class_c-sarasota-class-c-12") == ("contact_approach", "Sarasota Class C")
    assert classify("faa-class_e-somewhere-class-e-1") is None and classify("garbage") is None


def test_class_b_legs_need_clearance_and_class_d_legs_need_the_tower(legs):
    b = requirement_for_path(B_PATH, legs)
    assert b["requirement"] == "clearance" and b["airspaces"] == ["Tampa Class B"]
    assert b["legs"] == [["wp-brandon", "wp-downtown-tampa"], ["wp-downtown-tampa", "wp-i4-i75"]]
    d = requirement_for_path(D_PATH, legs)
    assert d["requirement"] == "contact_tower" and d["airspaces"] == ["Lakeland Class D"]
    assert d["legs"] == [D_PATH]
    assert requirement_for_path(D_PATH[::-1], legs)["requirement"] == "contact_tower"  # either direction


def test_a_route_with_no_flagged_leg_needs_nothing(legs):
    assert requirement_for_path(G_PATH, legs) == {"requirement": "none", "airspaces": [], "legs": []}
    assert requirement_for_path(["ev-lal"], legs)["requirement"] == "none"


def test_the_strongest_leg_wins_and_every_airspace_is_named(legs):
    path = ["ev-lal", "wp-lakeland", "wp-plant-city", "wp-brandon", "wp-downtown-tampa"]
    got = requirement_for_path(path, legs)
    assert got["requirement"] == "clearance"  # B beats D
    assert got["airspaces"] == ["Lakeland Class D", "Tampa Class B"]


def test_class_c_is_contact_approach_and_ranks_between_d_and_b():
    legs: Legs = {
        frozenset(("a", "b")): ("faa-class_d-x-class-d-1",),
        frozenset(("b", "c")): ("faa-class_c-y-class-c-2",),
        frozenset(("c", "d")): ("faa-class_b-z-class-b-3",),
    }
    assert requirement_for_path(["a", "b"], legs)["requirement"] == "contact_tower"
    assert requirement_for_path(["a", "b", "c"], legs)["requirement"] == "contact_approach"
    assert requirement_for_path(["a", "b", "c", "d"], legs)["requirement"] == "clearance"


def test_a_mark_counts_only_for_the_requirement_it_was_made_for(legs):
    from datetime import UTC, datetime

    mark = {"requirement": "clearance", "status": "cleared", "operator": "op", "at": datetime.now(UTC)}
    done = atc_state(B_PATH, legs, mark, {"op": "Jo Smith"})
    assert done["status"] == "done" and done["action"] == "cleared" and done["initials"] == "JS"
    assert atc_state(D_PATH, legs, mark)["status"] == "pending"  # rerouted into another requirement: starts again
    assert atc_state(G_PATH, legs, mark)["status"] == "not_needed"


# --- API ----------------------------------------------------------------------------------------------------
class Atc:
    """An app with a paired pilot screen (dev-001) and an operator, plus helpers."""

    def __init__(self, env: Env, c: TestClient, network: Network):
        self.env, self.c, self.network = env, c, network
        self.rig = Rig(c, network)
        op_token = operator_login(c)
        paired = c.post("/api/devices/dev-001/pair", json={"code": env.codes["dev-001"]})
        pilot_token = grab(r"aerolink_session_dev-001=([^;]+)", paired.headers["set-cookie"])
        self.cookie = f"{auth.COOKIE}={op_token}; aerolink_session_dev-001={pilot_token}"
        c.headers["cookie"] = self.cookie
        c.cookies.clear()
        self.svc = app_state(c).service
        self.rig.online("dev-001")

    def fly(self, aircraft: str, path: list[str], status: str = "active") -> dict:
        r = row(self.network, aircraft, path, time.time(), status=status)
        self.svc.flights[aircraft] = r
        return r

    def mark(self, flight: str, action: str):
        return self.c.post(f"/api/ops/flights/{flight}/clearance", json={"action": action})

    def routes(self) -> dict[str, dict]:
        return {r["aircraft_id"]: r for r in self.c.get("/api/ops/routes").json()}


@pytest.fixture
def atc(tmp_path: Path, fl_network: Network):
    env = Env(tmp_path)
    with TestClient(env.app(), base_url="https://testserver") as c:
        yield Atc(env, c, fl_network)


def test_routes_carry_the_requirement_for_each_flight(atc: Atc):
    atc.fly("dev-001", B_PATH)
    atc.fly("AL-02", D_PATH, "planned")
    atc.fly("AL-03", G_PATH)
    rows = atc.routes()
    assert rows["dev-001"]["atc"]["requirement"] == "clearance" and rows["dev-001"]["atc"]["status"] == "pending"
    assert rows["AL-02"]["atc"]["requirement"] == "contact_tower" and rows["AL-02"]["atc"]["airspaces"]
    assert rows["AL-03"]["atc"]["status"] == "not_needed"


def test_marking_cleared_is_recorded_idempotent_and_audited(atc: Atc, caplog):
    r = atc.fly("dev-001", B_PATH)
    caplog.set_level("INFO", logger="aerolink.audit")
    first = atc.mark(r["flight_id"], "cleared")
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "done" and body["action"] == "cleared" and body["by"] == "op" and body["initials"] == "O"
    again = atc.mark("dev-001", "cleared")  # by aircraft id, repeated
    assert again.json()["at"] == body["at"]
    assert atc.routes()["dev-001"]["atc"]["status"] == "done"
    events = [e for e in atc.c.get("/api/alerts?kind=clearance").json()]
    assert len(events) == 1 and events[0]["acked_by"] == "op" and events[0]["detail"]["action"] == "cleared"
    assert sum("ATC clearance obtained by op" in m for m in caplog.messages) == 1
    undone = atc.mark("dev-001", "revoke").json()
    assert undone["status"] == "pending" and undone["by"] is None
    assert atc.mark("dev-001", "revoke").json()["status"] == "pending"  # harmless twice
    assert len(atc.c.get("/api/alerts?kind=clearance").json()) == 2  # cleared, then revoked


def test_the_action_has_to_match_the_requirement(atc: Atc):
    atc.fly("dev-001", B_PATH)
    atc.fly("AL-02", D_PATH)
    atc.fly("AL-03", G_PATH)
    r = atc.mark("dev-001", "contacted")
    assert r.status_code == 422 and r.json()["detail"]["error"] == "wrong_action"
    r = atc.mark("AL-02", "cleared")
    assert r.status_code == 422
    assert atc.mark("AL-02", "contacted").json()["action"] == "contacted"
    r = atc.mark("AL-03", "cleared")
    assert r.status_code == 409 and r.json()["detail"]["error"] == "not_needed"
    assert atc.mark("dev-001", "approve").status_code == 422
    assert atc.mark("nobody", "cleared").status_code == 404
    atc.svc.flights["dev-001"]["status"] = "ended"
    assert atc.mark("dev-001", "cleared").status_code == 404


def test_marking_needs_an_operator_session(atc: Atc):
    atc.fly("dev-001", B_PATH)
    atc.c.headers["cookie"] = atc.cookie.split("; ")[1]  # the pilot's cookie only
    assert atc.mark("dev-001", "cleared").status_code == 401
    assert atc.c.post("/api/ops/flights/dev-001/clearance", json={}).status_code == 401
    guarded = [r for r in clearance.router.routes if isinstance(r, APIRoute) and r.path.endswith("/clearance")]
    assert guarded and all(auth.require_operator in [d.call for d in r.dependant.dependencies] for r in guarded)


def test_a_reroute_into_other_airspace_starts_pending_again(atc: Atc):
    r = atc.fly("dev-001", B_PATH)
    atc.mark("dev-001", "cleared")
    r["path"] = D_PATH  # the pilot took a route that needs the tower, not clearance
    got = atc.routes()["dev-001"]["atc"]
    assert got["requirement"] == "contact_tower" and got["status"] == "pending"
    r["path"] = B_PATH  # back: the old mark is still on file for Class B
    assert atc.routes()["dev-001"]["atc"]["status"] == "done"
    atc.fly("dev-001", B_PATH)  # a new flight (new flight id) has no mark
    assert atc.routes()["dev-001"]["atc"]["status"] == "pending"


def test_the_pilot_sees_the_mark_over_the_websocket_and_in_device_state(atc: Atc):
    atc.fly("dev-001", B_PATH)
    state = atc.c.get("/api/devices/dev-001").json()
    assert state["current_flight"]["atc"]["requirement"] == "clearance"
    assert state["current_flight"]["atc"]["status"] == "pending"
    with atc.c.websocket_connect("/ws/devices/dev-001", headers={"cookie": atc.cookie}) as ws:
        assert ws.receive_json()["type"] == "state"
        atc.mark("dev-001", "cleared")
        ev = ws.receive_json()
        assert ev["type"] == "clearance"
        p = ev["payload"]
        assert p["status"] == "done" and p["action"] == "cleared" and p["by_role"] == "ops"
        assert p["requirement"] == "clearance" and p["airspaces"] == ["Tampa Class B"]
        assert "op" not in p.values() and "by" not in p  # the controller's identity stays with ATC
        atc.mark("dev-001", "revoke")
        assert ws.receive_json()["payload"]["status"] == "pending"
    again = atc.c.get("/api/devices/dev-001").json()["current_flight"]["atc"]
    assert again["status"] == "pending" and again["by_role"] == "ops"


def test_another_unit_hears_nothing(atc: Atc):
    atc.fly("AL-02", B_PATH)
    with atc.c.websocket_connect("/ws/devices/dev-001", headers={"cookie": atc.cookie}) as ws:
        ws.receive_json()
        assert atc.mark("AL-02", "cleared").status_code == 200
        app_state(atc.c).hub.publish("dev-001", "heartbeat", {"t": "x"})
        assert ws.receive_json()["type"] == "heartbeat"  # the first thing the pilot got after the state


def test_candidate_route_requirement_for_route_cards(atc: Atc):
    r = atc.c.get("/api/devices/dev-001/atc-requirement", params={"path": ",".join(B_PATH)})
    assert r.status_code == 200 and r.json()["requirement"] == "clearance"
    assert atc.c.get("/api/devices/dev-001/atc-requirement", params={"path": "ev-lal"}).status_code == 422
    atc.c.headers["cookie"] = atc.cookie.split("; ")[0]  # operator only: no pilot session
    assert atc.c.get("/api/devices/dev-001/atc-requirement", params={"path": ",".join(B_PATH)}).status_code == 401


def test_pending_clearance_never_blocks_choosing_or_flying_a_route(atc: Atc):
    resp = atc.c.post("/api/devices/dev-001/route-request", json={"destination": "ev-orl"}).json()
    sel = {"request_id": resp["request_id"], "route_id": resp["routes"][0]["route_id"]}
    got = atc.c.post("/api/devices/dev-001/route-selection", json=sel)
    assert got.status_code == 200 and got.json()["status"] == "planned"  # whatever the airspace, no ATC mark needed
    flight = atc.c.get("/api/devices/dev-001").json()["current_flight"]
    assert flight["atc"]["status"] in ("pending", "not_needed")  # shown, never enforced
    assert atc.c.post("/api/devices/dev-001/flight/end").status_code == 200
    # and no routing/navigation code reads the marks at all
    for fn in (pilot.route_request, pilot.route_selection, pilot.flight_end):
        assert "clearance" not in inspect.getsource(fn) and "atc" not in inspect.getsource(fn).lower()
    import aerolink.service.core as core
    import aerolink.service.planner as planner

    for mod in (core, planner):
        assert "get_clearances" not in inspect.getsource(mod)


def test_ops_module_only_reads_marks():
    text = inspect.getsource(ops)
    assert "set_clearance" not in text and "delete_clearance" not in text


# --- migration 0006 ------------------------------------------------------------------------------------------
def test_migration_0006_up_and_down_on_sqlite(tmp_path: Path):
    db = tmp_path / "m.db"
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")

    def tables() -> set[str]:
        con = sqlite3.connect(db)
        out = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        con.close()
        return out

    command.upgrade(cfg, "head")
    assert "clearances" in tables()
    command.downgrade(cfg, "0005")
    assert "clearances" not in tables() and "operators" in tables()
    command.upgrade(cfg, "head")
    assert "clearances" in tables()


def test_site_inside_class_b_needs_clearance_on_its_own_connector():
    """A flight to/from a site inside Class B (its connector carries ``exempt_airspace``) needs a clearance."""
    from aerolink.service.clearance import load_legs, requirement_for_path

    legs = load_legs(Path(__file__).resolve().parents[2] / "airspace" / "central-fl-v1.geojson")
    req = requirement_for_path(["ev-parramore", "wp-orlando"], legs)
    assert req["requirement"] == "clearance"
    assert any("Orlando" in n and "Class B" in n for n in req["airspaces"])
