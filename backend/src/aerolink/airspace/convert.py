"""Convert clipped FAA airspace files into AeroLink's constraints format.

Usage (from backend/):
    uv run python -m aerolink.airspace.convert \
        --class-airspace ../airspace/source/florida-class-airspace.geojson \
        --special-use    ../airspace/source/florida-special-use-airspace.geojson \
        --cycle 2026-11-xx \
        --out ../airspace/constraints-fl.geojson

Every output area answers three questions directly:
  blocks          - is it a no-go area for AeroLink?            (policy below)
  floor_m/ceil_m  - vertical limits, metres above sea level      (one unit, one reference)
  in_focus_region - does it touch Central Florida?               (planner skips the rest)
"""

import argparse
import json
import re
from collections import Counter
from pathlib import Path

import shapely
from shapely.geometry import box, mapping, shape

FT_TO_M = 0.3048
AGL_CEILING_MARGIN_M = 100.0  # Central Florida terrain is low; pad AGL ceilings upward
UNLIMITED_FT = 60_000  # used when an upper limit is "unlimited" or unreadable
SIMPLIFY_DEG = 0.0005  # ~50 m at Florida latitudes

FLORIDA = box(-87.7, 24.3, -79.8, 31.1)
FOCUS = box(-82.8, 27.3, -81.1, 28.8)

# --- Policy: what counts as no-go -------------------------------------------------
CLASS_TYPES = {"B": "class_b", "C": "class_c", "D": "class_d", "E": "class_e"}
SUA_TYPES = {"R": "restricted", "P": "prohibited", "MOA": "moa", "W": "warning", "A": "alert"}
BLOCKING = {"class_b", "class_c", "class_d", "restricted", "prohibited", "moa", "warning", "alert"}
# class_e is kept for the map but never blocks (it covers almost everything above 700/1,200 ft).


def _limit_ft(val, uom, code, is_ceiling):
    """Return (feet MSL, original reference). Conservative: floors low, ceilings high."""
    code = (code or "").upper()
    uom = (uom or "").upper()
    if code == "SFC" or val in (None, "", "0", "SFC"):
        return (0.0, "SFC") if not is_ceiling else (float(UNLIMITED_FT), "UNKNOWN")
    try:
        n = float(val)
    except (TypeError, ValueError):
        return (float(UNLIMITED_FT), "UNKNOWN") if is_ceiling else (0.0, "UNKNOWN")
    if n < 0:  # FAA sentinel values (e.g. "up to Class A") -> treat as unlimited / surface
        return (float(UNLIMITED_FT), "SENTINEL") if is_ceiling else (0.0, "SENTINEL")
    ft = n * 100 if uom == "FL" else n
    ref = "STD" if uom == "FL" else (code or "MSL")
    if ref == "AGL" and is_ceiling:
        ft += AGL_CEILING_MARGIN_M / FT_TO_M
    return ft, ref


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "unnamed").lower()).strip("-")


def _drop_z(geom):
    return shapely.force_2d(geom)


def convert_feature(f, kind, cycle):
    p = f["properties"]
    if kind == "class":
        atype = CLASS_TYPES.get((p.get("CLASS") or "").upper())
    else:
        atype = SUA_TYPES.get((p.get("TYPE_CODE") or "").upper())
    if atype is None:
        return None, f"unknown type {p.get('CLASS') or p.get('TYPE_CODE')!r}"

    floor_ft, floor_ref = _limit_ft(p.get("LOWER_VAL"), p.get("LOWER_UOM"), p.get("LOWER_CODE"), False)
    ceil_ft, ceil_ref = _limit_ft(p.get("UPPER_VAL"), p.get("UPPER_UOM"), p.get("UPPER_CODE"), True)

    geom = _drop_z(shape(f["geometry"])).simplify(SIMPLIFY_DEG, preserve_topology=True)
    if geom.is_empty or not geom.intersects(FLORIDA):
        return None, "outside Florida"

    name = p.get("NAME") or "unnamed"
    return {
        "type": "Feature",
        "properties": {
            "id": f"faa-{atype}-{_slug(name)}-{p.get('OBJECTID')}",
            "name": name,
            "type": atype,
            "blocks": atype in BLOCKING,
            "floor_m": round(floor_ft * FT_TO_M, 1),
            "ceiling_m": round(ceil_ft * FT_TO_M, 1),
            "floor_ft": round(floor_ft),
            "ceiling_ft": round(ceil_ft),
            "floor_ref_original": floor_ref,
            "ceiling_ref_original": ceil_ref,
            "in_focus_region": geom.intersects(FOCUS),
            "always_active": True,  # part-time areas treated as always on
            "times_of_use": p.get("TIMESOFUSE"),
            "source": "faa",
            "source_id": p.get("GLOBAL_ID"),
            "data_cycle": cycle,
        },
        "geometry": mapping(geom),
    }, None


def convert(paths_and_kinds, cycle):
    out, skipped = [], Counter()
    for path, kind in paths_and_kinds:
        for f in json.loads(Path(path).read_text())["features"]:
            if not f.get("geometry"):
                skipped["no geometry"] += 1
                continue
            feat, why = convert_feature(f, kind, cycle)
            if feat:
                out.append(feat)
            else:
                skipped[why] += 1
    return out, skipped


def main():
    ap = argparse.ArgumentParser(description="Convert FAA airspace to AeroLink constraints.")
    ap.add_argument("--class-airspace", type=Path, required=True)
    ap.add_argument("--special-use", type=Path, required=True)
    ap.add_argument("--cycle", required=True, help="FAA data cycle date, e.g. 2026-11-27")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    features, skipped = convert([(a.class_airspace, "class"), (a.special_use, "sua")], a.cycle)
    a.out.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "name": "constraints-fl",
                "aerolink": {
                    "data_cycle": a.cycle,
                    "region_bbox": list(FLORIDA.bounds),
                    "focus_bbox": list(FOCUS.bounds),
                },
                "features": features,
            }
        )
        + "\n"
    )

    by_type = Counter(f["properties"]["type"] for f in features)
    focus = sum(f["properties"]["in_focus_region"] for f in features)
    print(f"wrote {len(features)} areas ({focus} in focus) -> {a.out} ({a.out.stat().st_size / 1024:,.0f} KB)")
    print("by type:", dict(sorted(by_type.items())))
    if skipped:
        print("skipped:", dict(skipped))


if __name__ == "__main__":
    main()
