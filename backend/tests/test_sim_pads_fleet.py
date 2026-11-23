import numpy as np
import pytest

from aerolink.network import Network
from aerolink.sim import pads as padlib
from aerolink.sim.fleet import choose_destination, create_fleet, home_sites
from aerolink.sim.state import PadState


# ---------------------------------------------------------------------- pads
def test_init_pads_excludes_waypoints_and_uses_capacity(fl_network: Network):
    pads = padlib.init_pads(fl_network)
    assert len(pads) == 28
    assert all("waypoint" not in fl_network.nodes[n]["types"] for n in pads)
    assert list(pads) == sorted(pads)
    for nid, p in pads.items():
        assert p.capacity == fl_network.nodes[nid].get("pads", 1)


def test_init_pads_overrides(fl_network: Network):
    assert padlib.init_pads(fl_network, {"ev-orl": 5})["ev-orl"].capacity == 5
    with pytest.raises(ValueError):
        padlib.init_pads(fl_network, {"nope": 1})
    with pytest.raises(ValueError):
        padlib.init_pads(fl_network, {"ev-orl": 0})


def test_occupy_release_idempotent():
    p = PadState("x", 1)
    padlib.occupy(p, "a")
    padlib.occupy(p, "a")
    assert p.occupants == ["a"]
    with pytest.raises(RuntimeError):
        padlib.occupy(p, "b")
    padlib.release(p, "a")
    padlib.release(p, "a")
    assert p.occupants == [] and p.free_slots() == 1


def test_request_is_first_come_first_served():
    p = PadState("x", 1)
    assert padlib.request(p, "a")
    assert not padlib.request(p, "b")
    assert not padlib.request(p, "c")
    assert not padlib.request(p, "b")  # no duplicate queue entries
    assert p.queue == ["b", "c"]
    padlib.release(p, "a")
    # A newcomer cannot jump the queue even though a slot is free.
    assert not padlib.request(p, "d")
    assert padlib.grant_from_queue(p) == ["b"]
    assert p.queue == ["c", "d"]
    assert padlib.grant_from_queue(p) == []


def test_request_already_occupant_is_granted():
    p = PadState("x", 1, occupants=["a"])
    assert padlib.request(p, "a") and p.queue == []


def test_grant_multiple_slots_in_order():
    p = PadState("x", 3, occupants=["z"], queue=["a", "b", "c"])
    assert padlib.grant_from_queue(p) == ["a", "b"]
    assert p.queue == ["c"] and p.free_slots() == 0


def test_leave_queue():
    p = PadState("x", 1, occupants=["z"], queue=["a", "b"])
    padlib.leave_queue(p, "a")
    padlib.leave_queue(p, "missing")
    assert p.queue == ["b"]


# --------------------------------------------------------------------- fleet
def test_default_fleet_composition_and_homes(fl_network: Network):
    pads = padlib.init_pads(fl_network)
    fleet = create_fleet(fl_network, pads, np.random.default_rng(1))
    assert len(fleet) == 10
    assert sum(a.vehicle_type == "passenger" for a in fleet.values()) == 7
    assert sum(a.vehicle_type == "medical" for a in fleet.values()) == 3
    for ac in fleet.values():
        assert ac.home_node in home_sites(fl_network, ac.vehicle_type)
        assert ac.status == "parked" and ac.current_node == ac.home_node
        assert ac.aircraft_id in pads[ac.home_node].occupants
    for p in pads.values():
        assert len(p.occupants) <= p.capacity


def test_fleet_ids_are_contract_valid(fl_network: Network):
    import re

    fleet = create_fleet(fl_network, padlib.init_pads(fl_network), np.random.default_rng(1))
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9-]{1,31}", aid) for aid in fleet)


def test_fleet_is_seed_deterministic(fl_network: Network):
    def homes(seed):
        f = create_fleet(fl_network, padlib.init_pads(fl_network), np.random.default_rng(seed))
        return {a: s.home_node for a, s in f.items()}

    assert homes(3) == homes(3)
    assert any(homes(s) != homes(3) for s in range(4, 10))


def test_fleet_rng_streams_are_independent(fl_network: Network):
    f = create_fleet(fl_network, padlib.init_pads(fl_network), np.random.default_rng(1))
    draws = [a.rng.random() for a in f.values()] + [a.telemetry_rng.random() for a in f.values()]
    assert len(set(draws)) == len(draws)


def test_fleet_overflow_is_rejected(fl_network: Network):
    n_pads = sum(padlib.init_pads(fl_network)[s].capacity for s in fl_network.nodes_for("passenger"))
    create_fleet(fl_network, padlib.init_pads(fl_network), np.random.default_rng(0), passenger=n_pads, medical=0)
    with pytest.raises(ValueError):
        create_fleet(fl_network, padlib.init_pads(fl_network), np.random.default_rng(0), passenger=n_pads + 1)
    with pytest.raises(ValueError):
        create_fleet(fl_network, padlib.init_pads(fl_network), np.random.default_rng(0), passenger=-1)


def test_empty_fleet(fl_network: Network):
    assert create_fleet(fl_network, padlib.init_pads(fl_network), np.random.default_rng(0), 0, 0) == {}


def test_no_sites_for_type_is_rejected(line_network: Network):
    from conftest import make_network, site

    net = make_network({"a": site(28, -81, ["passenger"])}, [])
    with pytest.raises(ValueError):
        create_fleet(net, padlib.init_pads(net), np.random.default_rng(0), passenger=0, medical=1)


def test_choose_destination_never_current_and_type_valid(fl_network: Network):
    f = create_fleet(fl_network, padlib.init_pads(fl_network), np.random.default_rng(0))
    rng = np.random.default_rng(0)
    for ac in f.values():
        for _ in range(50):
            d = choose_destination(fl_network, ac, rng)
            assert d is not None
            assert d != ac.current_node and ac.vehicle_type in fl_network.serves(d)


def test_choose_destination_return_home(fl_network: Network):
    f = create_fleet(fl_network, padlib.init_pads(fl_network), np.random.default_rng(0))
    ac = f["sim-p00"]
    ac.current_node = next(n for n in fl_network.nodes_for("passenger") if n != ac.home_node)
    rng = np.random.default_rng(0)
    assert all(choose_destination(fl_network, ac, rng, 1.0) == ac.home_node for _ in range(10))
    ac.current_node = ac.home_node
    assert all(choose_destination(fl_network, ac, rng, 1.0) != ac.home_node for _ in range(10))


def test_choose_destination_none_when_single_site():
    from conftest import make_network, site

    net = make_network({"a": site(28, -81, ["passenger"])}, [])
    f = create_fleet(net, padlib.init_pads(net), np.random.default_rng(0), 1, 0)
    assert choose_destination(net, f["sim-p00"], np.random.default_rng(0)) is None
