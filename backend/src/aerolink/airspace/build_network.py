"""Build the AeroLink corridor network (ARCHITECTURE §3.3, PLAN E2.2/E2.3).

    uv run python -m aerolink.airspace.build_network \
        --network ../airspace/network.yaml \
        --highways ../airspace/fdot-highways.geojson \
        --constraints ../airspace/constraints-fl.geojson \
        --out ../airspace/central-fl-v1.geojson \
        --report ../airspace/build-report.txt

Inputs: hand-written connections (network.yaml), FDOT highway shapes, FAA no-go
volumes. Output: nodes + corridors with altitude bands, plus a report. Exits with
status 1 if any pair of eVTOL sites ends up with fewer than the required routes.
"""

import argparse
import itertools
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx
import shapely
import yaml
from pyproj import Transformer
from shapely.geometry import LineString, Point, mapping, shape
from shapely.geometry.base import BaseGeometry

FT_TO_M = 0.3048
_TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32617", always_xy=True)  # UTM 17N
_TO_WGS = Transformer.from_crs("EPSG:32617", "EPSG:4326", always_xy=True)
BLOCKING_AIRSPACE_FOR_EXEMPTION = {"class_b", "class_c", "class_d"}
BACKTRACK_TOL_M = 200.0  # remove_backtracks: a corridor passing this close to its own end has doubled back

# FDOT AAM Business Plan (Nov 2025) airspace zones, as AeroLink applies them
# (ARCHITECTURE §5.2):
#   green  - uncontrolled / Class E: no constraint
#   yellow - Class D (and E): "suitable with prior planning and coordination"
#            -> corridors may pass, flagged as needing ATC coordination, avoided when possible
#   orange - Class B shelves (2,000-10,000 ft): fly beneath the floor or not at all
#   pink   - Class B surface area: no corridors; only a landing site's own approach
#   red / special use (restricted, prohibited, military, MOA): no-go
DEFAULT_COORDINATION_AIRSPACE = ["class_d"]
# Designated clearance routes (network.yaml `clearance_routes`) may also cross these,
# with ATC clearance: FDOT's "mitigation or special procedures" for Class B, like
# today's helicopter routes through Class B under a letter of agreement.
DEFAULT_CLEARANCE_AIRSPACE = ["class_b"]


def to_utm(geom):
    return shapely.transform(geom, lambda xy: _xy(_TO_UTM, xy))


def to_wgs(geom):
    return shapely.transform(geom, lambda xy: _xy(_TO_WGS, xy))


def _xy(t, xy):
    x, y = t.transform(xy[:, 0], xy[:, 1])
    xy[:, 0], xy[:, 1] = x, y
    return xy


SITE_TYPES = {"evtol", "helipad"}
SERVES_BY_DEFAULT = {"evtol": "passenger", "helipad": "medical"}


@dataclass
class Volume:
    id: str
    type: str
    floor_ft: float
    ceiling_ft: float
    geom: BaseGeometry  # UTM polygon


@dataclass
class Report:
    edges: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    coordination: list = field(default_factory=list)
    clearance: list = field(default_factory=list)
    routes: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)

    def text(self) -> str:
        lines = ["AeroLink network build report", ""]
        lines.append(f"Edges kept: {len(self.edges)}")
        for e in self.edges:
            lines.append(f"  {e}")
        lines.append(f"\nEdges dropped: {len(self.dropped)}")
        for d in self.dropped:
            lines.append(f"  {d}")
        lines.append(f"\nEdges needing ATC coordination (Class D, FDOT yellow zone): {len(self.coordination)}")
        for c in self.coordination:
            lines.append(f"  {c}")
        lines.append(f"\nDesignated routes needing Class B clearance (FDOT special procedures): {len(self.clearance)}")
        for c in self.clearance:
            lines.append(f"  {c}")
        lines.append("\nRoutes between eVTOL sites:")
        for pair, n in self.routes.items():
            lines.append(f"  {pair}: {n}")
        if self.problems:
            lines.append("\nPROBLEMS:")
            lines += [f"  {p}" for p in self.problems]
        return "\n".join(lines) + "\n"


def load_volumes(path: Path) -> list[Volume]:
    data = json.loads(path.read_text())
    out = []
    for f in data["features"]:
        p = f["properties"]
        if not p.get("blocks") or not p.get("in_focus_region", True):
            continue
        out.append(Volume(p["id"], p["type"], p["floor_ft"], p["ceiling_ft"], to_utm(shape(f["geometry"]))))
    return out


def load_highways(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text())
    return {f["properties"]["highway"]: to_utm(shape(f["geometry"])) for f in data["features"]}


class HighwayGraph:
    """A highway as a routable graph, so pieces, gaps and divided sections don't matter.

    FDOT delivers each highway as many segments (split at county lines, ramps and
    divided sections). Vertices are snapped to a ~10 m grid, consecutive vertices
    become edges, and dangling ends are bridged to the nearest other piece within
    `gap_m`. A corridor is then the shortest path along the road between two nodes.
    """

    def __init__(self, geom, gap_m: float = 300.0):
        self.geom = geom
        self.g = nx.Graph()
        for part in getattr(geom, "geoms", [geom]):
            coords = [self._key(c) for c in part.coords]
            for u, v in itertools.pairwise(coords):
                if u != v:
                    self.g.add_edge(u, v, w=Point(u).distance(Point(v)))
        self.keys = list(self.g.nodes)
        self.tree = shapely.STRtree([Point(k) for k in self.keys])
        self._bridge(gap_m)

    @staticmethod
    def _key(c) -> tuple[float, float]:
        return (round(c[0] / 10) * 10.0, round(c[1] / 10) * 10.0)

    def _bridge(self, gap_m: float) -> None:
        comp = {n: i for i, part in enumerate(nx.connected_components(self.g)) for n in part}
        ends = [n for n in self.g.nodes if self.g.degree(n) == 1]
        for e in ends:
            best = None
            for i in self.tree.query(Point(e).buffer(gap_m)):
                k = self.keys[i]
                if comp[k] == comp[e]:
                    continue
                d = Point(e).distance(Point(k))
                if d <= gap_m and (best is None or d < best[0]):
                    best = (d, k)
            if best:
                self.g.add_edge(e, best[1], w=best[0])

    def nearest(self, pt: Point):
        i = self.tree.nearest(pt)
        if i is None:
            raise ValueError("empty highway geometry")
        k = self.keys[int(i)]
        return k, Point(k).distance(pt)

    def path(self, a: Point, b: Point, simplify_m: float):
        """Line from a to b along the highway, or None if the road doesn't connect them."""
        ka, _ = self.nearest(a)
        kb, _ = self.nearest(b)
        try:
            nodes = nx.shortest_path(self.g, ka, kb, weight="w")
        except nx.NetworkXNoPath:
            return None
        seg = LineString([a.coords[0], *nodes, b.coords[0]])
        return remove_backtracks(seg.simplify(simplify_m, preserve_topology=False))


def _cut_start_spur(coords: list[tuple[float, float]], tol_m: float) -> list[tuple[float, float]]:
    """Drop an out-and-back detour at the start: if a later segment passes within
    ``tol_m`` of the first point, go straight to the farthest such point."""
    p = Point(coords[0])
    for k in range(len(coords) - 2, 0, -1):
        seg = LineString(coords[k : k + 2])
        if seg.distance(p) <= tol_m:
            q = seg.interpolate(seg.project(p))
            return [coords[0], (q.x, q.y), *coords[k + 1 :]]
    return coords


def remove_backtracks(line: LineString, tol_m: float = BACKTRACK_TOL_M) -> LineString:
    """Remove out-and-back spurs at either end of a corridor.

    A waypoint that sits beside its highway can snap to a road vertex past the
    point where the corridor turns, so the simplified line overshoots and doubles
    back on itself (up to 3.4 km on central-fl-v1). Aircraft would fly the same
    stretch twice, in opposite directions, at one level.
    """
    coords = [(float(x), float(y)) for x, y in line.coords]
    coords = _cut_start_spur(coords, tol_m)
    coords = _cut_start_spur(coords[::-1], tol_m)[::-1]
    out = [coords[0]]
    for c in coords[1:]:
        if Point(c).distance(Point(out[-1])) > 1.0:
            out.append(c)
    return LineString(out)


def choose_band(line, volumes, tree, cfg, exempt_ids=frozenset(), clearance_route=False):
    """Pick the corridor's altitude band (FDOT zones, see DEFAULT_COORDINATION_AIRSPACE).

    Tries bands from the top of the 1,000-3,000 ft range down. First looks for a band
    clear of all controlled airspace ("avoid controlled airspace when possible"); if
    none exists, accepts a band that only enters coordination airspace (Class D).

    On a designated clearance route, a third pass also accepts Class B (with ATC
    clearance). Restricted, prohibited and military airspace are never accepted.

    Returns (band, coordination_ids, clearance_ids, blockers): band is None when
    nothing works, and blockers then names the airspace that closed the lowest band.
    """
    coordination = set(cfg.get("coordination_airspace", DEFAULT_COORDINATION_AIRSPACE))
    clearance = set(cfg.get("clearance_airspace", DEFAULT_CLEARANCE_AIRSPACE)) if clearance_route else set()
    near = line.buffer(cfg["lateral_buffer_m"])
    hits = [volumes[i] for i in tree.query(near, predicate="intersects")]
    hits = [v for v in hits if v.id not in exempt_ids]

    def bands():
        top = cfg["band_max_ft"]
        while top - cfg["band_thickness_ft"] >= cfg["band_min_ft"]:
            yield top - cfg["band_thickness_ft"], top
            top -= cfg["band_thickness_ft"]

    def overlapping(bottom, top):
        return [v for v in hits if v.floor_ft < top and v.ceiling_ft > bottom]

    passes = [(set(), set()), (coordination, set())]
    if clearance:
        passes.append((coordination, clearance))
    for allow_coord, allow_clear in passes:
        for bottom, top in bands():
            inside = overlapping(bottom, top)
            hard = [v for v in inside if v.type not in allow_coord | allow_clear]
            if hard:
                continue
            coord = sorted({v.id for v in inside if v.type in allow_coord})
            clear = sorted({v.id for v in inside if v.type in allow_clear})
            return (bottom, top), coord, clear, []
    bottom = cfg["band_min_ft"]
    allowed = coordination | clearance
    blockers = [v.id for v in overlapping(bottom, bottom + cfg["band_thickness_ft"]) if v.type not in allowed]
    return None, [], [], blockers


def build(network: Path, highways: Path, constraints: Path):
    cfg_all = yaml.safe_load(network.read_text())
    cfg = cfg_all["build"]
    nodes = cfg_all["nodes"]
    report = Report()
    for nid, n in nodes.items():
        unknown = set(n.get("types", [])) - SITE_TYPES - {"waypoint"}
        if not n.get("types") or unknown:
            report.problems.append(f"{nid}: bad types {n.get('types')!r} (use evtol, helipad or waypoint)")
        if "waypoint" not in n.get("types", ["waypoint"]):
            # Which vehicle types may fly here (ARCHITECTURE §3.2); explicit `serves` wins.
            n.setdefault("serves", sorted({SERVES_BY_DEFAULT[t] for t in n["types"] if t in SERVES_BY_DEFAULT}))
    hw = {name: HighwayGraph(g) for name, g in load_highways(highways).items()}
    volumes = load_volumes(constraints)
    tree = shapely.STRtree([v.geom for v in volumes])
    pts = {n: to_utm(Point(v["lon"], v["lat"])) for n, v in nodes.items()}

    graph = nx.Graph()
    features = []
    for nid, n in nodes.items():
        graph.add_node(nid)
        features.append(
            {
                "type": "Feature",
                "properties": {"id": nid, "kind": "node", **{k: v for k, v in n.items() if k not in ("lat", "lon")}},
                "geometry": {"type": "Point", "coordinates": [n["lon"], n["lat"]]},
            }
        )

    clearance_routes = {frozenset(pair) for pair in cfg_all.get("clearance_routes", [])}
    work = [(a, b, h, "corridor") for a, b, h in cfg_all["edges"]] + [
        (a, b, None, "connector") for a, b in cfg_all["connectors"]
    ]

    for a, b, highway, kind in work:
        label = f"{a} -> {b}" + (f" ({highway})" if highway else " (connector)")
        if highway is not None:
            if highway not in hw:
                report.problems.append(f"{label}: highway {highway!r} not in highways file")
                continue
            line = hw[highway].path(pts[a], pts[b], cfg["simplify_m"])
            if line is None:
                report.problems.append(f"{label}: {highway} doesn't connect these nodes (gap in the road data?)")
                continue
            for n in (a, b):
                off = hw[highway].geom.distance(pts[n])
                if off > cfg.get("max_waypoint_offset_m", 2000):
                    report.problems.append(f"{label}: {n} is {off / 1000:.1f} km from {highway}; move it onto the road")
            exempt = frozenset()
        else:
            line = LineString([pts[a].coords[0], pts[b].coords[0]])
            site = a if "waypoint" not in nodes[a]["types"] else b
            # A connector may enter the Class B/C/D area that contains its own landing site.
            exempt = frozenset(
                v.id for v in volumes if v.type in BLOCKING_AIRSPACE_FOR_EXEMPTION and v.geom.contains(pts[site])
            )
        on_clearance_route = frozenset((a, b)) in clearance_routes
        band, coord, clear, blockers = choose_band(line, volumes, tree, cfg, exempt, on_clearance_route)
        if band is None:
            report.dropped.append(f"{label}: no clear band; blocked by {', '.join(sorted(set(blockers)))}")
            continue
        length = round(line.length)
        note = f", ATC coordination: {', '.join(coord)}" if coord else ""
        note += f", Class B clearance: {', '.join(clear)}" if clear else ""
        if clear:
            report.clearance.append(f"{label}: {', '.join(clear)}")
        report.edges.append(f"{label}: {length / 1000:.1f} km, band {band[0]}-{band[1]} ft{note}")
        if coord:
            report.coordination.append(f"{label}: {', '.join(coord)}")
        graph.add_edge(a, b, length_m=length)
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "id": f"{a}__{b}",
                    "kind": kind,
                    "from": a,
                    "to": b,
                    "highway": highway,
                    "length_m": length,
                    "band_floor_ft": band[0],
                    "band_ceiling_ft": band[1],
                    "exempt_airspace": sorted(exempt),
                    "coordination_airspace": coord,
                    "clearance_airspace": clear,
                },
                "geometry": mapping(to_wgs(line)),
            }
        )

    known = {frozenset((a, b)) for a, b, _, _ in work}
    for pair in clearance_routes - known:
        report.problems.append(f"clearance route {' -> '.join(sorted(pair))} is not an edge or connector")

    # Every landing site must be reachable from every other one (one connected network).
    sites = [n for n, v in nodes.items() if "waypoint" not in v.get("types", [])]
    if sites:
        main_part: set[str] = set(max(nx.connected_components(graph), key=len))
        for s in sites:
            if s not in main_part:
                report.problems.append(f"{s}: not connected to the network (check its connector)")

    evtol_sites = [n for n, v in nodes.items() if "evtol" in v["types"]]
    min_km = cfg.get("redundancy_min_distance_km", 0)
    for a, b in itertools.combinations(evtol_sites, 2):
        # Sites in the same city (closer than min_km) may share one approach.
        need = cfg["min_routes_per_pair"] if pts[a].distance(pts[b]) / 1000 >= min_km else 1
        n = 0
        if nx.has_path(graph, a, b):
            for _ in itertools.islice(nx.shortest_simple_paths(graph, a, b, weight="length_m"), need):
                n += 1
        report.routes[f"{a} <-> {b}"] = n
        if n < need:
            report.problems.append(f"{a} <-> {b}: only {n} route(s)")

    collection = {
        "type": "FeatureCollection",
        "name": cfg_all.get("version", "network"),
        "aerolink": {"build": cfg, "sources": [str(network.name), str(highways.name), str(constraints.name)]},
        "features": features,
    }
    return collection, report


def validate(collection: dict, constraints: Path, lateral_buffer_m: float | None = None) -> list[str]:
    """Independent 3D re-check of a built network (PLAN E2.4).

    Every corridor and connector, at its altitude band, must stay clear of every
    blocking volume within the lateral buffer, except the airspace it was
    explicitly exempted from (a landing site's own Class B/C/D), coordination
    airspace (Class D) recorded in `coordination_airspace`, and Class B recorded
    in `clearance_airspace` on a designated clearance route. Returns a list
    of violations; empty means the network is clean.
    """
    build_cfg = collection["aerolink"]["build"]
    buffer_m = lateral_buffer_m if lateral_buffer_m is not None else build_cfg["lateral_buffer_m"]
    coordination = set(build_cfg.get("coordination_airspace", DEFAULT_COORDINATION_AIRSPACE))
    volumes = load_volumes(constraints)
    tree = shapely.STRtree([v.geom for v in volumes])
    problems = []
    for f in collection["features"]:
        p = f["properties"]
        if p.get("kind") not in ("corridor", "connector"):
            continue
        line = to_utm(shape(f["geometry"]))
        exempt = (
            set(p.get("exempt_airspace", []))
            | set(p.get("coordination_airspace", []))
            | set(p.get("clearance_airspace", []))
        )
        for i in tree.query(line.buffer(buffer_m), predicate="intersects"):
            v = volumes[i]
            if v.id in exempt:
                continue
            if not (v.floor_ft < p["band_ceiling_ft"] and v.ceiling_ft > p["band_floor_ft"]):
                continue  # beside the corridor but above or below its band
            if v.type in coordination:
                problems.append(f"{p['id']} enters {v.id} ({v.type}) without recording ATC coordination")
            else:
                problems.append(
                    f"{p['id']} at {p['band_floor_ft']}-{p['band_ceiling_ft']} ft enters {v.id} "
                    f"({v.type}, {v.floor_ft:.0f}-{v.ceiling_ft:.0f} ft)"
                )
    return problems


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the AeroLink corridor network")
    for name in ("network", "highways", "constraints", "out", "report"):
        ap.add_argument(f"--{name}", type=Path, required=True)
    a = ap.parse_args()

    collection, report = build(a.network, a.highways, a.constraints)
    a.out.write_text(json.dumps(collection) + "\n")
    a.report.write_text(report.text())
    print(report.text())
    print(f"wrote {a.out} and {a.report}")
    if report.problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
