"""Shared helpers: schema registry and example loading."""

import json
import os

# Torch (Chronos) and LightGBM each ship their own OpenMP runtime; on macOS loading both in one process segfaults or
# hangs unless OpenMP is single-threaded. Set before any test imports either library (found in E1b).
os.environ.setdefault("OMP_NUM_THREADS", "1")

from pathlib import Path  # noqa: E402
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.protocols import Validator
from referencing import Registry, Resource

REPO = Path(__file__).resolve().parents[2]
SCHEMAS = REPO / "contracts" / "schemas"
EXAMPLES = REPO / "contracts" / "examples"


def need[T](x: T | None) -> T:
    """Narrow Optional to T for tests that expect a hit (a bare `assert` message-free failure otherwise)."""
    assert x is not None
    return x


def grab(pattern: str, text: str, group: int = 1) -> str:
    """`re.search(...).group(n)` that fails the test with a clear message when there is no match."""
    import re

    m = re.search(pattern, text)
    assert m is not None, f"{pattern!r} not found in {text!r}"
    return m.group(group)


def portal_of(c: Any) -> Any:
    """The TestClient's anyio portal; non-None inside `with TestClient(app)` (typed so ty accepts `.call`)."""
    assert c.portal is not None, "TestClient must be used as a context manager"
    return c.portal


def app_state(c: Any) -> Any:
    """`c.app.state` (starlette State); `TestClient.app` is typed as a bare ASGI callable."""
    return c.app.state


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def _registry() -> Registry:
    resources = []
    for path in SCHEMAS.glob("*.schema.json"):
        schema = load(path)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


REGISTRY = _registry()


def validator(name: str) -> Validator:
    return Draft202012Validator(
        load(SCHEMAS / f"{name}.schema.json"),
        registry=REGISTRY,
        format_checker=Draft202012Validator.FORMAT_CHECKER,  # enforce uuid / date-time
    )


# --------------------------------------------------------------------------- sim
import networkx as nx  # noqa: E402
import pytest  # noqa: E402

from aerolink.forecast.cv import CVForecaster  # noqa: E402
from aerolink.network import Edge, Network  # noqa: E402
from aerolink.routing.a1 import A1Advisor, A1Config  # noqa: E402

NETWORK_FILE = REPO / "airspace" / "central-fl-v1.geojson"


@pytest.fixture(scope="session")
def fl_network() -> Network:
    """The committed Central Florida network (read-only: do not mutate)."""
    return Network.from_geojson(NETWORK_FILE)


@pytest.fixture(scope="session")
def fl_a1(fl_network: Network) -> A1Advisor:
    """A1 advisor over the committed Central Florida network."""
    return A1Advisor(fl_network, CVForecaster(fl_network), cfg=A1Config())


def make_network(nodes: dict[str, dict], edges: list[tuple[str, str, float]], band=(2500, 3000)) -> Network:
    """Tiny synthetic network. ``nodes``: id -> {lat, lon, types, serves, pads};
    ``edges``: (source, target, length_m) with straight-line geometry."""
    graph = nx.Graph()
    node_props: dict[str, dict[str, Any]] = {}
    for nid, p in nodes.items():
        props = {"types": ["waypoint"], "serves": [], **p}
        node_props[nid] = props
        graph.add_node(nid)
    edge_map: dict[tuple[str, str], Edge] = {}
    for i, (u, v, length) in enumerate(edges):
        e = Edge(
            edge_id=f"e{i}",
            kind="corridor",
            source=u,
            target=v,
            length_m=length,
            band_floor_ft=band[0],
            band_ceiling_ft=band[1],
            highway=None,
            geometry=(
                (float(node_props[u]["lon"]), float(node_props[u]["lat"])),
                (float(node_props[v]["lon"]), float(node_props[v]["lat"])),
            ),
        )
        edge_map[(u, v)] = e
        edge_map[(v, u)] = e
        graph.add_edge(u, v, length_m=length, edge_id=e.edge_id, band_floor_ft=band[0], band_ceiling_ft=band[1])
    return Network(graph, node_props, edge_map)


def site(lat: float, lon: float, serves: list[str], pads: int = 1, kind: str = "evtol") -> dict:
    return {"lat": lat, "lon": lon, "types": [kind], "serves": serves, "pads": pads}


def wp(lat: float, lon: float) -> dict:
    return {"lat": lat, "lon": lon, "types": ["waypoint"], "serves": []}


@pytest.fixture
def line_network() -> Network:
    """A --- W1 --- W2 --- B  (passenger sites A, B; medical site H off W1; C off W2).

    Distances ~ true haversine so geometry and length agree (0.01 deg lat ~ 1113 m).
    """
    from aerolink.sim.physics import segment_length_m

    nodes = {
        "a": site(28.00, -81.50, ["passenger"], pads=1),
        "w1": wp(28.05, -81.50),
        "w2": wp(28.10, -81.50),
        "b": site(28.15, -81.50, ["passenger"], pads=1),
        "c": site(28.10, -81.45, ["passenger"], pads=2),
        "h": site(28.05, -81.45, ["medical"], pads=1, kind="helipad"),
        "h2": site(28.15, -81.45, ["medical"], pads=1, kind="helipad"),
    }
    pairs = [("a", "w1"), ("w1", "w2"), ("w2", "b"), ("w2", "c"), ("w1", "h"), ("w2", "h2")]
    edges = [
        (u, v, segment_length_m((nodes[u]["lon"], nodes[u]["lat"]), (nodes[v]["lon"], nodes[v]["lat"])))
        for u, v in pairs
    ]
    return make_network(nodes, edges)
