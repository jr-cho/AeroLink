"""Stand model (hardening C9, finding M5): after landing an aircraft clears the pad
to a stand, so a parked aircraft no longer blocks arrivals."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

import pytest
from test_sim_controller import make_controller, run_until

from aerolink.network import Network
from aerolink.sim import events as evlib
from aerolink.sim import pads as padlib
from aerolink.sim.controller import Controller
from aerolink.sim.invariants import InvariantChecker
from aerolink.sim.runner import run_simulation
from aerolink.sim.scenarios import DEFAULT_NETWORK, load_scenario
from aerolink.sim.world_config import WorldToggles

ROOT = Path(__file__).resolve().parents[1]
NOMINAL = ROOT / "scenarios" / "nominal_day.yaml"
PRE_STANDS_GOLDEN = "ca0015d73ffc3eb384d9a263a09169e2f7305a54ee3c6229fda901a0b85242cc"  # noqa: E501


def stands(ctl: Controller, **counts: int) -> Controller:
    for node, n in counts.items():
        ctl.world.pads[node].stands = n
    return ctl


def events(ctl: Controller, kind: str, aid: str | None = None) -> list[dict]:
    return [e for e in ctl.world.events if e["kind"] == kind and (aid is None or e.get("aircraft_id") == aid)]


def checker(ctl: Controller) -> tuple[InvariantChecker, Callable[[Controller], None]]:
    chk = InvariantChecker(ctl.network, ctl.cfg.dt_s)
    chk.check_step(ctl.world)
    return chk, lambda c: chk.check_step(c.world)


def steps(ctl: Controller, n: int, check=None) -> None:
    for _ in range(n):
        ctl.step()
        if check:
            check(ctl)


# ------------------------------------------------------------------ pad state
def test_free_for_departure_respects_queue():
    pad = padlib.PadState(node_id="x", capacity=1, stands=1)
    assert pad.free_for_departure()
    pad.queue.append("arrival")
    assert not pad.free_for_departure()  # the free slot is the arrival's
    pad.queue.clear()
    pad.occupants.append("other")
    assert not pad.free_for_departure()


def test_park_prefers_stand_then_pad():
    pad = padlib.PadState(node_id="x", capacity=1, stands=1)
    assert padlib.park(pad, "a1") is True
    assert padlib.park(pad, "a2") is False
    assert pad.stand_occupants == ["a1"] and pad.occupants == ["a2"]
    with pytest.raises(RuntimeError, match="full"):
        padlib.park(pad, "a3")
    with pytest.raises(RuntimeError, match="no free stand"):
        padlib.move_to_stand(pad, "a2")


def test_default_stand_counts(line_network: Network):
    pads = padlib.init_pads(line_network, default_stand_count=True)
    assert pads["c"].stands == 2  # eVTOL site: one stand per pad
    assert pads["h"].stands == 1  # helipad
    assert padlib.init_pads(line_network)["c"].stands == 0  # pad-only model
    assert padlib.init_pads(line_network, stand_overrides={"c": 5})["c"].stands == 5
    with pytest.raises(ValueError, match="unknown nodes"):
        padlib.init_pads(line_network, stand_overrides={"nope": 1})
    with pytest.raises(ValueError, match="stands >= 0"):
        padlib.init_pads(line_network, stand_overrides={"c": -1})


# ------------------------------------------------------------------ clear-off
def test_clears_to_stand_after_pad_clear_time(line_network: Network):
    ctl = stands(make_controller(line_network, {"p1": ("passenger", "a")}), b=1)
    chk, check = checker(ctl)
    assert ctl.request_trip("p1", "b")
    run_until(ctl, lambda c: bool(events(c, evlib.ARRIVAL)), check=check)
    touchdown = ctl.world.t
    pad = ctl.world.pads["b"]
    assert pad.occupants == ["p1"]
    run_until(ctl, lambda c: bool(events(c, evlib.STAND_IN)), check=check)
    assert ctl.world.t - touchdown == pytest.approx(padlib.PAD_CLEAR_S, abs=ctl.cfg.dt_s)
    ac = ctl.world.aircraft["p1"]
    assert ac.on_stand and ac.status == "turnaround"  # turnaround continues on the stand
    assert pad.occupants == [] and pad.stand_occupants == ["p1"]
    assert chk.ok, chk.violations


def test_stays_on_pad_while_stands_full_then_moves(line_network: Network):
    ctl = stands(make_controller(line_network, {"p1": ("passenger", "a")}), b=1)
    ctl.world.pads["b"].stand_occupants.append("ghost")
    assert ctl.request_trip("p1", "b")
    run_until(ctl, lambda c: bool(events(c, evlib.ARRIVAL)))
    steps(ctl, int(3 * padlib.PAD_CLEAR_S))
    assert events(ctl, evlib.STAND_IN) == []
    assert ctl.world.pads["b"].occupants == ["p1"]
    ctl.world.pads["b"].stand_occupants.remove("ghost")
    ctl.step()
    assert len(events(ctl, evlib.STAND_IN)) == 1 and ctl.world.aircraft["p1"].on_stand


def test_no_stands_keeps_pad(line_network: Network):
    ctl = make_controller(line_network, {"p1": ("passenger", "a")})  # stands 0
    assert ctl.request_trip("p1", "b")
    run_until(ctl, lambda c: c.world.aircraft["p1"].status == "parked")
    steps(ctl, 100)
    assert ctl.world.pads["b"].occupants == ["p1"] and events(ctl, evlib.STAND_IN) == []


@pytest.mark.parametrize(("n_stands", "lands_at"), [(0, "c"), (1, "b")])
def test_second_arrival_lands_once_first_is_on_stand(line_network: Network, n_stands: int, lands_at: str):
    """b has one pad. Without a stand p1 blocks it and p2 diverts to c; with a
    stand p1 clears off and p2 lands at b."""
    ctl = stands(
        make_controller(line_network, {"p1": ("passenger", "a"), "p2": ("passenger", "c")}, max_hold_s=120),
        b=n_stands,
    )
    chk, check = checker(ctl)
    assert ctl.request_trip("p1", "b")
    run_until(ctl, lambda c: bool(events(c, evlib.ARRIVAL, "p1")), check=check)
    assert ctl.request_trip("p2", "b")
    run_until(ctl, lambda c: bool(events(c, evlib.ARRIVAL, "p2")), check=check)
    assert ctl.world.aircraft["p2"].current_node == lands_at
    assert bool(events(ctl, evlib.DIVERSION, "p2")) is (n_stands == 0)
    assert chk.ok, chk.violations


# ------------------------------------------------------------------ departures
def _on_stand_at_a(line_network: Network) -> Controller:
    ctl = stands(make_controller(line_network, {"p1": ("passenger", "a")}), a=1)
    pad = ctl.world.pads["a"]
    padlib.move_to_stand(pad, "p1")
    ctl.world.aircraft["p1"].on_stand = True
    return ctl


def test_departure_waits_for_pad_and_logs_once(line_network: Network):
    ctl = _on_stand_at_a(line_network)
    pad = ctl.world.pads["a"]
    padlib.occupy(pad, "ghost")
    ac = ctl.world.aircraft["p1"]
    ac.next_trip_s = 0.0
    chk, check = checker(ctl)
    steps(ctl, 300, check)
    assert ac.status == "parked" and ac.on_stand
    assert len(events(ctl, evlib.DEPARTURE_WAIT)) == 1
    assert events(ctl, evlib.TRIP_REJECTED) == []  # the trip stays due, it is not dropped
    padlib.release(pad, "ghost")
    ctl.step()
    check(ctl)
    assert [e["kind"] for e in ctl.world.events[-2:]] == [evlib.DEPARTURE_RELEASED, evlib.DEPARTURE]
    assert ac.status == "climbing" and not ac.on_stand
    assert pad.stand_occupants == [] and pad.occupants == []
    assert chk.ok, chk.violations


def test_departure_yields_to_queued_arrival(line_network: Network):
    ctl = _on_stand_at_a(line_network)
    ctl.world.pads["a"].queue.append("arrival")
    assert not ctl.request_trip("p1", "b")
    assert len(events(ctl, evlib.DEPARTURE_WAIT)) == 1
    ctl.world.pads["a"].queue.clear()
    assert ctl.request_trip("p1", "b")
    assert events(ctl, evlib.DEPARTURE_RELEASED)


def test_medical_return_waits_on_stand(line_network: Network):
    ctl = stands(make_controller(line_network, {"m1": ("medical", "h2")}), h2=1)
    ctl.world.aircraft["m1"].home_node = "h"
    pad = ctl.world.pads["h2"]
    padlib.move_to_stand(pad, "m1")
    ctl.world.aircraft["m1"].on_stand = True
    padlib.occupy(pad, "ghost")
    steps(ctl, 50)
    assert ctl.world.aircraft["m1"].status == "parked"
    assert len(events(ctl, evlib.DEPARTURE_WAIT)) == 1
    assert events(ctl, evlib.ROUTE_UNAVAILABLE) == []
    padlib.release(pad, "ghost")
    ctl.step()
    assert ctl.world.aircraft["m1"].status == "climbing"


# ------------------------------------------------------------------ full runs
@pytest.fixture(scope="module")
def network() -> Network:
    return Network.from_geojson(DEFAULT_NETWORK)


def no_stands(network: Network) -> dict[str, int]:
    return {nid: 0 for nid, props in network.nodes.items() if "waypoint" not in props.get("types", [])}


def test_zero_stands_reproduces_pre_stand_digest(network: Network):
    scenario = dataclasses.replace(load_scenario(NOMINAL), stand_capacity=no_stands(network), world=WorldToggles())
    result = run_simulation(network, scenario, seed=42)
    assert result.digest() == PRE_STANDS_GOLDEN


def test_stands_reduce_holding(network: Network):
    base = dataclasses.replace(load_scenario(NOMINAL), duration_s=6 * 3600, world=WorldToggles(separation=True))
    starved = dataclasses.replace(base, stand_capacity=no_stands(network))

    def count(sc, kind: str) -> int:
        res = run_simulation(network, sc, seed=1, record_truth=False, check_invariants=True)
        assert res.violations == []
        return sum(e["kind"] == kind for e in res.events)

    assert count(base, evlib.HOLD_START) < count(starved, evlib.HOLD_START)
