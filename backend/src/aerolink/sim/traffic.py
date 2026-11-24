"""Per step spatial traffic index (PLAN E3.3 T0 / T1).

Built once per step in sorted id order, deterministic, and used for separation
measurement and for finding the aircraft ahead on the same edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aerolink.network import Edge
from aerolink.sim.state import AircraftState


@dataclass
class Track:
    """One aircraft's position as seen by the traffic index."""

    aircraft_id: str
    edge: Edge
    start_node: str
    end_node: str
    distance_along_edge_m: float
    alt_msl_m: float
    velocity_mps: float
    heading_deg: float
    vertical_speed_mps: float
    status: str = "cruise"


@dataclass
class EdgeTraffic:
    """All aircraft currently on one oriented edge."""

    edge: Edge
    start_node: str
    end_node: str
    tracks: list[Track] = field(default_factory=list)


class TrafficIndex:
    """Snapshot of airborne aircraft keyed by oriented edge."""

    def __init__(
        self,
        aircraft: dict[str, AircraftState],
    ):
        self.edges: dict[tuple[str, str, str], EdgeTraffic] = {}
        self.by_aircraft: dict[str, Track] = {}
        for aid in sorted(aircraft):
            ac = aircraft[aid]
            if not ac.airborne or ac.leg is None:
                continue
            leg = ac.leg
            self._add(
                Track(
                    aircraft_id=aid,
                    edge=leg.edge,
                    start_node=leg.start_node,
                    end_node=leg.end_node,
                    distance_along_edge_m=ac.distance_along_edge_m,
                    alt_msl_m=ac.position.alt_msl_m,
                    velocity_mps=ac.velocity_mps,
                    heading_deg=ac.heading_deg,
                    vertical_speed_mps=ac.vertical_speed_mps,
                    status=ac.status,
                )
            )
        self.background: list[Track] = []

    def add_background(self, tracks: list[Track]) -> None:
        """Merge non-cooperative background tracks into the index."""
        for tr in tracks:
            self._add(tr)
            self.background.append(tr)

    def _add(self, tr: Track) -> None:
        key = (tr.edge.edge_id, tr.start_node, tr.end_node)
        if key not in self.edges:
            self.edges[key] = EdgeTraffic(edge=tr.edge, start_node=tr.start_node, end_node=tr.end_node)
        self.edges[key].tracks.append(tr)
        self.by_aircraft[tr.aircraft_id] = tr

    def tracks_on_edge(self, edge: Edge, start_node: str, end_node: str) -> list[Track]:
        bucket = self.edges.get((edge.edge_id, start_node, end_node))
        return list(bucket.tracks) if bucket else []
