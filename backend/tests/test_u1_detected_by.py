"""U1 contract §7: off-route flight-status and the screens' flight state say who flagged it: ``ai`` or ``rule``.

Both paths run the real ``Service.scan_off_route``: the classical fixed rule (B1), and the AI path with the learned
detector stubbed to flag (the detector itself is evaluated in test_e55_offroute; this checks the wiring).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from conftest import validator
from test_service import T0, Clock, route_request, selection, telemetry

from aerolink.contracts import RouteResponse
from aerolink.network import Network
from aerolink.service.core import Service
from aerolink.service.pilot import device_view
from aerolink.service.planner import Planner
from aerolink.service.settings import Settings
from aerolink.service.store import Store
from aerolink.service.topics import Topic

DEV = "dev-001"


async def flying(tmp_path: Path, network: Network, method: str):
    """A passenger unit that has taken off and is now 3 km off its planned route (both samples off-route)."""
    clock = Clock()
    store = Store.connect(f"sqlite+aiosqlite:///{tmp_path / f'{method}.db'}")
    await store.create_all()
    svc = Service(store, Planner(network, Settings(method=method, forecaster="cv")), clock=clock)
    await svc.start()
    await store.register_device(DEV, "passenger", callsign="N999AL")
    origin = "ev-sef"
    lat, lon = network.nodes[origin]["lat"], network.nodes[origin]["lon"]
    await svc.handle(str(Topic("aircraft", DEV, "telemetry")), telemetry(DEV, lat, lon, T0))
    dest = "ev-parramore"
    out = await svc.handle(str(Topic("aircraft", DEV, "route-request")), route_request(DEV, dest))
    resp = RouteResponse.model_validate_json(out[0][1])
    await svc.handle(
        str(Topic("aircraft", DEV, "route-selection")),
        selection(DEV, str(resp.request_id), str(resp.routes[0].route_id)),
    )
    off_lat = lat + 3000.0 / 111_320.0  # 3 km north of the origin, well outside the 300 m corridor rule
    for k in (1, 2):  # speed above 5 m/s turns the planned flight active; two samples make the track judgeable
        t = T0.replace(second=k)
        await svc.handle(str(Topic("aircraft", DEV, "telemetry")), telemetry(DEV, off_lat, lon, t, speed=10.0))
    clock.t = T0.replace(second=3)
    return svc


def flight_status_of(out) -> list[dict]:
    return [json.loads(p) for _, p in out if json.loads(p).get("status") == "off_route"]


def test_classical_rule_flags_are_detected_by_rule(tmp_path: Path, fl_network: Network):
    import asyncio

    async def go() -> None:
        svc = await flying(tmp_path, fl_network, "classical")
        out = await svc.scan_off_route()
        msgs = flight_status_of(out)
        assert msgs, "the fixed corridor rule must flag a unit 3 km off its route"
        assert msgs[0]["detected_by"] == "rule"
        assert not list(validator("flight-status").iter_errors(msgs[0]))
        assert svc.off_route_by[DEV] == "rule"
        app_state = SimpleNamespace(service=svc, hub=SimpleNamespace(vehicle_types={}))
        dev = await svc.store.get_device(DEV)
        view = await device_view(app_state, dev)
        assert view["current_flight"]["status"] == "off_route"
        assert view["current_flight"]["detected_by"] == "rule"

    asyncio.run(go())


def test_ai_path_flags_are_detected_by_ai(tmp_path: Path, fl_network: Network, monkeypatch):
    import asyncio

    async def go() -> None:
        svc = await flying(tmp_path, fl_network, "aerolink")
        monkeypatch.setattr(svc.planner, "detect_off_route", lambda *a, **k: True)  # the learned detector says yes
        out = await svc.scan_off_route()
        msgs = flight_status_of(out)
        assert msgs and msgs[0]["detected_by"] == "ai"
        assert not list(validator("flight-status").iter_errors(msgs[0]))
        assert svc.off_route_by[DEV] == "ai"
        app_state = SimpleNamespace(service=svc, hub=SimpleNamespace(vehicle_types={}))
        view = await device_view(app_state, await svc.store.get_device(DEV))
        assert view["current_flight"]["detected_by"] == "ai"

    asyncio.run(go())


def test_detected_by_is_null_when_on_route_and_not_in_the_flight_state(tmp_path: Path, fl_network: Network):
    import asyncio

    async def go() -> None:
        svc = await flying(tmp_path, fl_network, "classical")
        # an ordinary (active, on-route) status message carries no detector
        row = next(iter(svc.flights.values()))
        msg = json.loads(svc._status_msg(row, "active").model_dump_json())
        assert msg["detected_by"] is None
        assert not list(validator("flight-status").iter_errors(msg))

    asyncio.run(go())
