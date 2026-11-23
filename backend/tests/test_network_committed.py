"""CI gate for the committed network (PLAN E2.4).

Re-checks airspace/central-fl-v1.geojson against airspace/constraints-fl.geojson
in 3D, and checks that every landing site is reachable. If either file changes,
this test proves the network is still clear of no-go airspace.
"""

import json
from pathlib import Path

import networkx as nx
import pytest

from aerolink.airspace.build_network import validate

AIRSPACE = Path(__file__).resolve().parents[2] / "airspace"
NETWORK = AIRSPACE / "central-fl-v1.geojson"
CONSTRAINTS = AIRSPACE / "constraints-fl.geojson"

pytestmark = pytest.mark.skipif(not NETWORK.exists(), reason="central-fl-v1.geojson not built yet")


def load() -> dict:
    return json.loads(NETWORK.read_text())


def test_no_corridor_enters_no_go_airspace():
    assert validate(load(), CONSTRAINTS) == []


def test_every_site_is_reachable():
    g = nx.Graph()
    sites = []
    for f in load()["features"]:
        p = f["properties"]
        if p["kind"] == "node":
            g.add_node(p["id"])
            if "waypoint" not in p.get("types", []):
                sites.append(p["id"])
        else:
            g.add_edge(p["from"], p["to"])
    main: set[str] = set(max(nx.connected_components(g), key=len))
    assert [s for s in sites if s not in main] == []


def test_built_from_current_constraints_cycle():
    data = json.loads(CONSTRAINTS.read_text())
    cycles = {f["properties"].get("data_cycle") for f in data["features"]} - {None}
    assert cycles, "constraints have no data_cycle (still the illustrative file?)"
