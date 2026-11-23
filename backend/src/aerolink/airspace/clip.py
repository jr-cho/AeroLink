"""Cut a large GeoJSON file down to the features inside a region.

Usage (from backend/):
    uv run python -m aerolink.airspace.clip IN.geojson OUT.geojson [--region florida|central-fl]

Features that touch the region are kept whole (not cut at the edge), so a
no-go area that straddles the boundary keeps its full real shape.
"""

import argparse
import json
from pathlib import Path

from shapely.geometry import box, shape

# (west, south, east, north) in degrees
REGIONS = {
    "florida": (-87.7, 24.3, -79.8, 31.1),
    "central-fl": (-82.8, 27.3, -81.1, 28.8),  # AeroLink region (ARCHITECTURE §3.1)
}


def clip(src: Path, dst: Path, region: str) -> tuple[int, int]:
    area = box(*REGIONS[region])
    data = json.loads(src.read_text())
    features = data.get("features", [])

    kept = []
    for f in features:
        geom = f.get("geometry")
        if geom is None:
            continue
        if shape(geom).intersects(area):
            kept.append(f)

    out = {"type": "FeatureCollection", "features": kept}
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(out))
    return len(features), len(kept)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("src", type=Path)
    p.add_argument("dst", type=Path)
    p.add_argument("--region", choices=REGIONS, default="florida")
    args = p.parse_args()

    total, kept = clip(args.src, args.dst, args.region)
    size_kb = args.dst.stat().st_size / 1024
    print(f"{args.src.name}: kept {kept} of {total} features -> {args.dst} ({size_kb:,.0f} KB)")


if __name__ == "__main__":
    main()
