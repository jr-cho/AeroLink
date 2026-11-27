"""F3: adaptive ADS-B polling (30 s viewed / 5 min idle), daily budget, 429 handling. Fake clock, no network."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aerolink.adsb_live import AdsbFeed, AdsbSettings, RateLimited
from aerolink.adsb_live.client import fetch_point
from aerolink.service.adsb import ViewerActivity, router

FIX = json.loads((Path(__file__).parent / "fixtures" / "adsb_live" / "point_response.json").read_text())
DAY = 86_400.0


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make_feed(clock: Clock, fetch=None, **kw) -> AdsbFeed:
    calls: list[float] = []

    async def default() -> dict:
        calls.append(clock.t)
        return {"ac": []}

    feed = AdsbFeed(AdsbSettings(**kw), fetch=fetch or default, clock=clock)
    cast(Any, feed).calls = calls  # test-only recorder
    return feed


def step(feed: AdsbFeed, clock: Clock) -> bool:
    """Advance the fake clock to the moment the next poll is due, then poll."""
    clock.t += feed.due_in()
    return asyncio.run(feed.poll_once())


def test_idle_then_active_intervals() -> None:
    clk = Clock()
    f = make_feed(clk)
    assert f.due_in() == 0.0  # first poll at once
    assert asyncio.run(f.poll_once())
    assert f.next_delay() == 300.0 and f.snapshot()["feed"]["poll_interval_s"] == 300.0  # nobody looking
    f.touch()
    assert f.next_delay() == 30.0
    clk.t += 119
    assert f.next_delay() == 30.0  # still within the 2 min viewer window
    clk.t += 2
    assert f.next_delay() == 300.0  # lapsed
    f.viewers = lambda: True  # an open /ws/ops socket counts however long it stays open
    assert f.next_delay() == 30.0


def test_a_viewer_arriving_wakes_an_idle_wait() -> None:
    f = make_feed(Clock())
    f._wake.clear()
    f.touch()
    assert f._wake.is_set()
    f._wake.clear()
    f.touch()  # already viewed: no need to re-plan
    assert not f._wake.is_set()


def test_budget_never_exceeded_with_constant_viewing_over_24h() -> None:
    clk = Clock()
    f = make_feed(clk, daily_budget=450)
    f.viewers = lambda: True
    used: list[float] = []
    horizon = clk.t + 3 * DAY
    while clk.t < horizon:
        clk.t += f.due_in()
        if f.budget_remaining() > 0 and asyncio.run(f.poll_once()):
            used.append(clk.t)
        if f.budget_remaining() == 0:
            clk.t += f.due_in()
    calls = cast(Any, f).calls
    assert len(calls) > 450  # more than a day's worth ran, so the rolling window was exercised
    for i, t in enumerate(calls):  # any rolling 24 h window holds at most the budget
        assert len([c for c in calls[: i + 1] if c > t - DAY]) <= 450
    # the pace stretched as the budget ran down: the first requests came at 30 s, later ones slower
    gaps = [b - a for a, b in zip(calls, calls[1:], strict=False)]
    assert gaps[0] == 30.0 and max(gaps) > 300.0


def test_budget_guard_refuses_without_calling() -> None:
    clk = Clock()
    f = make_feed(clk, daily_budget=2)
    f.viewers = lambda: True
    assert asyncio.run(f.poll_once()) and asyncio.run(f.poll_once())
    n = len(cast(Any, f).calls)
    assert not asyncio.run(f.poll_once()) and len(cast(Any, f).calls) == n
    assert f.budget_remaining() == 0 and f.snapshot()["feed"]["budget_remaining"] == 0
    clk.t += DAY + 1
    assert f.budget_remaining() == 2  # the window rolled


def test_429_backs_off_and_reports_rate_limited() -> None:
    clk = Clock()
    answers = [RateLimited(120.0), RateLimited(None), {"ac": []}]

    async def fetch() -> dict:
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    f = make_feed(clk, fetch)
    f.viewers = lambda: True
    assert not asyncio.run(f.poll_once())
    feed = f.snapshot()["feed"]
    assert feed["state"] == "rate_limited" and "rate limited" in feed["error"]
    assert f.next_delay() >= 120.0  # Retry-After honoured over the 30 s interval
    clk.t += 125
    assert f.state() != "rate_limited"
    assert not asyncio.run(f.poll_once())  # no Retry-After: exponential backoff from the active interval
    assert f.next_delay() >= 30.0 * 2**2
    clk.t += f.next_delay()
    assert asyncio.run(f.poll_once()) and f.state() == "ok" and f.snapshot()["feed"]["age_s"] == 0.0


def test_rate_limit_header_lowers_the_budget() -> None:
    clk = Clock()

    async def fetch() -> dict:
        return {"ac": [], "_ratelimit_remaining": 7}

    f = make_feed(clk, fetch)
    assert asyncio.run(f.poll_once()) and f.budget_remaining() == 7


def test_fetch_point_reads_429_and_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/limited"):
            return httpx.Response(429, headers={"retry-after": "42"})
        return httpx.Response(200, json={"ac": []}, headers={"x-ratelimit-remaining": "321"})

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            doc = await fetch_point(c, "http://x", 5.0)
            assert doc["_ratelimit_remaining"] == 321
            with pytest.raises(RateLimited) as e:
                await fetch_point(c, "http://x/limited", 5.0)  # path ends .../limited? not for point queries
            assert e.value.retry_after_s == 42.0

    # point queries end in the radius, so route the 429 by host instead
    def by_host(request: httpx.Request) -> httpx.Response:
        return handler(httpx.Request("GET", "http://x/limited")) if request.url.host == "limited" else handler(request)

    async def go2() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(by_host)) as c:
            assert (await fetch_point(c, "http://ok", 5.0))["_ratelimit_remaining"] == 321
            with pytest.raises(RateLimited) as e:
                await fetch_point(c, "http://limited", 5.0)
            assert e.value.retry_after_s == 42.0

    asyncio.run(go2())


def test_watchdog_threshold_follows_the_interval() -> None:
    from aerolink.service.watchdogs import Watchdogs

    clk = Clock()
    f = make_feed(clk)
    asyncio.run(f.poll_once())
    clk.t += 250  # idle: 5 min between polls is normal
    w = Watchdogs.__new__(Watchdogs)
    w.adsb, w._adsb_ref = f, None
    assert w.check_adsb()["state"] == "ok"
    clk.t += 1000  # polling stopped for far longer than any interval
    assert w.check_adsb()["state"] == "fail"


def test_viewer_activity_touches_the_feed_and_snapshot_has_the_new_fields() -> None:
    app = FastAPI()
    app.add_middleware(ViewerActivity)
    app.include_router(router)

    @app.get("/api/ops/routes")
    def ops() -> dict:
        return {}

    @app.get("/api/state")
    def state() -> dict:
        return {}

    clk = Clock()
    app.state.adsb = make_feed(clk)
    with TestClient(app) as c:
        assert app.state.adsb.last_view is None
        c.get("/api/state")
        assert app.state.adsb.last_view is None  # not a viewer path
        c.get("/api/ops/routes")
        assert app.state.adsb.last_view == clk.t
        app.state.adsb.last_view = None
        body = c.get("/api/adsb").json()
        assert app.state.adsb.last_view == clk.t
        assert {"poll_interval_s", "budget_remaining", "age_s"} <= set(body["feed"])
