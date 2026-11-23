import csv
import json
from pathlib import Path
from typing import Any

import yaml

from aerolink.airspace.sites import candidates, to_yaml, write_review


def feature(ident: str, name: str, type_code: str, lon: float, lat: float, **extra: Any) -> dict[str, Any]:
    props = {
        "IDENT": ident,
        "NAME": name,
        "TYPE_CODE": type_code,
        "OPERSTATUS": "OPERATIONAL",
        "MIL_CODE": "CIVIL",
        "PRIVATEUSE": 1,
        "SERVCITY": "TAMPA",
        "STATE": "FL",
    }
    props.update(extra)
    return {"type": "Feature", "properties": props, "geometry": {"type": "Point", "coordinates": [lon, lat]}}


def write(tmp: Path, features: list[dict[str, Any]]) -> Path:
    p = tmp / "airports.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return p


def test_keeps_central_florida_helipads_only(tmp_path):
    src = write(
        tmp_path,
        [
            feature("FD12", "TAMPA GENERAL HOSPITAL", "HP", -82.459, 27.938),
            feature("VDF", "TAMPA EXECUTIVE", "AD", -82.345, 28.014),  # airport, not HP
            feature("XX01", "MIAMI ROOFTOP", "HP", -80.19, 25.77),  # outside focus
            feature("XX02", "CLOSED PAD", "HP", -81.95, 28.05, OPERSTATUS="CLOSED"),
            feature("XX03", "BASE PAD", "HP", -82.50, 27.85, MIL_CODE="MIL"),
            feature("XX04", "CORPORATE TOWER", "HP", -81.38, 28.54),
        ],
    )
    rows = candidates(src, "central-fl", ["HP"])
    assert [r["faa_ident"] for r in rows] == ["FD12", "XX04"]  # hospital first
    tgh = rows[0]
    assert tgh["id"] == "hp-fd12" and tgh["type"] == "helipad" and tgh["hospital_like"] == "yes"
    assert tgh["private_use"] == "yes"  # private pads are kept
    assert rows[1]["hospital_like"] == ""


def test_lowercase_fields_and_airport_type(tmp_path):
    f = feature("VDF", "TAMPA EXECUTIVE", "AD", -82.345, 28.014)
    f["properties"] = {k.lower(): v for k, v in f["properties"].items()}
    rows = candidates(write(tmp_path, [f]), "central-fl", ["AD"])
    assert rows[0]["id"] == "ev-vdf" and rows[0]["type"] == "evtol"


def test_review_to_yaml_round_trip(tmp_path):
    src = write(
        tmp_path,
        [
            feature("FD12", "TAMPA GENERAL HOSPITAL", "HP", -82.459, 27.938),
            feature("XX04", "CORPORATE TOWER", "HP", -81.38, 28.54),
        ],
    )
    review = tmp_path / "review.csv"
    write_review(candidates(src, "central-fl", ["HP"]), review)

    # Simulate the human review: keep TGH only, mark it verified.
    rows = list(csv.DictReader(review.open()))
    rows[0].update(keep="yes", verified="yes", notes="rooftop pad")
    with review.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)

    nodes = yaml.safe_load("nodes:\n" + to_yaml(review))["nodes"]
    assert list(nodes) == ["hp-fd12"]
    n = nodes["hp-fd12"]
    assert n["types"] == ["helipad"] and n["verified"] is True and n["name"] == "Tampa General Hospital"
    assert abs(n["lat"] - 27.938) < 1e-6 and n["notes"] == "rooftop pad"


def fdem(name: str, kind: str, lon: float, lat: float, **extra: Any) -> dict[str, Any]:
    props = {
        "Name": name,
        "FACILITY_T": kind,
        "ACODE": "61FL",
        "UAID": "FL000123",
        "FAC_STATUS": "Active",
        "AOWNERSHIP": "Private",
        "AUSE": "Private",
        "City": " ",
        "COUNTY": "Hillsborough",
    }
    props.update(extra)
    return {"type": "Feature", "properties": props, "geometry": {"type": "Point", "coordinates": [lon, lat]}}


def test_fdem_format_and_nearest_waypoint(tmp_path):
    src = write(
        tmp_path,
        [
            fdem("TAMPA GENERAL HOSPITAL HELIPORT", "HELIPORT/HELIPAD", -82.45947, 27.93926),
            fdem("CARE FLIGHT HELIPORT", "HELIPORT/HELIPAD", -82.49, 27.98363, ACODE=" "),  # no FAA id -> UAID
            fdem("TAMPA EXECUTIVE", "AIRPORT", -82.345, 28.014),  # airport, not HP
            fdem("NAVY PAD", "HELIPORT/HELIPAD", -82.5, 27.85, AOWNERSHIP="Military Navy"),
        ],
    )
    network = tmp_path / "network.yaml"
    network.write_text("nodes:\n  wp-i4-i75: { types: [waypoint], lat: 27.94, lon: -82.46 }\n")
    rows = candidates(src, "central-fl", ["HP"], network)
    assert [r["id"] for r in rows] == ["hp-fl000123", "hp-61fl"]  # both medical-looking; sorted by name
    assert all(r["hospital_like"] == "yes" for r in rows)
    tgh = next(r for r in rows if r["id"] == "hp-61fl")
    assert tgh["nearest_waypoint"] == "wp-i4-i75" and tgh["km_to_waypoint"] < 1
    assert tgh["county"] == "Hillsborough" and tgh["private_use"] == "yes"
