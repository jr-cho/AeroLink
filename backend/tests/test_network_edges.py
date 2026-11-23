"""Edge cases for E3.1 network routing found in review."""

import pytest
from conftest import make_network, site, wp

from aerolink.network import Edge, Network
from aerolink.vehicle import vehicle_cfg


def test_edges_are_hashable_and_immutable(fl_network: Network):
    edge = next(iter(fl_network.edges.values()))
    assert isinstance(edge.geometry, tuple)
    assert {edge, edge} == {edge}
    with pytest.raises(AttributeError):
        edge.length_m = 1.0  # ty: ignore[invalid-assignment]


def test_both_directions_share_one_edge(fl_network: Network):
    for (u, v), e in fl_network.edges.items():
        assert fl_network.edges[(v, u)] is e
        assert {e.source, e.target} == {u, v}


def test_edge_other_and_orientation(fl_network: Network):
    e = next(iter(fl_network.edges.values()))
    assert e.other(e.source) == e.target
    assert e.other(e.target) == e.source
    assert e.oriented_geometry(e.source) == e.geometry
    assert e.oriented_geometry(e.target) == tuple(reversed(e.geometry))
    with pytest.raises(ValueError):
        e.other("not-a-node")
    with pytest.raises(ValueError):
        e.oriented_geometry("not-a-node")


def test_geometry_starts_at_source_node(fl_network: Network):
    for e in set(fl_network.edges.values()):
        lon, lat = e.geometry[0]
        src = fl_network.nodes[e.source]
        assert abs(lon - src["lon"]) < 1e-3 and abs(lat - src["lat"]) < 1e-3, e.edge_id


def test_same_source_and_target_returns_no_route(fl_network: Network):
    assert fl_network.shortest_paths("ev-orl", "ev-orl", vehicle_type="passenger") == []


@pytest.mark.parametrize("k", [0, -1])
def test_non_positive_k_returns_nothing(fl_network: Network, k: int):
    assert fl_network.shortest_paths("ev-orl", "ev-lal", vehicle_type="passenger", k=k) == []


def test_unknown_nodes_return_no_route(fl_network: Network):
    assert fl_network.shortest_paths("nope", "ev-lal", vehicle_type="passenger") == []
    assert fl_network.shortest_paths("ev-orl", "nope", vehicle_type="passenger") == []
    assert not fl_network.is_reachable("nope", "ev-lal")


def test_paths_are_sorted_distinct_simple_and_within_range(fl_network: Network):
    for vtype in ("passenger", "medical"):
        sites = sorted(fl_network.nodes_for(vtype))
        for s in sites[:4]:
            for t in sites[-4:]:
                paths = fl_network.shortest_paths(s, t, vehicle_type=vtype, k=5)
                lengths = [fl_network.route_length_m(p) for p in paths]
                assert lengths == sorted(lengths)
                assert len({tuple(p) for p in paths}) == len(paths)
                for p in paths:
                    assert p[0] == s and p[-1] == t
                    assert len(set(p)) == len(p)
                    assert fl_network.route_length_m(p) <= vehicle_cfg(vtype).range_m


def test_paths_never_transit_landing_sites(fl_network: Network):
    sites = set(fl_network.nodes_for("passenger")) | set(fl_network.nodes_for("medical"))
    for s in sorted(fl_network.nodes_for("medical"))[:5]:
        for t in sorted(fl_network.nodes_for("medical"))[-5:]:
            for p in fl_network.shortest_paths(s, t, vehicle_type="medical", k=3):
                assert not sites & set(p[1:-1])


def test_route_length_and_time_of_trivial_path(fl_network: Network):
    assert fl_network.route_length_m(["ev-orl"]) == 0.0
    assert fl_network.est_time_s(["ev-orl"], "passenger") == 0.0


def test_routing_skips_paths_through_sites_but_finds_detours():
    nodes = {
        "a": site(28.0, -81.0, ["passenger"]),
        "m": site(28.0, -80.9, ["passenger"]),  # a site in the middle of the short way
        "w": wp(28.1, -80.9),
        "b": site(28.0, -80.8, ["passenger"]),
    }
    net = make_network(nodes, [("a", "m", 1000), ("m", "b", 1000), ("a", "w", 3000), ("w", "b", 3000)])
    assert net.shortest_paths("a", "b", vehicle_type="passenger", k=3) == [["a", "w", "b"]]


def test_range_cutoff_stops_enumeration():
    nodes = {"a": site(28.0, -81.0, ["passenger"]), "w": wp(28.1, -81.0), "b": site(28.2, -81.0, ["passenger"])}
    net = make_network(nodes, [("a", "w", 100_000), ("w", "b", 100_000)])
    assert net.shortest_paths("a", "b", vehicle_type="passenger") == []


def test_disconnected_network_has_no_route():
    nodes = {"a": site(28.0, -81.0, ["passenger"]), "b": site(28.2, -81.0, ["passenger"])}
    net = make_network(nodes, [])
    assert net.shortest_paths("a", "b", vehicle_type="passenger") == []
    assert not net.is_reachable("a", "b")


def test_edge_between_unknown_pair_raises(fl_network: Network):
    with pytest.raises(KeyError):
        fl_network.edge_between("ev-orl", "ev-lal")


def test_edge_type_is_frozen_dataclass():
    e = Edge("x", "corridor", "a", "b", 1.0, 1000, 1500, None, ((0.0, 0.0), (1.0, 1.0)))
    assert hash(e) == hash(Edge("x", "corridor", "a", "b", 1.0, 1000, 1500, None, ((0.0, 0.0), (1.0, 1.0))))
