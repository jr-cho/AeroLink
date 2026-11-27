"""Simulated pilots follow AeroLink's rules (user decision 2026-11-26, U-API contract §8).

They acknowledge the AI disclaimer before their first flight, answer ATC-approved reroutes like a pilot (acknowledge or
unable, after a human-like delay, with the scenario's acceptance rate) and, on acknowledge, fly the new route.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from aerolink.network import Network
from aerolink.sim import live as live_mod
from aerolink.sim.live import Action, SimLive, reroute_actions


def rec_of(a: Action) -> dict:
    assert a.record is not None
    return a.record


def _rec(aid: str, lat: float, lon: float, alt: float = 300.0, gs: float = 60.0) -> dict:
    return {
        "schema_version": "4.0",
        "message_id": "m",
        "aircraft_id": aid,
        "vehicle_type": "passenger",
        "timestamp": "2026-11-26T12:00:00Z",
        "position": {"lat": lat, "lon": lon, "alt_msl_m": alt},
        "motion": {"ground_speed_mps": gs, "course_deg": 90.0, "vertical_speed_mps": 0.0},
        "fix": {"type": "3d", "hdop": 0.9, "satellites": 11, "replayed": False},
        "active_flight_id": "f-1",
    }


class Bus:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict | bytes]] = []

    async def publish(self, topic: str, payload: bytes) -> None:
        try:
            self.sent.append((topic, json.loads(payload)))
        except ValueError:
            self.sent.append((topic, payload))

    def topics(self, suffix: str) -> list[dict]:
        """JSON messages on topics ending with ``suffix`` (plain-text ones, like ``landed``, via ``raw``)."""
        return [p for t, p in self.sent if t.endswith(suffix) and isinstance(p, dict)]

    def raw(self, suffix: str) -> list[dict | bytes]:
        return [p for t, p in self.sent if t.endswith(suffix)]


def test_reroute_actions_fly_the_waypoints_descend_land_and_hold():
    last = _rec("sim-p01", 28.0, -81.5, alt=300.0, gs=60.0)
    # ~6 km east then ~6 km north at 300 m, landing on a pad at 30 m
    wps = [[28.0, -81.44, 300.0], [28.054, -81.44, 300.0]]
    acts = reroute_actions(last, wps, now_offset=10.0, speed=1.0, pad_alt_m=30.0, hold_until_offset=500.0)
    tel = [a for a in acts if a.kind == "telemetry"]
    assert all(a.synthetic for a in acts)
    assert 150 <= len(tel) <= 260  # ~12 km at 60 m/s, one per second
    assert tel[0].offset_s == pytest.approx(11.0)
    end = rec_of(tel[-1])["position"]
    assert end["lat"] == pytest.approx(28.054, abs=1e-6) and end["lon"] == pytest.approx(-81.44, abs=1e-6)
    assert end["alt_msl_m"] == pytest.approx(30.0, abs=1e-6)  # descended to the pad
    assert rec_of(tel[len(tel) // 2])["position"]["alt_msl_m"] == pytest.approx(300.0)  # cruise in the middle
    landed = [a for a in acts if a.kind == "landed"]
    assert len(landed) == 1 and landed[0].offset_s > tel[-1].offset_s
    ground = [a for a in acts if a.kind == "ground"]
    assert ground and ground[0].offset_s > landed[0].offset_s and ground[-1].offset_s < 500.0
    assert all(rec_of(g)["motion"]["ground_speed_mps"] == 0.0 for g in ground)


def test_reroute_actions_respect_replay_speed():
    last = _rec("sim-p01", 28.0, -81.5, gs=60.0)
    acts = reroute_actions(
        last, [[28.0, -81.44, 300.0]], now_offset=0.0, speed=10.0, pad_alt_m=None, hold_until_offset=None
    )
    tel = [a for a in acts if a.kind == "telemetry"]
    assert tel[1].offset_s - tel[0].offset_s == pytest.approx(0.1)  # one simulated second = 1/speed wall seconds
    assert not [a for a in acts if a.kind == "ground"]


def _sim(fl_network: Network, bus: Bus, rate: float = 1.0) -> SimLive:
    sim = SimLive(fl_network, bus.publish, speed=1000.0, seed=7)
    sim.acceptance_rate = rate
    return sim


def test_each_pilot_acknowledges_the_disclaimer_once(fl_network: Network):
    bus = Bus()
    sim = _sim(fl_network, bus)
    schedule = [
        Action(i * 0.001, "telemetry", aid, record=_rec(aid, 28.0, -81.5))
        for i, aid in enumerate(["a", "b", "a", "b", "a"])
    ]
    asyncio.run(sim.run_schedule(schedule))
    acks = bus.topics("/ai-ack")
    assert sorted(a["aircraft_id"] for a in acks) == ["a", "b"]  # once per aircraft
    assert all(a["version"] == "current" for a in acks)
    # the ack goes out right after the aircraft's first telemetry, before its second
    order = [t.split("/")[-1] for t, _ in bus.sent if "/a/" in t]
    assert order[:2] == ["telemetry", "ai-ack"]


@pytest.mark.parametrize(("rate", "expected"), [(1.0, "acknowledge"), (0.0, "unable")])
def test_pilot_answers_an_approved_reroute_with_the_scenario_rate(fl_network: Network, monkeypatch, rate, expected):
    monkeypatch.setattr(live_mod, "PILOT_DELAY_S", (0.0, 0.0))
    bus = Bus()
    sim = _sim(fl_network, bus, rate)
    sim.last_rec["sim-p01"] = _rec("sim-p01", 28.0, -81.5)
    flown: list[str] = []
    monkeypatch.setattr(sim, "fly_reroute", lambda aid, r: flown.append(aid) or True)
    reroute = {
        "id": "rr_1",
        "aircraft_id": "sim-p01",
        "status": "approved",
        "proposed": {"waypoints": [[28.0, -81.44, 300.0]]},
    }

    async def go() -> None:
        await sim.answer_reroute(reroute)
        await sim.answer_reroute(reroute)  # answered once only

    asyncio.run(go())
    answers = bus.topics("/reroute-response")
    assert len(answers) == 1 and answers[0]["response"] == expected and answers[0]["reroute_id"] == "rr_1"
    assert flown == (["sim-p01"] if expected == "acknowledge" else [])


def test_only_approved_reroutes_are_answered(fl_network: Network, monkeypatch):
    monkeypatch.setattr(live_mod, "PILOT_DELAY_S", (0.0, 0.0))
    bus = Bus()
    sim = _sim(fl_network, bus)
    asyncio.run(sim.answer_reroute({"id": "rr_2", "aircraft_id": "x", "status": "proposed"}))
    assert bus.topics("/reroute-response") == []


def test_an_acknowledged_reroute_replaces_the_rest_of_the_flight(fl_network: Network):
    """The original remainder of the flight is skipped; the new route is flown, landed, then the schedule resumes."""
    bus = Bus()
    sim = SimLive(fl_network, bus.publish, speed=200.0, seed=7)  # 1 simulated s = 5 ms
    aid = "sim-p01"
    orig = [Action(i * 0.005, "telemetry", aid, record=_rec(aid, 28.0, -81.5 + i * 1e-4)) for i in range(400)]
    orig.append(Action(400 * 0.005 + 0.001, "landed", aid))
    orig += [
        Action(2.2 + i * 0.005, "ground", aid, record=_rec(aid, 28.054, -81.44, alt=30.0, gs=0.0)) for i in range(5)
    ]
    reroute = {
        "id": "rr_3",
        "aircraft_id": aid,
        "status": "approved",
        "proposed": {"waypoints": [[28.0, -81.49, 300.0], [28.01, -81.49, 300.0]]},
    }

    async def go() -> None:
        run = asyncio.create_task(sim.run_schedule(orig))
        while len(bus.topics("/telemetry")) < 5:
            await asyncio.sleep(0.001)
        assert sim.fly_reroute(aid, reroute)
        await run

    asyncio.run(go())
    tel = bus.topics("/telemetry")
    lons = [t["position"]["lon"] for t in tel if t["motion"]["ground_speed_mps"] > 0]
    assert max(lons) < -81.485  # never flew the original remainder east to -81.46…
    lats = [t["position"]["lat"] for t in tel]
    assert max(lats) == pytest.approx(28.054, abs=1e-6)  # the original schedule resumed on the destination pad later
    assert pytest.approx(28.01, abs=1e-6) in lats  # reached the rerouted destination
    assert len(bus.raw("/status")) == 1  # exactly one 'landed' (the rerouted one; the original was skipped)


def test_a_withdrawn_reroute_is_not_answered(fl_network: Network, monkeypatch):
    """ATC dismissed (or it expired) while the pilot was still reading it: no answer goes out."""
    monkeypatch.setattr(live_mod, "PILOT_DELAY_S", (0.05, 0.05))
    bus = Bus()
    sim = SimLive(fl_network, bus.publish, speed=1.0, seed=7)
    rr = {"id": "rr_4", "aircraft_id": "x", "status": "approved", "proposed": {"waypoints": [[28.0, -81.44, 300.0]]}}

    async def go() -> None:
        pending = asyncio.create_task(sim.answer_reroute(rr))
        await asyncio.sleep(0.01)
        await sim.answer_reroute(dict(rr, status="dismissed"))
        await pending

    asyncio.run(go())
    assert bus.topics("/reroute-response") == []
