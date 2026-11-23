"""Network build tests on synthetic inputs (no FDOT/FAA downloads needed)."""

import json
from pathlib import Path
from typing import Any

import yaml
from shapely.geometry import LineString

from aerolink.airspace.build_network import build, remove_backtracks, validate

ROOT = Path(__file__).resolve().parents[2]
NETWORK = ROOT / "airspace" / "network.yaml"

# Highways drawn straight through the waypoints listed for them (stand-in for FDOT shapes).
HIGHWAY_ORDER = {
    "I-4": [
        "wp-downtown-tampa",
        "wp-i4-i75",
        "wp-plant-city",
        "wp-lakeland",
        "wp-auburndale",
        "wp-i4-us27",
        "wp-kissimmee",
        "wp-orlando",
    ],
    "SR-60": ["wp-downtown-tampa", "wp-brandon", "wp-mulberry", "wp-bartow", "wp-lake-wales"],
    "US-98": ["wp-lakeland", "wp-bartow"],
    "US-27": ["wp-i4-us27", "wp-haines-city", "wp-lake-wales", "wp-avon-park", "wp-sebring"],
    "US-17": ["wp-orlando", "wp-haines-city"],
}


def _square(lon: float, lat: float, d: float) -> list:
    return [[[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d]]]


def _volume(vid: str, kind: str, lon: float, lat: float, d: float, floor: int, ceil: int) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {
            "id": vid,
            "type": kind,
            "blocks": True,
            "in_focus_region": True,
            "floor_ft": floor,
            "ceiling_ft": ceil,
        },
        "geometry": {"type": "Polygon", "coordinates": _square(lon, lat, d)},
    }


def _write_inputs(tmp: Path, volumes: list[dict[str, Any]]) -> tuple[Path, Path]:
    nodes = yaml.safe_load(NETWORK.read_text())["nodes"]
    hw = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"highway": name},
                "geometry": {"type": "LineString", "coordinates": [[nodes[n]["lon"], nodes[n]["lat"]] for n in order]},
            }
            for name, order in HIGHWAY_ORDER.items()
        ],
    }
    (tmp / "hw.geojson").write_text(json.dumps(hw))
    (tmp / "c.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": volumes}))
    return tmp / "hw.geojson", tmp / "c.geojson"


def test_clean_network_builds_with_two_routes_per_pair(tmp_path):
    hw, c = _write_inputs(tmp_path, [])
    collection, report = build(NETWORK, hw, c)
    assert not report.problems, report.problems
    assert report.routes["ev-water-st <-> ev-orl"] == 2  # city pairs: two routes
    assert report.routes["ev-orl <-> ev-parramore"] == 1  # same city (<15 km): one is enough
    edges = [f for f in collection["features"] if f["properties"]["kind"] != "node"]
    assert all(f["properties"]["band_ceiling_ft"] == 3000 for f in edges)  # nothing overhead


def test_shelf_lowers_the_band(tmp_path):
    # 2,000 ft floor over Plant City: the I-4 edges there must fly at 1,500-2,000 ft or lower.
    hw, c = _write_inputs(tmp_path, [_volume("shelf", "class_b", -82.12, 28.02, 0.05, 2000, 10000)])
    collection, report = build(NETWORK, hw, c)
    e = next(f for f in collection["features"] if f["properties"]["id"] == "wp-plant-city__wp-lakeland")
    assert e["properties"]["band_ceiling_ft"] <= 2000


def test_restricted_area_drops_edge_and_is_named(tmp_path):
    # Surface-to-high restricted block over the Lake Wales-Avon Park leg of US-27.
    hw, c = _write_inputs(tmp_path, [_volume("r-test", "restricted", -81.55, 27.75, 0.05, 0, 18000)])
    _, report = build(NETWORK, hw, c)
    assert any("wp-lake-wales -> wp-avon-park" in d and "r-test" in d for d in report.dropped)
    assert any("ev-sef" in p for p in report.problems)  # Sebring loses its routes -> build fails


def test_connector_may_enter_its_own_class_d(tmp_path):
    # Class D around Orlando Executive: the ev-orl connector is exempt, a passing corridor is not.
    nodes = yaml.safe_load(NETWORK.read_text())["nodes"]
    orl = (nodes["ev-orl"]["lon"], nodes["ev-orl"]["lat"])
    hw, c = _write_inputs(tmp_path, [_volume("orl-d", "class_d", *orl, 0.03, 0, 2500)])
    collection, report = build(NETWORK, hw, c)
    conn = next(f for f in collection["features"] if f["properties"]["id"] == "ev-orl__wp-orlando")
    assert conn["properties"]["exempt_airspace"] == ["orl-d"]


def test_validate_catches_a_corridor_inside_restricted_airspace(tmp_path):
    hw, c = _write_inputs(tmp_path, [])
    collection, _ = build(NETWORK, hw, c)
    assert validate(collection, c) == []
    # Now add a restricted block over Plant City *after* the build: the re-check must flag it.
    (tmp_path / "c.geojson").write_text(
        json.dumps(
            {"type": "FeatureCollection", "features": [_volume("r-late", "restricted", -82.12, 28.03, 0.05, 0, 18000)]}
        )
    )
    problems = validate(collection, c)
    assert problems and all("r-late" in p for p in problems)


def test_unconnected_site_fails_the_build(tmp_path):
    cfg = yaml.safe_load(NETWORK.read_text())
    cfg["nodes"]["hp-lonely"] = {"types": ["helipad"], "name": "Lonely", "lat": 28.0, "lon": -82.0}
    net = tmp_path / "network.yaml"
    net.write_text(yaml.safe_dump(cfg))
    hw, c = _write_inputs(tmp_path, [])
    _, report = build(net, hw, c)
    assert any("hp-lonely" in p and "not connected" in p for p in report.problems)


def test_class_d_is_crossed_with_coordination_and_avoided_when_possible(tmp_path):
    # Class D from the surface to 2,500 ft over Plant City (FDOT yellow zone).
    hw, c = _write_inputs(tmp_path, [_volume("pcm-d", "class_d", -82.12, 28.03, 0.05, 0, 2500)])
    collection, report = build(NETWORK, hw, c)
    e = next(f for f in collection["features"] if f["properties"]["id"] == "wp-plant-city__wp-lakeland")
    # A band above the Class D exists, so the corridor avoids it (no coordination needed).
    assert e["properties"]["band_floor_ft"] >= 2500 and e["properties"]["coordination_airspace"] == []

    # Class D reaching the top of the band: no clear band, so cross it with coordination.
    (tmp_path / "c.geojson").write_text(
        json.dumps(
            {"type": "FeatureCollection", "features": [_volume("pcm-d", "class_d", -82.12, 28.03, 0.05, 0, 3500)]}
        )
    )
    collection, report = build(NETWORK, hw, c)
    e = next(f for f in collection["features"] if f["properties"]["id"] == "wp-plant-city__wp-lakeland")
    assert e["properties"]["coordination_airspace"] == ["pcm-d"]
    assert any("pcm-d" in line for line in report.coordination)
    assert not report.dropped and validate(collection, c) == []


def test_class_b_surface_area_still_blocks(tmp_path):
    # FDOT pink zone: Class B from the surface. Corridors may not pass, even with coordination.
    hw, c = _write_inputs(tmp_path, [_volume("tpa-b", "class_b", -81.55, 27.75, 0.05, 0, 10000)])
    _, report = build(NETWORK, hw, c)
    assert any("wp-lake-wales -> wp-avon-park" in d and "tpa-b" in d for d in report.dropped)


def test_validate_ignores_class_d_below_the_band(tmp_path):
    # Class D next to the corridor but topping out below the band is fine (Bartow case).
    hw, c = _write_inputs(tmp_path, [_volume("bow-d", "class_d", -81.84, 27.90, 0.05, 0, 2500)])
    collection, _ = build(NETWORK, hw, c)
    assert validate(collection, c) == []


def _with_clearance(tmp_path: Path, pair: list[str]) -> Path:
    cfg = yaml.safe_load(NETWORK.read_text())
    cfg["clearance_routes"] = [pair]
    net = tmp_path / "network.yaml"
    net.write_text(yaml.safe_dump(cfg))
    return net


def test_designated_clearance_route_may_cross_class_b(tmp_path):
    # Surface Class B over Lake Wales-Avon Park: blocked normally (see above), allowed when designated.
    net = _with_clearance(tmp_path, ["wp-lake-wales", "wp-avon-park"])
    hw, c = _write_inputs(tmp_path, [_volume("mco-b", "class_b", -81.55, 27.75, 0.05, 0, 10000)])
    collection, report = build(net, hw, c)
    e = next(f for f in collection["features"] if f["properties"]["id"] == "wp-lake-wales__wp-avon-park")
    assert e["properties"]["clearance_airspace"] == ["mco-b"]
    assert any("mco-b" in line for line in report.clearance)
    assert not report.problems and validate(collection, c) == []


def test_clearance_route_never_crosses_restricted_airspace(tmp_path):
    net = _with_clearance(tmp_path, ["wp-lake-wales", "wp-avon-park"])
    hw, c = _write_inputs(tmp_path, [_volume("r-test", "restricted", -81.55, 27.75, 0.05, 0, 18000)])
    _, report = build(net, hw, c)
    assert any("wp-lake-wales -> wp-avon-park" in d and "r-test" in d for d in report.dropped)


# ------------------------------------------------------------ remove_backtracks
def test_start_spur_removed():
    # goes 3 km south past the start, then back north through it
    line = LineString([(0, 0), (0, -3000), (0, 5000), (4000, 9000)])
    assert list(remove_backtracks(line).coords) == [(0, 0), (0, 5000), (4000, 9000)]


def test_end_spur_removed():
    # overshoots the end by 500 m and comes back
    line = LineString([(0, 0), (6000, 0), (6500, 0), (6000, 50)])
    out = list(remove_backtracks(line).coords)
    assert out[0] == (0, 0) and out[-1] == (6000, 50)
    assert LineString(out).length < 6100


def test_straight_and_dogleg_lines_unchanged():
    for coords in ([(0, 0), (10_000, 0)], [(0, 0), (-1000, 1500), (-1000, -20_000)]):
        assert list(remove_backtracks(LineString(coords)).coords) == [(float(x), float(y)) for x, y in coords]


def test_committed_network_has_no_backtracks():
    """No committed corridor turns back on itself (>150 deg) near either end."""
    import math

    net = json.loads((Path(__file__).resolve().parents[2] / "airspace" / "central-fl-v1.geojson").read_text())
    for f in net["features"]:
        if f["properties"]["kind"] == "node":
            continue
        pts = f["geometry"]["coordinates"]
        for i in range(1, len(pts) - 1):
            k = math.cos(math.radians(pts[i][1]))
            ax, ay = (pts[i][0] - pts[i - 1][0]) * k, pts[i][1] - pts[i - 1][1]
            bx, by = (pts[i + 1][0] - pts[i][0]) * k, pts[i + 1][1] - pts[i][1]
            cos = (ax * bx + ay * by) / (math.hypot(ax, ay) * math.hypot(bx, by))
            assert cos > math.cos(math.radians(150)), (f["properties"]["id"], i)
