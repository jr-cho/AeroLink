"""Load and query the built corridor network (PLAN E3.1).

Interface:
    net = Network.from_geojson(Path("airspace/central-fl-v1.geojson"))
    net.nodes_for("passenger")         # node ids that serve passenger
    net.shortest_paths(source, dest, vehicle_type, k=3)

The network keeps the committed geometry from `build_network.py`: nodes, corridor
edges, and connectors.  It also carries altitude bands so the simulator can place
aircraft on each edge at the correct MSL altitude.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from aerolink.contracts import VehicleType
from aerolink.vehicle import vehicle_cfg


@dataclass(frozen=True)
class Edge:
    edge_id: str
    kind: str  # "corridor" | "connector"
    source: str
    target: str
    length_m: float
    band_floor_ft: int
    band_ceiling_ft: int
    highway: str | None
    geometry: tuple[tuple[float, float], ...]

    def other(self, node: str) -> str:
        """Return the opposite endpoint of this (undirected) edge."""
        if node == self.source:
            return self.target
        if node == self.target:
            return self.source
        raise ValueError(f"{node!r} is not an endpoint of {self.edge_id}")

    def oriented_geometry(self, from_node: str) -> tuple[tuple[float, float], ...]:
        """Geometry ordered from ``from_node`` to the other endpoint."""
        if from_node == self.source:
            return self.geometry
        if from_node == self.target:
            return tuple(reversed(self.geometry))
        raise ValueError(f"{from_node!r} is not an endpoint of {self.edge_id}")


class Network:
    """Routable corridor network with vehicle-aware destination filtering."""

    def __init__(
        self,
        graph: nx.Graph,
        nodes: dict[str, dict],
        edges: dict[tuple[str, str], Edge],
    ):
        self.graph = graph
        self.nodes = nodes
        self.edges = edges
        self._site_nodes = [nid for nid, n in nodes.items() if "waypoint" not in n.get("types", [])]
        self._distance_cache: dict[tuple[str, str, str], float | None] = {}

    @classmethod
    def from_geojson(cls, path: Path) -> Network:
        data = json.loads(path.read_text())
        graph = nx.Graph()
        nodes: dict[str, dict] = {}
        edges: dict[tuple[str, str], Edge] = {}

        for f in data["features"]:
            p = f["properties"]
            geom = f["geometry"]
            if p.get("kind") == "node":
                nid = p["id"]
                # Keep node props but replace lat/lon from geometry for fidelity.
                props = {k: v for k, v in p.items() if k != "id"}
                if geom["type"] == "Point":
                    props["lon"] = geom["coordinates"][0]
                    props["lat"] = geom["coordinates"][1]
                nodes[nid] = props
                graph.add_node(nid)
            elif p.get("kind") in ("corridor", "connector"):
                src = p["from"]
                tgt = p["to"]
                edge = Edge(
                    edge_id=p["id"],
                    kind=p["kind"],
                    source=src,
                    target=tgt,
                    length_m=p["length_m"],
                    band_floor_ft=int(p["band_floor_ft"]),
                    band_ceiling_ft=int(p["band_ceiling_ft"]),
                    highway=p.get("highway"),
                    geometry=tuple((float(c[0]), float(c[1])) for c in geom["coordinates"]),
                )
                edges[(src, tgt)] = edge
                edges[(tgt, src)] = edge
                graph.add_edge(
                    src,
                    tgt,
                    length_m=p["length_m"],
                    edge_id=p["id"],
                    band_floor_ft=int(p["band_floor_ft"]),
                    band_ceiling_ft=int(p["band_ceiling_ft"]),
                )

        missing = sorted(nid for nid, p in nodes.items() if "waypoint" not in p.get("types", []) and "serves" not in p)
        if missing:
            raise ValueError(f"landing sites without 'serves': {missing}")
        return cls(graph, nodes, edges)

    def serves(self, node_id: str) -> list[str]:
        """Vehicle types served by a node (its ``serves`` property, written by
        `build_network.py`); ``[]`` for waypoints. The loader rejects landing
        sites without ``serves``."""
        return list(self.nodes[node_id].get("serves", []))

    def nodes_for(self, vehicle_type: VehicleType) -> list[str]:
        """All non-waypoint nodes that can be a destination for this vehicle type."""
        return [nid for nid in self._site_nodes if vehicle_type in self.serves(nid)]

    def is_reachable(self, source: str, target: str) -> bool:
        if source not in self.graph or target not in self.graph:
            return False
        return nx.has_path(self.graph, source, target)

    def shortest_paths(
        self,
        source: str,
        target: str,
        vehicle_type: VehicleType,
        k: int = 3,
    ) -> list[list[str]]:
        """Return up to k distinct simple paths, sorted by total length.

        Filters out any path whose total edge length exceeds the vehicle's range,
        and paths that transit another landing site. Only the *target* must serve
        ``vehicle_type``: the origin is wherever the aircraft is (ARCHITECTURE lets a
        medical aircraft be based at any site), so it is not checked.
        """
        if k <= 0 or source == target:
            return []
        if source not in self.graph or target not in self.graph:
            return []
        if vehicle_type not in self.serves(target):
            return []

        max_range = vehicle_cfg(vehicle_type).range_m
        sites = set(self._site_nodes)
        paths: list[list[str]] = []
        try:
            # shortest_simple_paths yields paths in non-decreasing length order, so
            # the first over-range path means every later one is over range too.
            for path in nx.shortest_simple_paths(self.graph, source, target, weight="length_m"):
                if self.route_length_m(path) > max_range:
                    break
                # Landing sites are endpoints only, never transit points.
                if any(n in sites for n in path[1:-1]):
                    continue
                paths.append(path)
                if len(paths) >= k:
                    break
        except nx.NetworkXNoPath:
            return []
        return paths

    def route_distance_m(self, source: str, target: str, vehicle_type: VehicleType) -> float | None:
        """Length of the shortest valid route (``shortest_paths`` rules, so within a
        full battery's range), 0 for ``source == target``, None if there is none.
        Cached: the network is immutable."""
        if source == target:
            return 0.0
        key = (source, target, vehicle_type)
        if key not in self._distance_cache:
            paths = self.shortest_paths(source, target, vehicle_type, k=1)
            self._distance_cache[key] = self.route_length_m(paths[0]) if paths else None
        return self._distance_cache[key]

    def edge_between(self, u: str, v: str) -> Edge:
        return self.edges[(u, v)]

    def route_length_m(self, path: list[str]) -> float:
        """Total ground length of a path in metres."""
        return sum(self.edges[(path[i], path[i + 1])].length_m for i in range(len(path) - 1))

    def est_time_s(self, path: list[str], vehicle_type: VehicleType) -> float:
        """Distance / cruise-speed estimate for a path."""
        return self.route_length_m(path) / vehicle_cfg(vehicle_type).cruise_speed_mps
