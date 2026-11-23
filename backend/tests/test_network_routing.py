"""Tests for vehicle-aware routing on the committed network (PLAN E3.1)."""

import dataclasses
import itertools
import json
from pathlib import Path

import networkx as nx
import pytest

from aerolink.network import Network
from aerolink.sim.physics import segment_length_m
from aerolink.vehicle import VEHICLES, VehicleType, vehicle_cfg


@pytest.fixture
def network() -> Network:
    return Network.from_geojson(Path(__file__).parents[2] / "airspace" / "central-fl-v1.geojson")


def test_network_loads(network: Network):
    assert network.nodes
    assert network.edges
    assert network.graph.number_of_edges() > 0


def test_node_serves_matches_expected_site_types(network: Network):
    assert "passenger" in network.serves("ev-water-st")
    assert "medical" in network.serves("hp-61fl")
    assert "passenger" not in network.serves("hp-61fl")
    assert "medical" not in network.serves("ev-water-st")


def test_nodes_for_passenger_only_sites(network: Network):
    passenger_nodes = network.nodes_for("passenger")
    medical_nodes = network.nodes_for("medical")
    assert "ev-water-st" in passenger_nodes
    assert "ev-water-st" not in medical_nodes
    assert "hp-61fl" in medical_nodes
    assert "hp-61fl" not in passenger_nodes


def test_passenger_cannot_route_to_helipad(network: Network):
    paths = network.shortest_paths("ev-water-st", "hp-61fl", vehicle_type="passenger", k=1)
    assert paths == []


def test_medical_can_route_to_tampa_helipad(network: Network):
    # Medical aircraft may be based at eVTOL sites and can fly to any helipad.
    paths = network.shortest_paths("ev-orl", "hp-61fl", vehicle_type="medical", k=1)
    assert paths


def test_medical_can_route_to_orlando_helipad(network: Network):
    paths = network.shortest_paths("ev-orl", "hp-fd28", vehicle_type="medical", k=1)
    assert paths


def test_tampa_to_orlando_route_is_within_range(network: Network):
    paths = network.shortest_paths("ev-water-st", "ev-orl", vehicle_type="passenger", k=1)
    assert paths
    assert network.route_length_m(paths[0]) <= 160_000.0


def test_every_vertiport_pair_fits_passenger_range(network: Network):
    # The longest pair, Sebring -> Orlando, is 153.0 km; range is 160 km.
    shortest = nx.shortest_path_length(network.graph, "ev-sef", "ev-orl", weight="length_m")
    assert shortest == pytest.approx(153_034, abs=1)
    assert shortest < vehicle_cfg("passenger").range_m
    sites = sorted(network.nodes_for("passenger"))
    for a, b in itertools.combinations(sites, 2):
        assert network.shortest_paths(a, b, vehicle_type="passenger", k=1), (a, b)


def test_range_filter_drops_real_over_range_pair(network: Network, monkeypatch: pytest.MonkeyPatch):
    # With 150 km of range, Sebring -> Orlando (153.0 km) is out of reach both ways.
    short = dataclasses.replace(VEHICLES["passenger"], range_m=150_000.0)
    monkeypatch.setitem(VEHICLES, "passenger", short)
    assert network.shortest_paths("ev-sef", "ev-orl", vehicle_type="passenger") == []
    assert network.shortest_paths("ev-orl", "ev-sef", vehicle_type="passenger") == []
    assert network.shortest_paths("ev-sef", "ev-lal", vehicle_type="passenger")


def test_range_filter_artificial_short_range(network: Network, monkeypatch: pytest.MonkeyPatch):
    short = dataclasses.replace(VEHICLES["passenger"], range_m=1.0)
    monkeypatch.setitem(VEHICLES, "passenger", short)
    assert network.shortest_paths("ev-water-st", "ev-orl", vehicle_type="passenger", k=1) == []


def test_range_filter_truncates_alternatives(network: Network):
    """Tampa -> Orlando: the I-4 path (140.5 km) fits; every alternative is over range."""
    paths = network.shortest_paths("ev-water-st", "ev-orl", vehicle_type="passenger", k=3)
    assert len(paths) == 1
    assert network.route_length_m(paths[0]) == pytest.approx(140_450, abs=1)


def test_multicandidate_routes(network: Network):
    """Tampa -> Lakeland: I-4 via Plant City first, then the Brandon/Mulberry/Bartow route."""
    paths = network.shortest_paths("ev-water-st", "ev-lal", vehicle_type="passenger", k=3)
    assert paths == [
        ["ev-water-st", "wp-downtown-tampa", "wp-i4-i75", "wp-plant-city", "wp-lakeland", "ev-lal"],
        ["ev-water-st", "wp-downtown-tampa", "wp-brandon", "wp-mulberry", "wp-bartow", "wp-lakeland", "ev-lal"],
    ]
    lengths = [network.route_length_m(p) for p in paths]
    assert lengths == pytest.approx([65_700, 100_300], abs=100)


SITES = sorted(
    nid
    for nid, p in Network.from_geojson(Path(__file__).parents[2] / "airspace" / "central-fl-v1.geojson").nodes.items()
    if "waypoint" not in p.get("types", [])
)


@pytest.fixture(scope="module")
def shared_network() -> Network:
    return Network.from_geojson(Path(__file__).parents[2] / "airspace" / "central-fl-v1.geojson")


@pytest.mark.parametrize("vehicle_type", ["passenger", "medical"])
@pytest.mark.parametrize("source", SITES)
def test_all_pairs_routing_rules(shared_network: Network, source: str, vehicle_type: VehicleType):
    """Every ordered site pair x vehicle type: type filter, range filter, simple
    paths, no landing-site transit, sorted by length, geometry matches length_m."""
    net = shared_network
    sites = set(SITES)
    max_range = vehicle_cfg(vehicle_type).range_m
    for target in SITES:
        paths = net.shortest_paths(source, target, vehicle_type=vehicle_type, k=3)
        if target == source or vehicle_type not in net.serves(target):
            assert paths == [], (source, target)
            continue
        shortest = nx.shortest_path_length(net.graph, source, target, weight="length_m")
        lengths = [net.route_length_m(p) for p in paths]
        assert lengths == sorted(lengths)
        assert all(length <= max_range for length in lengths)
        if not paths:
            # Either too far, or every path within range transits another site.
            assert shortest > max_range or all(
                any(n in sites for n in p[1:-1])
                for p in itertools.takewhile(
                    lambda p: net.route_length_m(p) <= max_range,
                    nx.shortest_simple_paths(net.graph, source, target, weight="length_m"),
                )
            )
        for p in paths:
            assert p[0] == source and p[-1] == target
            assert len(set(p)) == len(p)
            assert not any(n in sites for n in p[1:-1])
            geo = sum(
                segment_length_m(*net.edges[(u, v)].oriented_geometry(u)[i : i + 2])
                for u, v in itertools.pairwise(p)
                for i in range(len(net.edges[(u, v)].geometry) - 1)
            )
            assert geo == pytest.approx(net.route_length_m(p), rel=0.01)


def test_est_time_matches_cruise_speed(network: Network):
    from aerolink.vehicle import vehicle_cfg

    path = network.shortest_paths("ev-water-st", "ev-orl", vehicle_type="passenger", k=1)[0]
    length = network.route_length_m(path)
    est_time = network.est_time_s(path, "passenger")
    assert est_time == pytest.approx(length / vehicle_cfg("passenger").cruise_speed_mps)


def test_loader_rejects_site_without_serves(tmp_path: Path):
    def node(nid: str, types: list[str], **extra) -> dict:
        props = {"kind": "node", "id": nid, "types": types, **extra}
        return {"type": "Feature", "properties": props, "geometry": {"type": "Point", "coordinates": [-81.5, 28.0]}}

    features = [node("wp", ["waypoint"]), node("ok", ["evtol"], serves=["passenger"]), node("bad", ["helipad"])]
    path = tmp_path / "net.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    with pytest.raises(ValueError, match=r"landing sites without 'serves': \['bad'\]"):
        Network.from_geojson(path)
