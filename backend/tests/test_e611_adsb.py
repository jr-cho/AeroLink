"""E6.11: live ADS-B parse/filter, expiry, feed states, backoff, API shape (recorded fixture, no network)."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aerolink.adsb_live import ATTRIBUTION, AdsbFeed, AdsbSettings, background_mass, parse_response
from aerolink.adsb_live.client import query_circle
from aerolink.network import Network
from aerolink.service.adsb import router
from aerolink.service.settings import Settings

FIX = json.loads((Path(__file__).parent / "fixtures" / "adsb_live" / "point_response.json").read_text())


def ac(hex_: str = "a00001", **kw) -> dict:
    base = copy.deepcopy(next(a for a in FIX["ac"] if a.get("category") == "A3"))
    base.update(hex=hex_, type="adsb_icao", flight="N123AB", category="A1", alt_baro=1500, gs=90.0, dbFlags=0)
    base.update(lat=28.0, lon=-81.9, mlat=[], seen_pos=0.4)
    base.update(kw)
    return base


def test_recorded_fixture_parses_and_drops_airliners() -> None:
    kept, n, dropped = parse_response(FIX)
    assert n == len(kept) and n + dropped == len(FIX["ac"])
    assert all(a["alt_ft"] <= 3000 and not (a["callsign"] or "").startswith(("SWA", "UAL")) for a in kept)


@pytest.mark.parametrize(
    "patch",
    [
        {"alt_baro": "ground"},
        {"alt_baro": 5000},
        {"alt_baro": 50},
        {"gs": 20.0},
        {"gs": 300.0},
        {"type": "mlat"},
        {"type": "tisb_other"},
        {"dbFlags": 1},
        {"category": "A3"},
        {"flight": "AAL123  "},
        {"lat": 40.0},
        {"hex": "~abc123"},
        {"lat": None},
    ],
)
def test_filters_drop(patch: dict) -> None:
    kept, _, dropped = parse_response({"ac": [ac(**patch)]})
    assert kept == [] and dropped == 1


def test_filters_keep_good_and_unknown_category() -> None:
    row = ac()
    unknown = ac("a00002")
    del unknown["category"]
    kept, n, dropped = parse_response({"ac": [row, unknown, ac("a00003", category="A7")]})
    assert n == 3 and dropped == 0
    assert kept[0]["icao"] == "a00001" and kept[0]["callsign"] == "N123AB" and kept[0]["gs_kt"] == 90.0


def test_query_circle_covers_focus_box() -> None:
    lat, lon, r = query_circle()
    assert 27.9 < lat < 28.2 and -82.1 < lon < -81.8 and 50 <= r <= 80


class Clock:
    t = 1000.0

    def __call__(self) -> float:
        return self.t


def feed_with(responses: list, clock: Clock) -> AdsbFeed:
    it = iter(responses)

    async def fetch() -> dict:
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    return AdsbFeed(
        AdsbSettings(poll_active_s=5.0, poll_idle_s=5.0, max_backoff_s=60.0, ttl_s=60.0, stale_after_s=30.0),
        fetch=fetch,
        clock=clock,
    )


def poll(f: AdsbFeed) -> bool:
    return asyncio.run(f.poll_once())


def test_state_transitions_ok_stale_down_ok_and_backoff() -> None:
    clk = Clock()
    f = feed_with([{"ac": [ac()]}, OSError("no route"), OSError("no route"), OSError("x"), {"ac": []}], clk)
    assert f.state() == "down"  # nothing received yet
    assert poll(f) and f.state() == "ok" and len(f.snapshot()["aircraft"]) == 1
    assert f.next_delay() == 5.0
    clk.t += 5
    assert not poll(f) and f.state() == "stale" and f.next_delay() == 10.0
    assert "no route" in f.snapshot()["feed"]["error"]
    clk.t += 40
    assert not poll(f) and f.state() == "down" and f.next_delay() == 20.0
    assert not poll(f) and f.next_delay() == 40.0
    for _ in range(10):
        f.failures += 1
    assert f.next_delay() == 60.0  # capped
    f.failures = 0
    assert poll(f) and f.state() == "ok" and f.snapshot()["feed"]["error"] is None


def test_expiry_after_60s() -> None:
    clk = Clock()
    f = feed_with([{"ac": [ac()]}, {"ac": []}], clk)
    poll(f)
    clk.t += 59
    assert len(f.snapshot()["aircraft"]) == 1
    clk.t += 2
    assert f.snapshot()["aircraft"] == []


def test_state_logged_only_on_change(caplog: pytest.LogCaptureFixture) -> None:
    clk = Clock()
    f = feed_with([{"ac": []}] + [OSError("e")] * 5, clk)
    with caplog.at_level("WARNING", logger="aerolink.adsb"):
        poll(f)
        for _ in range(5):
            clk.t += 60
            poll(f)
    assert len([r for r in caplog.records if "ADS-B feed" in r.message]) == 2  # down->ok, ok->down


def test_disabled_feed() -> None:
    f = AdsbFeed(AdsbSettings(enabled=False))
    snap = f.snapshot()
    assert snap["feed"]["state"] == "disabled" and snap["aircraft"] == []
    asyncio.run(f.run())  # returns at once


def test_api_shape_and_attribution() -> None:
    clk = Clock()
    f = feed_with([{"ac": [ac()]}], clk)
    poll(f)
    app = FastAPI()
    app.include_router(router)
    app.state.adsb = f
    body = TestClient(app).get("/api/adsb").json()
    assert body["attribution"] == ATTRIBUTION and "ODbL" in ATTRIBUTION
    assert set(body["feed"]) >= {"state", "last_ok_at", "error"} and body["feed"]["state"] == "ok"
    (a,) = body["aircraft"]
    assert set(a) >= {
        "icao",
        "callsign",
        "category",
        "lat",
        "lon",
        "alt_ft",
        "gs_kt",
        "track_deg",
        "vert_rate_fpm",
        "seen_s",
    }
    assert a["source"] == "adsb"
    assert TestClient(FastAPI()).get("/api/adsb").status_code == 404  # router not included -> nothing leaks


def test_api_without_feed_reports_disabled() -> None:
    app = FastAPI()
    app.include_router(router)
    assert TestClient(app).get("/api/adsb").json()["feed"]["state"] == "disabled"


def test_background_mass_matches_off_route_representation() -> None:
    net = Network.from_geojson(Settings().network_file)
    node = next(iter(net.nodes.values()))
    row = {"lat": node["lat"], "lon": node["lon"], "gs_kt": 90.0}
    out = background_mass([row, row], net, 5000.0)
    assert out and abs(sum(p for p, *_ in out) - 2.0) < 1e-6  # one unit of mass per aircraft
    p, nid, lo, mid, hi = out[0]
    assert nid in net.nodes and lo <= mid <= hi
    f = AdsbFeed(AdsbSettings())
    assert f.background_mass(net, 5000.0) == []


@pytest.mark.skipif(os.environ.get("ADSB_LIVE_TEST") != "1", reason="set ADSB_LIVE_TEST=1 to hit the real adsb.lol API")
def test_live_smoke() -> None:
    f = AdsbFeed(AdsbSettings(poll_active_s=5.0))

    async def go() -> bool:
        import httpx

        from aerolink.adsb_live import fetch_point

        async with httpx.AsyncClient() as c:
            f._fetch = lambda: fetch_point(c, f.settings.url, 15.0)
            return await f.poll_once()

    assert asyncio.run(go()) and f.state() == "ok"


def test_no_internet_reports_down_and_run_survives() -> None:
    f = AdsbFeed(AdsbSettings(url="http://127.0.0.1:9", poll_active_s=2.0, poll_idle_s=2.0, timeout_s=1.0))

    async def go() -> None:
        task = asyncio.create_task(f.run())
        await asyncio.sleep(1.0)
        task.cancel()

    asyncio.run(go())
    assert f.state() == "down" and f.snapshot()["feed"]["error"]
