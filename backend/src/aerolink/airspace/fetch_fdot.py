"""Turn FDOT highway shapefiles into a small WGS84 GeoJSON for the network build.

Step 1 - inspect (shows which field names the route numbers):
    uv run python -m aerolink.airspace.fetch_fdot inspect ../airspace/source/fdot/interstates.zip

Step 2 - extract the highways listed in network.yaml:
    uv run python -m aerolink.airspace.fetch_fdot extract \
        --network ../airspace/network.yaml \
        --source-dir ../airspace/source/fdot \
        --out ../airspace/fdot-highways.geojson

Download the zips from FDOT's GIS page (Interstates, U.S. Highways, State Roads)
into airspace/source/fdot/ as interstates.zip, us_roads.zip, state_roads.zip.
"""

import argparse
import json
from collections import Counter
from pathlib import Path

import pyogrio
import shapely
import yaml
from pyproj import Transformer
from shapely.geometry import box, mapping
from shapely.ops import linemerge, unary_union

FOCUS_WITH_MARGIN = box(-83.0, 27.1, -80.9, 29.0)  # focus region + ~20 km margin


def _read(path: Path):
    """Read a shapefile (or zipped shapefile) as (geometries in WGS84, attribute rows)."""
    meta, geoms, field_data, fields = _read_raw(path)
    crs = meta.get("crs")
    if crs and crs not in ("EPSG:4326", "OGC:CRS84"):
        t = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        geoms = [shapely.transform(g, lambda xy: _tx(t, xy)) if g is not None else None for g in geoms]
    rows = (
        [dict(zip(fields, vals, strict=True)) for vals in zip(*field_data, strict=True)]
        if fields.size
        else [{}] * len(geoms)
    )
    return geoms, rows


def _tx(t, xy):
    x, y = t.transform(xy[:, 0], xy[:, 1])
    xy[:, 0], xy[:, 1] = x, y
    return xy


def _read_raw(path: Path):
    meta, _, wkb, field_data = pyogrio.raw.read(path)
    geoms = shapely.from_wkb(wkb)
    fields = meta["fields"]
    return meta, list(geoms), field_data, fields


def inspect(path: Path, show: int = 8) -> None:
    geoms, rows = _read(path)
    print(f"{path.name}: {len(geoms)} features")
    if not rows or not rows[0]:
        print("  (no attribute fields)")
        return
    for field in rows[0]:
        values = Counter(str(r.get(field)) for r in rows)
        sample = ", ".join(v for v, _ in values.most_common(show))
        print(f"  {field:<12} {len(values):>5} distinct   e.g. {sample}")


def extract(network: Path, source_dir: Path, out: Path) -> None:
    cfg = yaml.safe_load(network.read_text())
    cache: dict[str, tuple] = {}
    features = []
    for name, spec in cfg["highways"].items():
        layer = spec["layer"]
        if layer not in cache:
            cache[layer] = _read(source_dir / f"{layer}.zip")
        geoms, rows = cache[layer]
        want = str(spec["value"]).strip()
        parts = [
            g
            for g, r in zip(geoms, rows, strict=True)
            if g is not None and str(r.get(spec["field"], "")).strip() == want
        ]
        if not parts:
            raise SystemExit(f"{name}: no features where {spec['field']} == {want!r} in {layer}")
        merged = linemerge(unary_union(parts).intersection(FOCUS_WITH_MARGIN))
        merged = merged.simplify(0.0001, preserve_topology=False)  # ~10 m; trims file size
        features.append(
            {
                "type": "Feature",
                "properties": {"highway": name, "source": f"FDOT {layer}", "parts": len(parts)},
                "geometry": mapping(merged),
            }
        )
        print(f"{name}: {len(parts)} source segments -> {merged.geom_type}")

    out.write_text(json.dumps({"type": "FeatureCollection", "features": features}) + "\n")
    print(f"wrote {out} ({out.stat().st_size / 1024:,.0f} KB)")


def main() -> None:
    ap = argparse.ArgumentParser(description="FDOT highway shapefiles -> GeoJSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("inspect")
    i.add_argument("path", type=Path)
    e = sub.add_parser("extract")
    e.add_argument("--network", type=Path, required=True)
    e.add_argument("--source-dir", type=Path, required=True)
    e.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.cmd == "inspect":
        inspect(a.path)
    else:
        extract(a.network, a.source_dir, a.out)


if __name__ == "__main__":
    main()
