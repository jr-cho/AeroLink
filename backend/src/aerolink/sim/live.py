"""Simulator live mode (PLAN E6.4): replay a scenario over MQTT in real time and report its flight plans.

    python -m aerolink.sim.live --scenario scenarios/nominal_day.yaml [--seed 42] [--speed 1] [--loop]

Design "replay + report plans" (user-approved):

* The scenario runs offline first (``run_simulation`` is pure and deterministic). Its telemetry is
  published to ``aerolink/sim/{id}/telemetry`` paced by the original timestamps, re-stamped to
  "now". Live and offline therefore show the same behaviour for the same seed.
* At each simulated departure the sim tells the backend its plan: ``route-request`` (destination),
  then ``route-selection`` for the returned option whose path equals the path it flies. If no option
  matches (or the backend answers no route), it sends ``flight-plan`` instead: a simulator-only
  message ``{schema_version, message_id, aircraft_id, vehicle_type, request_id, path}`` on
  ``aerolink/sim/{id}/flight-plan`` (not part of the device contract) so the backend still records
  the flown path. Simulated aircraft do not take the pilot's own AI suggestions (it is a replay), but
  simulated PILOTS follow AeroLink's rules (user decision 2026-11-26, contract §8):
  - each acknowledges the AI disclaimer once, when its aircraft first appears (``aerolink/sim/{id}/ai-ack``);
  - each answers an ATC-approved reroute (``aerolink/sim/{id}/reroute``) like a pilot: after a human-like delay
    (seeded, 5–20 s) it sends ``acknowledge`` or ``unable`` (``aerolink/sim/{id}/reroute-response``), accepting with
    the scenario's own ``suggestion_acceptance_rate``; on ``acknowledge`` it FLIES the new route: the rest of that
    flight's replay is replaced by telemetry along the proposed waypoints (``reroute_actions``).
* Parked and turnaround aircraft report too (1 Hz, speed 0, at their pad), as a real unit does whenever it is
  powered. The v4.0 telemetry schema has no on-ground field, so the backend derives ``on_ground`` from the motion
  (stationary, level, no active flight). These records come from the run's ground truth, so the airborne telemetry
  stays identical to the offline run.
* When an aircraft has landed the sim publishes ``landed`` (sim-only) on its ``status`` topic: the backend ends
  the flight and keeps the track (the aircraft is now parked and keeps reporting). ``offline`` (scenario switch or
  end of the replay) makes the backend forget the aircraft.
* ``aerolink/sim/control/scenario`` ``{"scenario": name}`` switches scenario: the replay stops, every
  reported aircraft is marked offline, the new scenario is simulated offline and replay restarts.

With ``--speed`` > 1 the replay is compressed in wall time; the backend's ETAs are still real-time
flight times, so use speed 1 for anything that compares live ETAs with the aircraft.
MQTT settings come from MQTT_HOST / MQTT_PORT / MQTT_SIM_USERNAME / MQTT_SIM_PASSWORD (.env.example).
"""

from __future__ import annotations

import argparse
import asyncio
import heapq
import itertools
import json
import logging
import math
import os
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_OID, uuid4, uuid5

import aiomqtt

from aerolink.contracts import SCHEMA_VERSION
from aerolink.network import Network
from aerolink.sim.runner import SimulationResult, run_simulation
from aerolink.sim.scenarios import load_scenario
from aerolink.sim.state import ON_PAD

REPO = Path(__file__).resolve().parents[4]
SCENARIOS = REPO / "backend" / "scenarios"
CONTROL_TOPIC = "aerolink/sim/control/scenario"
RESPONSE_TIMEOUT_S = 5.0
PILOT_DELAY_S = (5.0, 20.0)  # a simulated pilot's human-like time to answer an ATC reroute
DESCENT_S = 60.0  # the last minute of a rerouted flight descends to the pad
log = logging.getLogger("aerolink.sim.live")

Publish = Callable[[str, bytes], Awaitable[None]]


def _ts(record: dict) -> datetime:
    return datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))


def _iso(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def restamp(records: list[dict], start: datetime, speed: float = 1.0) -> list[tuple[float, dict]]:
    """(wall seconds after start, record re-stamped to the wall-clock time it is sent, fresh message_id).

    With ``speed`` > 1 the replay is compressed: timestamps stay on the wall clock, so the
    backend sees aircraft moving ``speed`` times faster than they really would.
    """
    if not records:
        return []
    t0 = _ts(records[0])
    out = []
    for r in sorted(records, key=_ts):
        offset = (_ts(r) - t0).total_seconds() / speed
        out.append((offset, stamp(r, start + timedelta(seconds=offset))))
    return out


def stamp(record: dict, when: datetime) -> dict:
    return dict(record, timestamp=_iso(when), message_id=str(uuid4()))


# --- the schedule: everything the sim says, and when -------------------------------------------------------
@dataclass(frozen=True)
class Action:
    offset_s: float  # wall seconds after the replay starts
    kind: Literal["telemetry", "ground", "depart", "landed"]
    aircraft_id: str
    record: dict | None = None  # telemetry
    path: tuple[str, ...] = ()  # depart: the path actually flown
    vehicle_type: str = "passenger"
    synthetic: bool = False  # generated live by a sim pilot's acknowledged reroute (never suppressed)


# --- a simulated pilot flies an acknowledged reroute --------------------------------------------------------------
_EARTH_M = 6_371_008.8


def _dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    k = math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(math.radians(b[0] - a[0]) * _EARTH_M, math.radians(b[1] - a[1]) * _EARTH_M * k)


def _bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    k = math.cos(math.radians((a[0] + b[0]) / 2))
    return math.degrees(math.atan2(math.radians(b[1] - a[1]) * k, math.radians(b[0] - a[0]))) % 360.0


def reroute_actions(
    last: dict,
    waypoints: list[list[float]],
    now_offset: float,
    speed: float,
    pad_alt_m: float | None,
    hold_until_offset: float | None,
) -> list[Action]:
    """The rest of a flight after its simulated pilot acknowledged a reroute (pure).

    From the aircraft's last telemetry ``last`` along ``waypoints`` ([lat, lon, alt_m], the proposed route from the
    next node to the destination pad) at the aircraft's current ground speed, one record per simulated second
    (``1/speed`` wall seconds apart), descending over the last ``DESCENT_S`` to ``pad_alt_m``; then ``landed``; then,
    if the aircraft arrives before ``hold_until_offset`` (its original landing), one ground record per second on the
    pad until then, so the original schedule resumes seamlessly. Never slower than the original: reroutes are only
    proposed when no later.
    """
    aid, vt = last["aircraft_id"], last["vehicle_type"]
    pos0 = (last["position"]["lat"], last["position"]["lon"])
    pts = [(pos0[0], pos0[1], last["position"]["alt_msl_m"])] + [(w[0], w[1], w[2]) for w in waypoints]
    legs = [(pts[i], pts[i + 1], _dist_m(pts[i][:2], pts[i + 1][:2])) for i in range(len(pts) - 1)]
    total = sum(d for *_, d in legs)
    gs = max(float(last["motion"]["ground_speed_mps"]), 20.0)
    n = max(1, math.ceil(total / gs))
    end_alt = pts[-1][2] if pad_alt_m is None else pad_alt_m

    def at(dist: float) -> tuple[float, float, float, float]:
        for a, b, d in legs:
            if dist <= d or (a, b, d) == legs[-1]:
                f = 0.0 if d == 0 else min(1.0, dist / d)
                return (
                    a[0] + f * (b[0] - a[0]),
                    a[1] + f * (b[1] - a[1]),
                    a[2] + f * (b[2] - a[2]),
                    _bearing(a[:2], b[:2]),
                )
            dist -= d
        a, b, _ = legs[-1]
        return b[0], b[1], b[2], _bearing(a[:2], b[:2])

    def rec(lat: float, lon: float, alt: float, gs_: float, course: float, vs: float, flight: Any) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "message_id": str(uuid4()),
            "aircraft_id": aid,
            "vehicle_type": vt,
            "timestamp": last["timestamp"],  # stamp() sets the wall time when sent
            "position": {"lat": lat, "lon": lon, "alt_msl_m": alt},
            "motion": {"ground_speed_mps": gs_, "course_deg": course, "vertical_speed_mps": vs},
            "fix": {"type": "3d", "hdop": 0.9, "satellites": 11, "replayed": False},
            "active_flight_id": flight,
        }

    out: list[Action] = []
    flight = last.get("active_flight_id")
    for k in range(1, n + 1):
        lat, lon, alt, course = at(min(total, k * gs))
        left_s = n - k
        if left_s < DESCENT_S:  # descend linearly to the pad over the last minute
            f = 1.0 - left_s / DESCENT_S
            alt = alt + f * (end_alt - alt)
        vs = 0.0 if left_s >= DESCENT_S else (end_alt - pts[-1][2]) / DESCENT_S
        out.append(
            Action(
                now_offset + k / speed,
                "telemetry",
                aid,
                record=rec(lat, lon, alt, gs, course, vs, flight),
                synthetic=True,
            )
        )
    land = now_offset + n / speed
    out.append(Action(land + 2e-3, "landed", aid, synthetic=True))
    if hold_until_offset is not None:
        lat, lon = pts[-1][0], pts[-1][1]
        course = out[-2].record["motion"]["course_deg"] if len(out) > 1 and out[-2].record else 0.0
        t = land + 1.0 / speed
        while t < hold_until_offset:
            out.append(Action(t, "ground", aid, record=rec(lat, lon, end_alt, 0.0, course, 0.0, None), synthetic=True))
            t += 1.0 / speed
    return out


def ground_records(result: SimulationResult) -> list[dict]:
    """Telemetry for aircraft on a pad (parked or turnaround), one per aircraft per step, from the run's truth.

    Empty when the run did not record truth. Position is the pad; speed 0; the heading it last had.
    """
    start = datetime.fromisoformat(result.scenario.start_time)
    types = {r["aircraft_id"]: r["vehicle_type"] for r in result.telemetry}
    types |= {aid: ac.vehicle_type for aid, ac in result.final_world.aircraft.items()}
    out = []
    for row in result.truth:
        if row["status"] not in ON_PAD:
            continue
        out.append(
            {
                "schema_version": SCHEMA_VERSION,
                "message_id": str(
                    uuid5(NAMESPACE_OID, f"{row['aircraft_id']}@{row['t']}")
                ),  # stamp() renews it when sent
                "aircraft_id": row["aircraft_id"],
                "vehicle_type": types[row["aircraft_id"]],
                "timestamp": _iso(start + timedelta(seconds=row["t"])),
                "position": {"lat": row["lat"], "lon": row["lon"], "alt_msl_m": row["alt_msl_m"]},
                "motion": {
                    "ground_speed_mps": 0.0,
                    "course_deg": row["heading_deg"] % 360.0,
                    "vertical_speed_mps": 0.0,
                },
                "fix": {"type": "3d", "hdop": 0.9, "satellites": 11, "replayed": False},
                "active_flight_id": None,
            }
        )
    return sorted(out, key=_ts)


def build_schedule(result: SimulationResult, speed: float = 1.0) -> list[Action]:
    """Telemetry, departure reports and arrival ("landed") messages of one offline run, in send order.

    Pure: the same result gives the same schedule. A departure is reported just after the
    aircraft's first telemetry (the backend needs a position to snap the origin); an arrival ends
    the aircraft after its last telemetry of that flight.
    """
    recs = sorted(result.telemetry, key=_ts)
    sim_start = datetime.fromisoformat(result.scenario.start_time)
    grounded = ground_records(result)
    if not recs and not grounded:
        return []
    t0 = min(_ts(r) for r in [*recs[:1], *grounded[:1]])

    def offset(rec: dict) -> float:
        return (_ts(rec) - t0).total_seconds() / speed

    def event_offset(e: dict) -> float:
        return (sim_start + timedelta(seconds=e["t"]) - t0).total_seconds() / speed

    actions = [Action(offset(r), "telemetry", r["aircraft_id"], record=r) for r in recs]
    tel: dict[str, list[tuple[float, dict]]] = {}
    for a in actions:
        tel.setdefault(a.aircraft_id, []).append((a.offset_s, a.record or {}))
    actions += [
        Action(offset(r), "ground", r["aircraft_id"], record=r) for r in grounded
    ]  # after `tel` (airborne only)
    deps: dict[str, list[float]] = {}
    for e in result.events:
        if e["kind"] == "departure" and e.get("aircraft_id") in tel:
            aid, off = e["aircraft_id"], event_offset(e)
            deps.setdefault(aid, []).append(off)
            first = next(((o, r) for o, r in tel[aid] if o >= off - 1e-6), None)
            if first is not None:
                actions.append(
                    Action(first[0] + 1e-3, "depart", aid, path=tuple(e["path"]), vehicle_type=first[1]["vehicle_type"])
                )
    for e in result.events:
        if e["kind"] == "arrival" and e.get("aircraft_id") in tel:
            aid, off = e["aircraft_id"], event_offset(e)
            next_dep = min((d for d in deps.get(aid, []) if d > off), default=float("inf"))
            last = max((o for o, _ in tel[aid] if off <= o < next_dep), default=off)
            actions.append(Action(max(off, last) + 2e-3, "landed", aid))
    return sorted(actions, key=lambda a: a.offset_s)  # stable: telemetry stays ahead of the report it triggers


# --- the live run ---------------------------------------------------------------------------------------------
def option_path(origin: str, route: dict) -> list[str]:
    """The path of a route-response option: the origin the sim flies from + the nodes it reaches."""
    return [origin, *(e["node"] for e in route["eta"])]


class SimLive:
    """Replays scenarios through ``publish`` and reads the backend's answers via ``on_message``."""

    def __init__(
        self,
        network: Network,
        publish: Publish,
        scenarios_dir: Path = SCENARIOS,
        seed: int | None = None,
        speed: float = 1.0,
        loop: bool = False,
        response_timeout_s: float = RESPONSE_TIMEOUT_S,
    ):
        self.network = network
        self.publish = publish
        self.scenarios_dir = scenarios_dir
        self.seed, self.speed, self.loop = seed, speed, loop
        self.response_timeout_s = response_timeout_s
        self.scenario_name = ""
        self.known: set[str] = set()  # aircraft the backend has been told about
        self.pending: dict[str, asyncio.Future[dict]] = {}
        self.sent: dict[str, int] = dict.fromkeys(
            ("telemetry", "ground", "plans_selected", "plans_reported", "landed", "offline"), 0
        )
        self._replay: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        # Simulated pilots (contract §8): disclaimer acks, reroute answers, and the live schedule they can change.
        self.acceptance_rate = 1.0
        self.acked: set[str] = set()  # aircraft whose pilot acknowledged the AI disclaimer this replay
        self.last_rec: dict[str, dict] = {}  # latest telemetry per aircraft (where a reroute starts from)
        self.cut: dict[str, tuple[float, float]] = {}  # aircraft -> (from, until): original actions to skip
        self.answered: set[str] = set()  # reroute ids already answered
        self.withdrawn: set[str] = set()  # approved reroutes ATC withdrew (dismissed/expired) before the pilot answered
        self._heap: list[tuple[float, int, Action]] = []
        self._seq = itertools.count()
        self._wall0 = 0.0
        self._schedule: list[Action] = []
        self.sent["ai_ack"] = self.sent["reroute_ack"] = self.sent["reroute_unable"] = 0

    # --- scenarios ------------------------------------------------------------------------------------------
    def simulate(self, name: str, path: Path | None = None) -> SimulationResult:
        scenario = load_scenario(path or self.scenarios_dir / f"{name}.yaml")
        log.info(
            "running %s offline (seed %s, %.0f s)...", scenario.name, self.seed or scenario.seed, scenario.duration_s
        )
        return run_simulation(self.network, scenario, self.seed, None, True)  # truth: where parked aircraft sit

    async def start(self, name: str, path: Path | None = None) -> None:
        result = await asyncio.to_thread(self.simulate, name, path)
        self.scenario_name = name
        self.acceptance_rate = float(getattr(result.scenario, "suggestion_acceptance_rate", 1.0))
        log.info(
            "%d telemetry records from %d aircraft",
            len(result.telemetry),
            len({r["aircraft_id"] for r in result.telemetry}),
        )
        self._replay = asyncio.create_task(self._replay_loop(result))

    async def switch(self, name: str) -> bool:
        """Stop, mark every reported aircraft offline, simulate ``name`` and replay it. False if unknown."""
        path = self.scenarios_dir / f"{name}.yaml"
        if not path.exists():
            log.warning("unknown scenario %r ignored", name)
            return False
        await self.stop()
        for aid in sorted(self.known):
            await self._offline(aid)
        await self.start(name, path)
        return True

    async def stop(self) -> None:
        tasks = [t for t in [self._replay, *self._tasks] if t is not None and t is not asyncio.current_task()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._replay, self.pending = None, {}
        self._tasks.clear()

    async def wait(self) -> None:
        """Until the current replay has finished (never, with ``loop``); follows scenario switches."""
        while True:
            task = self._replay
            if task is None:  # between stop() and start() of a switch
                await asyncio.sleep(0.05)
                continue
            await asyncio.wait([task])
            if self._replay is task:
                return

    # --- replay -------------------------------------------------------------------------------------------------
    async def _replay_loop(self, result: SimulationResult) -> None:
        schedule = build_schedule(result, self.speed)
        while True:
            await self.run_schedule(schedule)
            if not self.loop:
                return
            for aid in sorted(self.known):
                await self._offline(aid)

    async def run_schedule(self, schedule: list[Action]) -> None:
        """Send ``schedule`` paced on the wall clock. A priority queue, so a simulated pilot's acknowledged reroute can
        insert its new flight while the original remainder of that flight is skipped (``self.cut``)."""
        start_wall, start = time.monotonic(), datetime.now(UTC)
        self._wall0, self._schedule = start_wall, schedule
        self.cut, self.acked, self.answered, self.last_rec = {}, set(), set(), {}
        self._heap = [(a.offset_s, next(self._seq), a) for a in schedule]
        heapq.heapify(self._heap)
        while self._heap:
            off, _, a = heapq.heappop(self._heap)
            window = self.cut.get(a.aircraft_id)
            if window and not a.synthetic and window[0] < off <= window[1]:
                continue  # the original flight this aircraft's pilot rerouted away from
            delay = a.offset_s - (time.monotonic() - start_wall)
            if delay > 0:
                await asyncio.sleep(delay)
            if a.kind in ("telemetry", "ground") and a.record is not None:
                rec = stamp(a.record, start + timedelta(seconds=a.offset_s))
                self.known.add(a.aircraft_id)
                await self.publish(f"aerolink/sim/{a.aircraft_id}/telemetry", json.dumps(rec).encode())
                self.sent[a.kind] += 1
                if a.kind == "telemetry":
                    self.last_rec[a.aircraft_id] = rec
                if a.aircraft_id not in self.acked:
                    await self.ack_disclaimer(a.aircraft_id)
            elif a.kind == "depart":
                task = asyncio.create_task(self.report_plan(a))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:  # landed
                await self.publish(f"aerolink/sim/{a.aircraft_id}/status", b"landed")
                self.sent["landed"] += 1

    def now_offset(self) -> float:
        return time.monotonic() - self._wall0

    # --- simulated pilots follow AeroLink's rules (contract §8) -------------------------------------------------
    async def ack_disclaimer(self, aircraft_id: str) -> None:
        """Before its first flight a simulated pilot acknowledges the AI disclaimer, like a real one."""
        self.acked.add(aircraft_id)
        msg = {"aircraft_id": aircraft_id, "version": "current", "timestamp": _iso(datetime.now(UTC))}
        await self.publish(f"aerolink/sim/{aircraft_id}/ai-ack", json.dumps(msg).encode())
        self.sent["ai_ack"] += 1

    async def answer_reroute(self, reroute: dict) -> None:
        """Answer an ATC-approved reroute after a human-like delay: acknowledge (and fly it) or unable.

        Seeded per reroute, accepting with the scenario's ``suggestion_acceptance_rate`` (no tuning)."""
        rid, aid = str(reroute.get("id")), str(reroute.get("aircraft_id"))
        if reroute.get("status") in ("dismissed", "expired"):
            self.withdrawn.add(rid)  # withdrawn: a pending answer is dropped
            return
        if rid in self.answered or reroute.get("status") != "approved":
            return
        self.answered.add(rid)
        rng = random.Random(f"{self.seed}:{rid}")
        await asyncio.sleep(rng.uniform(*PILOT_DELAY_S) / self.speed)
        if rid in self.withdrawn:
            log.info("reroute %s for %s withdrawn before the pilot answered", rid, aid)
            return
        response = "acknowledge" if rng.random() < self.acceptance_rate else "unable"
        msg = {"aircraft_id": aid, "reroute_id": rid, "response": response, "timestamp": _iso(datetime.now(UTC))}
        await self.publish(f"aerolink/sim/{aid}/reroute-response", json.dumps(msg).encode())
        self.sent["reroute_ack" if response == "acknowledge" else "reroute_unable"] += 1
        if response == "acknowledge":
            self.fly_reroute(aid, reroute)

    def fly_reroute(self, aid: str, reroute: dict) -> bool:
        """Replace the rest of this aircraft's current flight with the acknowledged route. False if it can't."""
        last = self.last_rec.get(aid)
        wps = (reroute.get("proposed") or {}).get("waypoints") or []
        if last is None or not wps:
            log.warning("cannot fly reroute %s for %s (no position or waypoints)", reroute.get("id"), aid)
            return False
        now = self.now_offset()
        landing = next(
            (a for a in self._schedule if a.aircraft_id == aid and a.kind == "landed" and a.offset_s > now), None
        )
        pad = next(
            (
                a
                for a in self._schedule
                if landing
                and a.aircraft_id == aid
                and a.kind == "ground"
                and a.offset_s > landing.offset_s
                and a.record
            ),
            None,
        )
        pad_alt = pad.record["position"]["alt_msl_m"] if pad and pad.record else None
        hold_until = landing.offset_s if landing else None
        for a in reroute_actions(last, wps, now, self.speed, pad_alt, hold_until):
            heapq.heappush(self._heap, (a.offset_s, next(self._seq), a))
        self.cut[aid] = (now, landing.offset_s if landing else math.inf)
        log.info("%s flies reroute %s (%d waypoints)", aid, reroute.get("id"), len(wps))
        return True

    async def _offline(self, aircraft_id: str) -> None:
        self.known.discard(aircraft_id)
        await self.publish(f"aerolink/sim/{aircraft_id}/status", b"offline")
        self.sent["offline"] += 1

    # --- reporting plans ----------------------------------------------------------------------------------------
    async def report_plan(self, a: Action) -> None:
        """route-request, then route-selection of the option that is the flown path (else flight-plan)."""
        path, aid = list(a.path), a.aircraft_id
        request_id = uuid4()
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self.pending[str(request_id)] = fut
        now = _iso(datetime.now(UTC))
        await self.publish(
            f"aerolink/sim/{aid}/route-request",
            json.dumps(
                {
                    "schema_version": "4.0",
                    "message_id": str(uuid4()),
                    "request_id": str(request_id),
                    "aircraft_id": aid,
                    "vehicle_type": a.vehicle_type,
                    "timestamp": now,
                    "destination_node": path[-1],
                }
            ).encode(),
        )
        resp: dict | None = None
        try:
            resp = await asyncio.wait_for(fut, self.response_timeout_s)
        except TimeoutError:
            log.warning("no route-response for %s; reporting its path directly", aid)
        finally:
            self.pending.pop(str(request_id), None)
        route = next((r for r in (resp or {}).get("routes", []) if option_path(path[0], r) == path), None)
        if route is not None:
            await self.publish(
                f"aerolink/sim/{aid}/route-selection",
                json.dumps(
                    {
                        "schema_version": "4.0",
                        "message_id": str(uuid4()),
                        "request_id": str(request_id),
                        "aircraft_id": aid,
                        "route_id": route["route_id"],
                        "timestamp": _iso(datetime.now(UTC)),
                    }
                ).encode(),
            )
            self.sent["plans_selected"] += 1
            return
        await self.publish(
            f"aerolink/sim/{aid}/flight-plan",
            json.dumps(
                {
                    "schema_version": "4.0",
                    "message_id": str(uuid4()),
                    "aircraft_id": aid,
                    "vehicle_type": a.vehicle_type,
                    "request_id": str(request_id),
                    "path": path,
                }
            ).encode(),
        )
        self.sent["plans_reported"] += 1

    # --- messages from the backend --------------------------------------------------------------------------------
    def on_message(self, topic: str, payload: bytes) -> None:
        if topic == CONTROL_TOPIC:
            try:
                name = json.loads(payload)["scenario"]
            except (ValueError, KeyError, TypeError):
                log.warning("bad control message %r", payload[:100])
                return
            task = asyncio.create_task(self.switch(str(name)))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif topic.endswith("/reroute"):
            try:
                reroute = json.loads(payload)
            except ValueError:
                return
            task = asyncio.create_task(self.answer_reroute(reroute))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif topic.endswith("/route-response"):
            try:
                resp = json.loads(payload)
            except ValueError:
                return
            fut = self.pending.get(str(resp.get("request_id")))
            if fut is not None and not fut.done():
                fut.set_result(resp)


async def publish(records: list[dict], speed: float, client: aiomqtt.Client) -> int:
    """Telemetry only (kept for scripts): the paced replay without plan reports."""
    start_wall = time.monotonic()
    plan = restamp(records, datetime.now(UTC), speed)
    for offset, rec in plan:
        delay = offset - (time.monotonic() - start_wall)
        if delay > 0:
            await asyncio.sleep(delay)
        await client.publish(f"aerolink/sim/{rec['aircraft_id']}/telemetry", json.dumps(rec), qos=1)
    return len(plan)


async def main_async(a: argparse.Namespace) -> None:
    probe = load_scenario(a.scenario)
    network = Network.from_geojson(REPO / (probe.network_file or "airspace/central-fl-v1.geojson"))
    e = os.environ
    async with aiomqtt.Client(
        e.get("MQTT_HOST", "localhost"),
        int(e.get("MQTT_PORT", "1883")),
        username=e.get("MQTT_SIM_USERNAME", "sim"),
        password=e.get("MQTT_SIM_PASSWORD") or None,
        identifier="aerolink-sim",
    ) as client:

        async def send(topic: str, payload: bytes) -> None:
            await client.publish(topic, payload, qos=1)

        sim = SimLive(network, send, a.scenarios_dir, a.seed, a.speed, a.loop)
        await client.subscribe("aerolink/sim/+/route-response", qos=1)
        await client.subscribe("aerolink/sim/+/reroute", qos=1)  # ATC-approved reroutes for simulated pilots
        await client.subscribe(CONTROL_TOPIC, qos=1)
        await sim.start(a.scenario.stem, a.scenario)

        async def listen() -> None:
            async for m in client.messages:
                sim.on_message(m.topic.value, bytes(m.payload) if isinstance(m.payload, bytes | bytearray) else b"")

        listener = asyncio.create_task(listen())
        try:
            await sim.wait()
        finally:
            listener.cancel()
            await sim.stop()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scenario", type=Path, default=SCENARIOS / "nominal_day.yaml")
    ap.add_argument("--scenarios-dir", type=Path, default=SCENARIOS, help="where scenario switching finds NAME.yaml")
    ap.add_argument("--seed", type=int, default=int(os.environ["SIM_SEED"]) if os.environ.get("SIM_SEED") else None)
    ap.add_argument("--speed", type=float, default=1.0, help="time multiplier (10 = ten times faster)")
    ap.add_argument("--loop", action="store_true", help="replay forever")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main_async(ap.parse_args(argv)))


if __name__ == "__main__":
    main()
