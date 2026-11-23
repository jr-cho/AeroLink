from typing import Any

from aerolink.airspace.convert import convert_feature

# R-2901K (Avon Park restricted area), as exported by FAA ADDS.
R2901K_PROPS: dict[str, Any] = {
    "OBJECTID": 1114,
    "GLOBAL_ID": "8E172819-87DC-415F-A166-5677B1E5C09F",
    "NAME": "R-2901K",
    "TYPE_CODE": "R",
    "CLASS": None,
    "UPPER_VAL": "310",
    "UPPER_UOM": "FL",
    "UPPER_CODE": "STD",
    "LOWER_VAL": "230",
    "LOWER_UOM": "FL",
    "LOWER_CODE": "STD",
    "TIMESOFUSE": "INTERMITTENT, 0600 - 2400, MON - FRI",
}

R2901K_GEOMETRY: dict[str, Any] = {
    "type": "Polygon",
    "coordinates": [
        [
            [-81.2330586042359, 28.0002829938351, 0.0],
            [-81.3497253041835, 28.0002829908913, 0.0],
            [-81.4219475445215, 27.9169496347934, 0.0],
            [-81.4219475376844, 27.7002829159543, 0.0],
            [-81.099725209077, 27.2794494865313, 0.0],
            [-80.99972518209, 27.3502828403886, 0.0],
            [-81.2330585961894, 27.7461162648344, 0.0],
            [-81.2330586042359, 28.0002829938351, 0.0],
        ]
    ],
}


def make_feature(**overrides: Any) -> dict[str, Any]:
    """R-2901K with some FAA properties replaced."""
    return {
        "type": "Feature",
        "properties": {**R2901K_PROPS, **overrides},
        "geometry": R2901K_GEOMETRY,
    }


def test_restricted_area_high_altitude():
    f, why = convert_feature(make_feature(), "sua", "2026-11-27")
    assert why is None and f is not None
    p = f["properties"]
    assert p["type"] == "restricted" and p["blocks"] is True
    assert p["floor_ft"] == 23000 and p["ceiling_ft"] == 31000  # FL230-FL310
    assert p["in_focus_region"] is True
    assert all(len(pt) == 2 for pt in f["geometry"]["coordinates"][0])  # height dropped


def test_surface_floor():
    f, _ = convert_feature(
        make_feature(
            LOWER_VAL="0",
            LOWER_CODE="SFC",
            UPPER_VAL="5000",
            UPPER_UOM="FT",
            UPPER_CODE="MSL",
        ),
        "sua",
        "c",
    )
    assert f is not None
    assert f["properties"]["floor_m"] == 0
    assert f["properties"]["ceiling_ft"] == 5000


def test_class_e_kept_but_not_blocking():
    f, _ = convert_feature(make_feature(CLASS="E"), "class", "c")
    assert f is not None
    assert f["properties"]["type"] == "class_e"
    assert f["properties"]["blocks"] is False


def test_unknown_type_skipped():
    f, why = convert_feature(make_feature(TYPE_CODE="ZZ"), "sua", "c")
    assert f is None and why is not None
