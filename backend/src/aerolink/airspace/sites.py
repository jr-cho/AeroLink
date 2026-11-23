"""Find landing-site candidates in the FAA ADDS "Airports" dataset (PLAN E2.2).

The FAA lists every helipad (hospital pads included) as an "airport" with
TYPE_CODE = HP. This tool turns that national file into a short review sheet,
then turns the rows you keep into `nodes:` entries for airspace/network.yaml.

Usage (from backend/):

    # 1. See what's in the file (field names and their values)
    uv run python -m aerolink.airspace.sites inspect ../airspace/source/faa-airports.geojson

    # 2. Write a review sheet of helipads in Central Florida
    uv run python -m aerolink.airspace.sites candidates ../airspace/source/faa-airports.geojson \
        --out ../airspace/sites-review.csv

    # 3. Open the CSV, put "yes" in the keep column for the sites you want, fill pads/verified/notes

    # 4. Print network.yaml lines for the kept rows (paste them under `nodes:`)
    uv run python -m aerolink.airspace.sites to-yaml ../airspace/sites-review.csv

Two input formats are read (detected automatically):
  * FAA ADDS "Airports": TYPE_CODE (HP/AD), IDENT, NAME, OPERSTATUS, MIL_CODE, PRIVATEUSE
  * FDEP "Airports" (Statewide Critical Assets, "Airports FDEM and FDOT merge"):
    FACILITY_T (HELIPORT/HELIPAD, AIRPORT), ACODE, UAID, Name, FAC_STATUS, AOWNERSHIP, AUSE

Field names are matched case-insensitively. With --network, each candidate gets its
nearest corridor waypoint and distance, so far-away pads are easy to spot.
"""

import argparse
import csv
import json
import re
import string
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from shapely.geometry import Point, box

from aerolink.airspace.clip import REGIONS

# FAA TYPE_CODE -> AeroLink site type (ARCHITECTURE §3.2)
SITE_TYPE_BY_FAA = {"HP": "helipad", "AD": "evtol"}
ID_PREFIX = {"helipad": "hp", "evtol": "ev"}

# Name words that suggest a hospital / EMS pad. A guess to sort the sheet, not a filter.
HOSPITAL_WORDS = re.compile(
    r"hospital|medical|\bmed\b|health|clinic|trauma|memorial|baycare|adventhealth|"
    r"\bhca\b|\bems\b|emergency|children|regional\s+med|rgnl\s+med|\bmc\b|"
    r"st\.?\s+joseph|morton\s+plant|mease|baptist|care\s*flight|fire[\s/-]*rescue",
    re.IGNORECASE,
)

REVIEW_COLUMNS = [
    "keep",
    "id",
    "name",
    "type",
    "lat",
    "lon",
    "pads",
    "source",
    "verified",
    "notes",
    "faa_ident",
    "faa_type",
    "private_use",
    "city",
    "county",
    "hospital_like",
    "nearest_waypoint",
    "km_to_waypoint",
]


def prop(props: dict[str, Any], name: str) -> Any:
    """Read a property whatever its case (IDENT, ident, Ident)."""
    want = name.lower()
    for k, v in props.items():
        if k.lower() == want:
            return v
    return None


def position(feature: dict[str, Any]) -> tuple[float, float] | None:
    """(lon, lat) from the point geometry, falling back to numeric LATITUDE/LONGITUDE fields."""
    g = feature.get("geometry") or {}
    if g.get("type") == "Point" and g.get("coordinates"):
        lon, lat = g["coordinates"][:2]
        return float(lon), float(lat)
    p = feature.get("properties") or {}
    try:
        return float(prop(p, "longitude")), float(prop(p, "latitude"))
    except (TypeError, ValueError):
        return None


def is_fdem(props: dict[str, Any]) -> bool:
    """FDEP/FDEM-FDOT layer rather than FAA ADDS."""
    return prop(props, "facility_t") is not None


def faa_type_of(props: dict[str, Any]) -> str:
    if is_fdem(props):
        kind = str(prop(props, "facility_t")).upper()
        return "HP" if "HELI" in kind else "AD" if "AIRPORT" in kind else kind
    return str(prop(props, "type_code") or "").upper()


def ident_of(props: dict[str, Any]) -> str:
    """FAA location id; the FDOT UAID when a pad has no FAA id."""
    for field in ("ident", "acode", "uaid"):
        value = str(prop(props, field) or "").strip()
        if value:
            return value
    return ""


def is_operational(props: dict[str, Any]) -> bool:
    status = prop(props, "fac_status") if is_fdem(props) else prop(props, "operstatus")
    if status is None or not str(status).strip():
        return True
    text = str(status).upper()
    return "OPER" in text or "ACTIVE" in text


def is_military(props: dict[str, Any]) -> bool:
    if is_fdem(props):
        return str(prop(props, "aownership") or "").upper().startswith("MILITARY")
    return str(prop(props, "mil_code") or "").upper().startswith("MIL")


def is_private(props: dict[str, Any]) -> bool:
    if is_fdem(props):
        return str(prop(props, "ause") or "").strip().lower() == "private"
    return str(prop(props, "privateuse")).strip() in ("1", "True", "true", "Y")


def inspect(path: Path) -> None:
    features = json.loads(path.read_text())["features"]
    print(f"{path.name}: {len(features)} features")
    if not features:
        return
    keys = list((features[0].get("properties") or {}).keys())
    print(f"fields: {', '.join(keys)}")
    for field in (
        "type_code",
        "mil_code",
        "operstatus",
        "privateuse",
        "state",
        "facility_t",
        "fac_status",
        "aownership",
        "ause",
    ):
        if prop(features[0].get("properties") or {}, field) is None:
            continue
        values = Counter(str(prop(f.get("properties") or {}, field)) for f in features)
        top = ", ".join(f"{v} ({n})" for v, n in values.most_common(12))
        print(f"  {field:<11} {top}")


def load_waypoints(network: Path | None) -> dict[str, Point]:
    """Corridor waypoints from network.yaml, in UTM metres."""
    if network is None:
        return {}
    import yaml
    from pyproj import Transformer

    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32617", always_xy=True)
    nodes = yaml.safe_load(network.read_text()).get("nodes", {})
    return {
        nid: Point(*to_utm.transform(n["lon"], n["lat"]))
        for nid, n in nodes.items()
        if "waypoint" in n.get("types", [])
    }


def candidates(path: Path, region: str, faa_types: list[str], network: Path | None = None) -> list[dict[str, Any]]:
    area = box(*REGIONS[region])
    waypoints = load_waypoints(network)
    to_utm = None
    if waypoints:
        from pyproj import Transformer

        to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32617", always_xy=True)
    rows = []
    for f in json.loads(path.read_text())["features"]:
        p = f.get("properties") or {}
        faa_type = faa_type_of(p)
        if faa_type not in faa_types:
            continue
        pos = position(f)
        if pos is None or not area.contains(Point(pos)):
            continue
        if not is_operational(p) or is_military(p):
            continue
        site_type = SITE_TYPE_BY_FAA.get(faa_type, faa_type.lower())
        ident = ident_of(p)
        name = str(prop(p, "name") or "").strip()
        hospital = bool(HOSPITAL_WORDS.search(name))
        nearest, km = "", ""
        if waypoints and to_utm is not None:
            here = Point(*to_utm.transform(*pos))
            nearest = min(waypoints, key=lambda w: waypoints[w].distance(here))
            km = round(waypoints[nearest].distance(here) / 1000, 1)
        rows.append(
            {
                "keep": "",
                "id": f"{ID_PREFIX.get(site_type, 'xx')}-{ident.lower() or 'unknown'}",
                "name": string.capwords(name.lower()),
                "type": site_type,
                "lat": round(pos[1], 5),
                "lon": round(pos[0], 5),
                "pads": 1 if site_type == "helipad" else 2,
                "source": f"FAA ADDS Airports {ident}".strip(),
                "verified": "no",
                "notes": "",
                "faa_ident": ident,
                "faa_type": faa_type,
                "private_use": "yes" if is_private(p) else "no",
                "city": string.capwords(str(prop(p, "servcity") or prop(p, "city") or "").strip().lower()),
                "county": str(prop(p, "county") or "").strip(),
                "hospital_like": "yes" if hospital else "",
                "nearest_waypoint": nearest,
                "km_to_waypoint": km,
            }
        )
    # Hospital-looking pads first, then by name.
    rows.sort(key=lambda r: (r["hospital_like"] != "yes", r["name"]))
    return rows


def write_review(rows: list[dict[str, Any]], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=REVIEW_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def to_yaml(review: Path) -> str:
    """network.yaml `nodes:` lines for every row with keep = yes."""
    lines = []
    seen: set[str] = set()
    with review.open(newline="") as fh:
        for r in csv.DictReader(fh):
            if r.get("keep", "").strip().lower() not in ("yes", "y", "x", "1"):
                continue
            nid = r["id"].strip()
            if nid in seen:
                raise ValueError(f"duplicate id {nid!r} in {review.name}")
            seen.add(nid)
            types = [t.strip() for t in re.split(r"[;,]", r["type"]) if t.strip()]
            fields = [
                f"types: [{', '.join(types)}]",
                f"name: {json.dumps(r['name'])}",
                f"lat: {float(r['lat']):.5f}",
                f"lon: {float(r['lon']):.5f}",
                f"pads: {int(r['pads'] or 1)}",
                f"source: {json.dumps(r['source'])}",
                f"verified: {'true' if r['verified'].strip().lower() in ('yes', 'y', 'true') else 'false'}",
            ]
            if r.get("notes", "").strip():
                fields.append(f"notes: {json.dumps(r['notes'].strip())}")
            lines.append(f"  {nid}: {{ {', '.join(fields)} }}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_inspect = sub.add_parser("inspect", help="print fields and value counts")
    p_inspect.add_argument("src", type=Path)

    p_cand = sub.add_parser("candidates", help="write a review CSV of candidate sites")
    p_cand.add_argument("src", type=Path)
    p_cand.add_argument("--out", type=Path, required=True)
    p_cand.add_argument("--region", choices=REGIONS, default="central-fl")
    p_cand.add_argument("--types", default="HP", help="FAA TYPE_CODEs, comma-separated (HP, AD, ...)")
    p_cand.add_argument("--network", type=Path, help="network.yaml, to add nearest waypoint + distance")

    p_yaml = sub.add_parser("to-yaml", help="print network.yaml node lines for kept rows")
    p_yaml.add_argument("review", type=Path)

    a = ap.parse_args()
    if a.cmd == "inspect":
        inspect(a.src)
    elif a.cmd == "candidates":
        faa_types = [t.strip().upper() for t in a.types.split(",") if t.strip()]
        rows = candidates(a.src, a.region, faa_types, a.network)
        write_review(rows, a.out)
        hospital = sum(r["hospital_like"] == "yes" for r in rows)
        print(f"{len(rows)} candidates ({hospital} look like hospital pads) in {a.region} -> {a.out}")
        if not rows:
            print("nothing matched: run `inspect` and check TYPE_CODE values", file=sys.stderr)
    else:
        print(to_yaml(a.review))


if __name__ == "__main__":
    main()
