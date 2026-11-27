"""R1 checkpoint 2: the overview endpoints, the cache and its regeneration rules, the background refresh and the
language-model seam. The model is always a stub (``ov.call_llm`` is patched in every test; the default stub raises).
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from conftest import app_state, portal_of
from fastapi.testclient import TestClient
from test_e610_ops import plan_on, row
from test_g2_auth import operator_login
from test_service import run

from aerolink.network import Network
from aerolink.service import ops
from aerolink.service import ops_overview as ov
from aerolink.service.app import create_app
from aerolink.service.settings import Settings
from aerolink.service.store import Store
from aerolink.summary import llm as llm_mod


@pytest.fixture(autouse=True)
def no_model(monkeypatch: pytest.MonkeyPatch):
    """Default: any model call is a test failure. Tests that need a model patch ``ov.call_llm`` themselves."""
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    def refuse(system_prompt, lines, max_tokens):
        raise AssertionError("a test reached the language model without a stub")

    monkeypatch.setattr(ov, "call_llm", refuse)
    monkeypatch.setattr(ov, "REFRESH_S", 3600.0)  # no background ticks during a test


class Stub:
    """A fake model: records calls; ``reply`` is text, a callable of the lines, or an exception to raise."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], int]] = []
        self.reply: Any = None  # None: echo the fact lines as bullets

    def __call__(self, system_prompt: str, lines: list[str], max_tokens: int) -> str:
        self.calls.append((system_prompt, list(lines), max_tokens))
        if isinstance(self.reply, BaseException):
            raise self.reply
        if callable(self.reply):
            return self.reply(lines)
        if self.reply is None:
            return "\n".join(f"- {ln}" for ln in lines)
        return self.reply


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Stub:
    s = Stub()
    monkeypatch.setattr(ov, "call_llm", s)
    return s


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
        operator_login(c)
        s = app_state(c).service
        now = time.time()
        p1 = fl_network.shortest_paths("ev-water-st", "ev-suntrax", vehicle_type="passenger", k=1)[0]
        p2 = ["hp-1fl7", "wp-bartow", "wp-lakeland", "wp-auburndale", "ev-suntrax"]
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


def refresh(c: TestClient) -> None:
    portal_of(c).call(ov.refresh_overview, c.app)


def fresh_cache(c: TestClient) -> ov.Cache:
    """Forget the overview so the next refresh is a cold start (template), as after a restart."""
    cache = ov.cache_of(c.app)
    cache.overview = None
    return cache


def flight_id(c: TestClient, callsign: str) -> str:
    return next(r["flight_id"] for r in c.get("/api/ops/routes").json() if r["callsign"] == callsign)


def wait_for_llm(c: TestClient, cache: ov.Cache) -> None:
    for _ in range(200):
        if not cache.inflight:
            return
        time.sleep(0.02)
    raise AssertionError("background flight LLM never finished")


# --- auth and the routes ----------------------------------------------------------------------------------
def test_both_endpoints_need_an_operator_session(client: TestClient):
    anon = TestClient(client.app)  # no lifespan, no cookie
    assert anon.get("/api/ops/overview").status_code == 401
    assert anon.get("/api/ops/flights/anything/overview").status_code == 401


def test_unknown_flight_is_404(client: TestClient):
    assert client.get("/api/ops/flights/no-such-flight/overview").status_code == 404


def test_the_endpoints_are_registered_and_the_ops_router_is_unchanged(client: TestClient):
    paths = set(cast(Any, client.app).openapi()["paths"])  # the included routers are resolved lazily in this FastAPI
    assert {"/api/ops/overview", "/api/ops/flights/{flight_id}/overview"} <= paths
    ops_paths = {getattr(r, "path", "") for r in ops.router.routes}
    assert "/api/ops/overview" not in ops_paths  # test_e610 pins ops.router to its four read endpoints


# --- cold start and the background refresh -------------------------------------------------------------
def test_the_first_request_answers_with_a_template_and_never_calls_the_model(client: TestClient, stub: Stub):
    fresh_cache(client)
    body = client.get("/api/ops/overview").json()
    assert body["source"] == "template" and body["bullets"] and body["horizon_min"] == 30
    assert body["check"] == {"rejected": False} and stub.calls == []
    assert len(body["bullets"]) <= ov.MAX_BULLETS and all(len(b) <= ov.MAX_CHARS for b in body["bullets"])


def test_the_background_refresh_uses_the_model_text_when_it_passes(client: TestClient, stub: Stub):
    fresh_cache(client)
    refresh(client)  # cold template
    assert stub.calls == []
    refresh(client)  # the LLM is due at once after a cold start
    assert len(stub.calls) == 1
    system_prompt, lines, max_tokens = stub.calls[0]
    assert system_prompt == ov.OVERVIEW_PROMPT and max_tokens == ov.OVERVIEW_MAX_TOKENS and lines
    body = client.get("/api/ops/overview").json()
    assert body["source"] == "llm" and body["check"] == {"rejected": False}


def test_a_number_the_facts_do_not_have_gives_the_template_and_a_rejection(client: TestClient, stub: Stub):
    fresh_cache(client)
    refresh(client)
    stub.reply = "- Lakeland has 99 arrivals in the next half hour."
    refresh(client)
    body = client.get("/api/ops/overview").json()
    assert body["source"] == "template" and body["check"]["rejected"] is True
    assert "99" in body["check"]["reason"]
    assert body["bullets"] == ov.template_bullets(body["facts"])


@pytest.mark.parametrize(
    "reply, why",
    [
        ("\n".join(f"- line {i}" for i in range(5)), "bullets"),  # 5 bullets
        ("- " + "word " * 40, "140"),  # a bullet over 140 characters
        ("- **Bold** lead", "markdown"),
        ("No bullets at all.", "no '- '"),
        ("", "no '- '"),
    ],
)
def test_limits_and_format_violations_give_the_template(client: TestClient, stub: Stub, reply: str, why: str):
    fresh_cache(client)
    refresh(client)
    stub.reply = reply
    refresh(client)
    body = client.get("/api/ops/overview").json()
    assert body["source"] == "template" and body["check"]["rejected"] is True and why in body["check"]["reason"]


def test_a_model_error_gives_the_template_without_an_exception(client: TestClient, stub: Stub):
    fresh_cache(client)
    refresh(client)
    stub.reply = RuntimeError("set LLM_MODEL and LLM_API_KEY (see .env.example)")
    refresh(client)
    body = client.get("/api/ops/overview").json()
    assert body["source"] == "template" and body["check"]["rejected"] is False
    assert "error" in body["check"]["reason"]


def test_the_model_text_is_regenerated_only_when_old_enough_and_the_facts_changed(
    client: TestClient, stub: Stub, monkeypatch: pytest.MonkeyPatch
):
    clock = {"t": 1_800_000_000.0}
    app_state(client).service.clock = lambda: datetime.fromtimestamp(clock["t"], UTC)  # the service clock
    facts_now = {"v": [{"kind": "quiet"}]}
    monkeypatch.setattr(ov, "facts_of", lambda snap: facts_now["v"])
    fresh_cache(client)
    refresh(client)  # cold template
    refresh(client)  # LLM run #1 at t0
    assert len(stub.calls) == 1
    facts_now["v"] = [
        {
            "kind": "reroute",
            "reroute_id": "rr",
            "callsign": "AL-02",
            "gain_s": 120.0,
            "status": "proposed",
            "medical": False,
        }
    ]  # the facts changed
    clock["t"] += 10
    refresh(client)
    assert len(stub.calls) == 1  # changed, but only 10 s old: no new text yet
    body = client.get("/api/ops/overview").json()  # §9.4: the template for the NEW facts, never the old LLM text
    assert body["source"] == "template" and body["bullets"] == ov.template_bullets(body["facts"])
    clock["t"] += 50
    refresh(client)
    assert len(stub.calls) == 2  # changed and 60 s old: regenerated
    assert client.get("/api/ops/overview").json()["source"] == "llm"
    refresh(client)
    clock["t"] += 100
    refresh(client)
    assert len(stub.calls) == 2  # unchanged facts: nothing to do however old the text is


def test_two_refreshes_never_overlap_and_only_one_model_call_runs_at_a_time(
    client: TestClient, stub: Stub, monkeypatch: pytest.MonkeyPatch
):
    active = {"now": 0, "max": 0}
    lock = threading.Lock()
    real_snapshot = ov.take_snapshot

    async def slow_snapshot(app):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.05)
        active["now"] -= 1
        return await real_snapshot(app)

    def slow_model(system_prompt, lines, max_tokens):
        with lock:
            active["llm"] = active.get("llm", 0) + 1
            active["llm_max"] = max(active.get("llm_max", 0), active["llm"])
        time.sleep(0.05)
        with lock:
            active["llm"] -= 1
        return "\n".join(f"- {ln}" for ln in lines)

    monkeypatch.setattr(ov, "take_snapshot", slow_snapshot)
    monkeypatch.setattr(ov, "call_llm", slow_model)
    fresh_cache(client)
    fid_a, fid_b = flight_id(client, "AL-01"), flight_id(client, "AL-03")
    now = app_state(client).service.clock().timestamp()
    cache = ov.cache_of(client.app)

    async def storm():
        app = client.app
        await asyncio.gather(
            ov.refresh_overview(app),
            ov.refresh_overview(app),
            ov.refresh_overview(app),
            ov._llm_flight(app, fid_a, ["one fact."], "d1", now),
            ov._llm_flight(app, fid_b, ["two fact."], "d2", now),
        )

    portal_of(client).call(storm)
    assert active["max"] == 1, "two snapshot refreshes overlapped"
    assert active["llm_max"] == 1, "two model calls were in flight at once"
    assert cache.overview is not None  # some attempts may be skipped while the slot is busy (never queued)


# --- per flight -------------------------------------------------------------------------------------------
def test_a_flight_gets_the_template_at_once_then_the_model_text(client: TestClient, stub: Stub):
    fresh_cache(client)
    stub.reply = lambda lines: lines[0]  # one prose sentence, as the flight prompt asks
    fid = flight_id(client, "AL-01")
    cache = ov.cache_of(client.app)
    first = client.get(f"/api/ops/flights/{fid}/overview").json()
    assert first["source"] == "template" and first["callsign"] == "AL-01" and first["flight_id"] == fid
    wait_for_llm(client, cache)
    assert len(stub.calls) == 1 and stub.calls[0][0] == ov.FLIGHT_PROMPT
    second = client.get(f"/api/ops/flights/{fid}/overview").json()
    assert second["source"] == "llm"
    assert len(second["text"]) <= ov.FLIGHT_MAX_CHARS and second["text"].count(".") <= 2 + second["text"].count("..")


def test_a_flight_text_over_two_sentences_gives_the_template(client: TestClient, stub: Stub):
    fresh_cache(client)
    fid = flight_id(client, "AL-01")
    cache = ov.cache_of(client.app)
    client.get(f"/api/ops/flights/{fid}/overview")
    wait_for_llm(client, cache)
    stub.reply = "One. Two. Three. Four."
    cache.flights[fid].llm_at = None  # the entry is due again
    cache.flights[fid].digest = "stale"
    client.get(f"/api/ops/flights/{fid}/overview")
    wait_for_llm(client, cache)
    assert client.get(f"/api/ops/flights/{fid}/overview").json()["source"] == "template"


def test_flight_facts_cover_arrival_legs_conflicts_reroutes_and_atc():
    now = 1_800_000_000.0
    row_ = {
        "flight_id": "f1",
        "callsign": "AL-01",
        "destination": "ev-lal",
        "path": ["ev-x", "wp-a", "wp-b", "ev-lal"],
        "eta_p10": now + 600,
        "eta_p50": now + 660,
        "eta_p90": now + 720,
        "nodes": [],
        "status": "active",
        "atc": {"requirement": "clearance", "status": "pending", "legs": [["wp-a", "wp-b"]]},
        "medical": False,
    }
    row_["nodes"] = [
        {"node": "wp-a", "eta_p10": now + 300, "eta_p50": now + 320, "eta_p90": now + 340},
        {"node": "ev-lal", "eta_p10": now + 600, "eta_p50": now + 660, "eta_p90": now + 720},
    ]
    cong = {
        "steps_min": list(range(0, 50, 5)),
        "sites": [],
        "edges": [{"edge": ["wp-a", "wp-b"], "levels": ["heavy"] * 4 + ["low"] * 6, "peak": "heavy"}],
    }
    snap = {
        "now": now,
        "routes": [row_, {"flight_id": "f2", "callsign": "AL-02"}],
        "congestion": cong,
        "conflicts": [
            {
                "severity": "high",
                "kind": "junction",
                "time_to_conflict_s": 200,
                "flights": [{"flight_id": "f1"}, {"flight_id": "f2"}],
            }
        ],
        "reroutes": [
            {
                "id": "rr",
                "flight_id": "f1",
                "callsign": "AL-01",
                "gain_s": 240.0,
                "status": "proposed",
                "trigger": "faster",
            }
        ],
        "names": {"ev-lal": "Lakeland Linder Intl Airport (LAL)", "wp-a": "A", "wp-b": "B"},
        "network": SimpleNamespace(edge_between=lambda u, v: (_ for _ in ()).throw(KeyError((u, v)))),
    }
    lines = ov.flight_sentences(snap, row_)
    joined = "\n".join(lines)
    assert lines[0].startswith("Arrives at Lakeland Linder Intl Airport ")  # the trailing parenthetical is dropped
    assert "A to B is heavy" in joined  # a connector-like span without a highway
    assert "Predicted high junction conflict with AL-02 in 4 min." in joined
    assert "A reroute for AL-01 waits for approval, saves 4 min." in joined
    assert "AL-01 needs ATC clearance by" in joined
    text = ov.flight_template(lines)
    assert len(text) <= ov.FLIGHT_MAX_CHARS and ov.parse_flight(text, ov.facts_for_llm_lines(lines)) is None


# --- the seam and the model settings ----------------------------------------------------------------------
def test_the_model_seam_is_module_level_with_the_agreed_signature():
    assert list(inspect.signature(ov.call_llm).parameters) == ["system_prompt", "lines", "max_tokens"]


def test_the_route_summary_keeps_its_defaults():
    params = inspect.signature(llm_mod.call_llm).parameters
    assert params["timeout_s"].default == 20.0 and params["max_tokens"].default == 120
    assert params["system"].default == llm_mod.SYSTEM_PROMPT


def test_the_service_clock_drives_both_the_etas_and_the_horizon(client: TestClient, fl_network: Network):
    """Two clocks were the bug (the snapshot read wall time while the ETAs read the service clock). Pin the
    service clock to a sim time far from wall time, seed flights from that time, and check every fact against it."""
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    t = _dt(2026, 11, 21, 22, 20, tzinfo=ZoneInfo("America/New_York")).timestamp()
    s = app_state(client).service
    s.clock = lambda: _dt.fromtimestamp(t, _UTC)
    s.flights.clear()
    for aid, path in (
        ("AL-11", fl_network.shortest_paths("ev-parramore", "ev-suntrax", vehicle_type="passenger", k=1)[0]),
        ("AL-12", fl_network.shortest_paths("ev-orl", "ev-lal", vehicle_type="passenger", k=1)[0]),
    ):
        s.flights[aid] = row(fl_network, aid, path, t, source="sim")
    snap = portal_of(client).call(ov.take_snapshot, client.app)
    assert snap["now"] == t, "the horizon's now is the service clock"
    routes = snap["routes"]
    assert routes, "the seeded flights must be live"
    for r in routes:
        assert t <= float(r["eta_p50"]) <= t + 3 * 3600, "ETAs are computed from the service clock, not wall time"
    horizon_end = t + ov.HORIZON_S + ov.SLOT_S
    for f in ov.facts_of(snap):
        for key in ("from_s", "to_s", "earliest_due_s"):
            if key in f:
                assert f[key] <= horizon_end, f"{f['kind']} {key} is beyond the horizon"
    # moving the service clock forward past the flights' windows removes their facts
    s.clock = lambda: _dt.fromtimestamp(t + 10 * 3600, _UTC)
    later = portal_of(client).call(ov.take_snapshot, client.app)
    # only overdue pending clearances may remain (they stay listed until ATC marks them, as confirmed earlier)
    assert all(f["kind"] == "clearance" and f["earliest_due_s"] < t + 10 * 3600 for f in ov.facts_of(later))


def test_a_hung_model_call_times_out_to_the_template_and_holds_the_slot_until_it_returns(
    client: TestClient, stub: Stub, monkeypatch: pytest.MonkeyPatch
):
    """§9.1: the caller gets the template after LLM_TIMEOUT_S; the slot stays taken until the thread returns;
    attempts meanwhile are skipped (never queued), so the model is not called a second time."""
    monkeypatch.setattr(ov, "LLM_TIMEOUT_S", 0.2)
    release = threading.Event()
    calls: list[int] = []

    def hung(system_prompt, lines, max_tokens):
        calls.append(1)
        release.wait(10)
        return "- done."

    monkeypatch.setattr(ov, "call_llm", hung)
    fresh_cache(client)
    refresh(client)  # cold template
    started = time.monotonic()
    refresh(client)  # the attempt hangs and times out
    assert time.monotonic() - started < 3
    body = client.get("/api/ops/overview").json()
    assert body["source"] == "template" and "no answer" in body["check"]["reason"]
    cache = ov.cache_of(client.app)
    assert cache.llm_busy, "the thread still runs, so the slot is still taken"
    with pytest.raises(ov.LlmBusy):
        portal_of(client).call(ov.run_llm, cache, hung, ["x"], 10)
    assert len(calls) == 1, "a skipped attempt never reaches the model"
    release.set()
    for _ in range(100):
        portal_of(client).call(asyncio.sleep, 0.02)
        if not cache.llm_busy:
            break
    assert not cache.llm_busy
    assert portal_of(client).call(ov.run_llm, cache, lambda *a: "ok", [], 1) == "ok"


def test_flights_that_are_no_longer_live_are_pruned(client: TestClient):
    fresh_cache(client)
    refresh(client)
    cache = ov.cache_of(client.app)
    cache.flights["ghost"] = ov.FlightText("GHOST", "d", "template", "2026-01-01T00:00:00")
    refresh(client)
    assert "ghost" not in cache.flights


def test_a_reroute_under_a_minute_arrives_no_later_and_never_says_saves_0_min():
    f = {"kind": "reroute", "reroute_id": "r", "callsign": "AL-01", "gain_s": 30.0, "status": "proposed",
         "medical": False}  # fmt: skip
    assert ov.sentence(f) == "A reroute for AL-01 waits for approval, arrives no later."
    assert "saves 0 min" not in ov.sentence({**f, "gain_s": 59.0})
    assert ov.sentence({**f, "gain_s": 240.0}) == "A reroute for AL-01 waits for approval, saves 4 min."


def test_llm_text_is_shown_only_while_its_facts_are_current():
    ov_ = ov.Overview(facts=[], digest="new", template=["template for new facts."], generated_at="t",
                      llm=["old facts text."], llm_digest="old")  # fmt: skip
    assert ov.shown_overview(ov_) == ("template", ["template for new facts."])
    ov_.llm_digest = "new"
    assert ov.shown_overview(ov_) == ("llm", ["old facts text."])


def test_the_flight_template_follows_the_priority_arrival_reroute_atc_legs():
    now = 1_800_000_000.0
    row_ = {
        "flight_id": "f1", "callsign": "AL-01", "destination": "ev-lal", "path": ["ev-x", "wp-a", "wp-b", "ev-lal"],
        "eta_p10": now + 600, "eta_p50": now + 660, "eta_p90": now + 720, "status": "active", "medical": False,
        "atc": {"requirement": "clearance", "status": "pending", "legs": [["wp-a", "wp-b"]]},
        "nodes": [{"node": "wp-a", "eta_p10": now + 300, "eta_p50": now + 320, "eta_p90": now + 340}],
    }  # fmt: skip
    cong = {"steps_min": list(range(0, 50, 5)), "sites": [],
            "edges": [{"edge": ["wp-a", "wp-b"], "levels": ["heavy"] * 4 + ["low"] * 6, "peak": "heavy"}]}  # fmt: skip
    snap = {
        "now": now, "routes": [row_], "congestion": cong, "conflicts": [],
        "reroutes": [{"id": "rr", "flight_id": "f1", "callsign": "AL-01", "gain_s": 240.0, "status": "proposed",
                      "trigger": "faster"}],  # fmt: skip
        "names": {"ev-lal": "Lakeland", "wp-a": "A", "wp-b": "B"},
        "network": SimpleNamespace(edge_between=lambda u, v: None),
    }  # fmt: skip
    lines = ov.flight_sentences(snap, row_)
    order = [
        next(i for i, s in enumerate(lines) if s.startswith("Arrives")),
        next(i for i, s in enumerate(lines) if "reroute" in s),
        next(i for i, s in enumerate(lines) if "needs ATC clearance" in s),
        next(i for i, s in enumerate(lines) if "is heavy" in s),
    ]
    assert order == sorted(order), lines
    assert ov.flight_template(lines).startswith(lines[0] + " " + lines[1])  # the template is arrival + reroute


def test_an_overdue_pending_clearance_says_it_is_still_pending():
    """Orchestrator ruling (3): overdue pending clearances stay listed, worded as still pending, same grouping."""
    now = 1_800_000_000.0
    atc = {"requirement": "clearance", "status": "pending", "legs": [["wp-a", "wp-b"]]}
    rows = []
    for cs in ("AL-01", "AL-02"):
        r = {"flight_id": f"f-{cs}", "callsign": cs, "destination": "ev-lal", "status": "active", "medical": False,
             "atc": atc, "eta_p50": now + 1200, "nodes": [{"node": "wp-a", "eta_p50": now - 600}]}  # fmt: skip
        rows.append(r)
    out = [
        f
        for f in ov.facts_of(
            {
                "now": now,
                "routes": rows,
                "congestion": {"steps_min": [], "sites": [], "edges": []},
                "reroutes": [],
                "names": {},
                "network": None,
            }
        )
        if f["kind"] == "clearance"
    ]  # noqa
    assert len(out) == 1 and out[0]["overdue_count"] == 2 and out[0]["callsigns"] == ["AL-01", "AL-02"]
    assert ov.template_bullets(out) == [
        f"2 flights need ATC clearance; earliest due {ov.clock(now - 600)} (AL-01); 2 overdue."
    ]


def test_the_event_loop_keeps_ticking_while_a_snapshot_runs(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """F4: the synchronous route rows run in a thread; a slow calculation must not stall the loop."""
    from aerolink.service import ops as ops_mod

    real = ops_mod._routes_rows

    def slow(*args, **kwargs):
        time.sleep(0.4)  # a heavy synchronous calculation
        return real(*args, **kwargs)

    monkeypatch.setattr(ops_mod, "_routes_rows", slow)

    async def measure() -> int:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        task = asyncio.create_task(ticker())
        await ov.take_snapshot(client.app)
        task.cancel()
        return ticks

    assert portal_of(client).call(measure) >= 5


def test_a_failed_snapshot_on_a_cold_start_is_a_503_and_after_a_good_one_the_cache_is_served(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """§11.3: no cache and the snapshot fails -> 503 (logged); with a cache -> the last good one."""
    real = ov.take_snapshot

    async def broken(app):
        raise RuntimeError("planner exploded")

    fresh_cache(client)
    ov.cache_of(client.app).snapshot = None
    monkeypatch.setattr(ov, "take_snapshot", broken)
    with caplog.at_level("ERROR", logger="aerolink.service.ops_overview"):
        r = client.get("/api/ops/overview")
    assert r.status_code == 503 and r.json() == {"error": "overview_unavailable"}
    assert "snapshot failed" in caplog.text
    fid = flight_id(client, "AL-01")
    monkeypatch.setattr(ov, "take_snapshot", broken)
    ov.cache_of(client.app).snapshot = None
    assert client.get(f"/api/ops/flights/{fid}/overview").status_code == 503

    monkeypatch.setattr(ov, "take_snapshot", real)  # one good snapshot
    assert client.get("/api/ops/overview").status_code == 200
    good = client.get("/api/ops/overview").json()
    monkeypatch.setattr(ov, "take_snapshot", broken)  # now the data call fails again
    cached = client.get("/api/ops/overview")
    assert cached.status_code == 200 and cached.json()["bullets"] == good["bullets"]
    assert client.get(f"/api/ops/flights/{fid}/overview").status_code == 200
