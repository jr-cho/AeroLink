"""U1 story 1: per-leg predicted traffic level at the ETA (U-API §1).

``route_segments`` is pure; the planner cases run on the committed Central Florida network with the cv forecaster
(no model files) and check the JSON shape against the route-response schema.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from conftest import validator

from aerolink.contracts import RouteResponse
from aerolink.network import Network
from aerolink.service.ai import route_segments
from aerolink.service.planner import Planner
from aerolink.service.settings import Settings
from aerolink.traffic import forecast_traffic

T0 = 1_800_000_000.0


def test_route_segments_one_entry_per_leg_with_arrival_time():
    seen: list[tuple[str, str, float]] = []

    def level(u: str, v: str, t: float) -> str:
        seen.append((u, v, t))
        return "moderate" if v == "b" else "low"

    segs = route_segments(["a", "b", "c"], {"b": 100.0, "c": 250.0}, level)
    assert segs == [
        {"from": "a", "to": "b", "eta_s": 100.0, "level": "moderate"},
        {"from": "b", "to": "c", "eta_s": 250.0, "level": "low"},
    ]
    assert seen == [("a", "b", 100.0), ("b", "c", 250.0)]  # the level is asked at the far end's ETA


def test_route_segments_skips_a_leg_without_an_eta_instead_of_guessing():
    segs = route_segments(["a", "b", "c"], {"c": 250.0}, lambda u, v, t: "low")
    assert [(s["from"], s["to"]) for s in segs] == [("b", "c")]


def test_route_segments_of_a_single_node_path_is_empty():
    assert route_segments(["a"], {"a": 1.0}, lambda u, v, t: "low") == []


def test_forecast_heavy_at_arrival_shows_on_the_leg_arriving_then(fl_network: Network):
    """Constructed traffic: five expected arrivals at one node around T0+600 make it heavy then, not hours later."""
    p = Planner(fl_network, Settings(method="classical", forecaster="cv"))
    node = next(n for n in fl_network.nodes if fl_network.graph.degree(n) > 1)
    expected = [(1.0, node, T0 + 540, T0 + 600, T0 + 660)] * 5
    forecast = forecast_traffic([], {node: 1}, expected)
    heavy = p.edge_levels(forecast, node, node, T0 + 600)
    later = p.edge_levels(forecast, node, node, T0 + 7200)
    assert heavy == "heavy"
    assert later == "low"


@pytest.mark.parametrize("method", ["classical", "aerolink"])
def test_planner_route_carries_one_segment_per_leg(fl_network: Network, method: str):
    p = Planner(fl_network, Settings(method=method))
    dest = fl_network.nodes_for("medical")[0]
    resp = p.plan(
        request_id=uuid4(), aircraft_id="dev-001", vehicle_type="medical", origin="ev-sef",
        destination=dest, now=datetime.fromtimestamp(T0, UTC), planned=[],
    )  # fmt: skip
    assert resp.status == "ok"
    for route in resp.routes:
        eta = {e.node: e.eta_p50.timestamp() for e in route.eta}
        assert [s.to for s in route.segments] == list(eta)  # one leg into each ETA node, in route order
        assert all(a.to == b.from_ for a, b in zip(route.segments, route.segments[1:], strict=False))  # a chain
        assert len(route.segments) == len(route.eta)  # every leg has an arrival time
        for s in route.segments:
            assert s.eta_s == pytest.approx(eta[s.to])
            assert s.level in ("low", "moderate", "heavy")


def test_route_response_json_uses_the_contract_key_from(fl_network: Network):
    p = Planner(fl_network, Settings(method="classical", forecaster="cv"))
    resp = p.plan(
        request_id=uuid4(), aircraft_id="dev-001", vehicle_type="medical", origin="ev-sef",
        destination=fl_network.nodes_for("medical")[0], now=datetime.fromtimestamp(T0, UTC), planned=[],
    )  # fmt: skip
    data = resp.model_dump(mode="json")
    first = data["routes"][0]["segments"][0]
    assert set(first) == {"from", "to", "eta_s", "level"}
    assert not list(validator("route-response").iter_errors(data))  # schema accepts it
    RouteResponse.model_validate_json(resp.model_dump_json())  # and the model reads it back


def test_traffic_endpoint_carries_by_minute_levels_on_nodes_and_edges(tmp_path):
    """U-API §1: ``by_minute`` (new key) reads the same 5-minute list at +0, +15, +30, +45; ``levels`` is unchanged."""
    import asyncio

    from fastapi.testclient import TestClient

    from aerolink.service.app import create_app
    from aerolink.service.settings import Settings
    from aerolink.service.store import Store

    url = f"sqlite+aiosqlite:///{tmp_path / 'traffic.db'}"

    async def make_tables() -> None:
        store = Store.connect(url)
        await store.create_all()
        await store.close()

    asyncio.run(make_tables())
    app = create_app(Settings(database_url=url), mqtt=False)
    with TestClient(app) as c:
        body = c.get("/api/traffic").json()
    for entry in [*body["nodes"].values(), *body["edges"].values()][:50]:
        lv = entry["levels"]
        assert len(lv) == 10
        assert entry["by_minute"] == {"0": lv[0], "15": lv[3], "30": lv[6], "45": lv[9]}


def test_device_traffic_needs_a_pilot_session_and_matches_api_traffic(tmp_path):
    """U-API §5: the pilot-side copy of /api/traffic sits behind the screen's session, with the same payload."""
    import asyncio

    from fastapi.testclient import TestClient

    from aerolink.service.app import create_app
    from aerolink.service.devices import provision
    from aerolink.service.settings import Settings
    from aerolink.service.store import Store

    url = f"sqlite+aiosqlite:///{tmp_path / 'dtraffic.db'}"

    async def prepare() -> str:
        store = Store.connect(url)
        await store.create_all()
        code = (await provision(store, "dev-001", "passenger", False, "https://aerolink.test", tmp_path / "labels"))[
            "code"
        ]
        await store.close()
        return code

    code = asyncio.run(prepare())
    app = create_app(Settings(database_url=url, forecaster="cv", suggestion_interval_s=0), mqtt=False)
    with TestClient(app, base_url="https://aerolink.test") as c:
        assert c.get("/api/devices/dev-001/traffic").status_code == 401
        assert c.post("/api/devices/dev-001/pair", json={"code": code}).status_code == 200
        pilot = c.get("/api/devices/dev-001/traffic")
        assert pilot.status_code == 200
        operator_side = c.get("/api/traffic").json()
    body = pilot.json()
    assert set(body) == set(operator_side)  # the same fields: forecast_time, nodes, edges, cones, levels, by_minute ...
    assert body["nodes"] == operator_side["nodes"] and body["edges"] == operator_side["edges"]
    assert body["steps_min"] == operator_side["steps_min"]
    assert all("by_minute" in e for e in list(body["nodes"].values())[:5])
