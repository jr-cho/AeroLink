"""FastAPI application: wires settings, store, planner, service, MQTT link and loops."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator

from fastapi import FastAPI

from aerolink.network import Network
from aerolink.service import ops_overview
from aerolink.service.adsb import ViewerActivity, start_adsb
from aerolink.service.adsb import router as adsb_router
from aerolink.service.ai import pilot_screen_flying, unit_adsb_view
from aerolink.service.ai_api import router as ai_router
from aerolink.service.api import build_airspace, router
from aerolink.service.clearance import load_legs
from aerolink.service.clearance import router as clearance_router
from aerolink.service.core import Service
from aerolink.service.hub import Hub
from aerolink.service.models import ModelRegistry
from aerolink.service.mqtt import MqttLink
from aerolink.service.ops import router as ops_router
from aerolink.service.pilot import router as pilot_router
from aerolink.service.planner import Planner
from aerolink.service.reroute import router as reroute_router
from aerolink.service.settings import Settings
from aerolink.service.store import Store
from aerolink.service.watchdogs import Watchdogs

log = logging.getLogger("aerolink.service")


async def _every(seconds: float, fn) -> None:
    while True:
        await asyncio.sleep(seconds)
        try:
            await fn()
        except Exception:
            log.exception("loop %s failed", getattr(fn, "__name__", fn))


def create_app(settings: Settings | None = None, store: Store | None = None, mqtt: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        st = store or Store.connect(settings.database_url)
        network = await asyncio.to_thread(Network.from_geojson, settings.network_file)  # load once at startup
        app.state.airspace = await asyncio.to_thread(build_airspace, settings)  # cached for /api/airspace
        app.state.atc_legs = await asyncio.to_thread(load_legs, settings.network_file)  # E6.14 leg flags
        registry = ModelRegistry(network, settings)
        # A worker thread: building the planner may load the default forecaster.
        planner = await asyncio.to_thread(Planner, network, settings, None, registry)
        service = Service(st, planner)
        service.atc_legs = app.state.atc_legs
        await service.start()
        hub = Hub()  # carries a unit's messages to its paired screens (E3) and alerts to /ws/ops (E6.9)
        service.events.hub = hub
        hub.adsb_source = lambda aid: unit_adsb_view(
            app.state, aid
        )  # pilot traffic gets live ADS-B + its state (§4, §9)
        link = MqttLink(settings, service, hub) if mqtt else None
        service.publish = link.publish if link else None
        app.state.service, app.state.link, app.state.hub = service, link, hub
        app.state.ws_heartbeat_s = settings.ws_heartbeat_s

        async def preload() -> None:
            """All models, once, off the event loop; the API is already serving (cv works at once)."""
            await asyncio.to_thread(registry.load_all)
            await service.refresh_forecast()  # now with the demand model

        async def expire() -> None:
            service.expire_tracks()

        watchdogs = Watchdogs(service, settings, link, hub, registry=registry)
        app.state.watchdogs = watchdogs
        tasks = [asyncio.create_task(_every(settings.forecast_interval_s, service.refresh_forecast))]
        tasks.append(asyncio.create_task(_every(1.0, service.check_units)))
        tasks.append(asyncio.create_task(_every(settings.watchdog_interval_s, watchdogs.evaluate)))
        tasks.append(asyncio.create_task(_every(5.0, expire)))
        if settings.preload_models:
            tasks.append(asyncio.create_task(preload()))
        tasks.append(asyncio.create_task(_every(settings.reroute_interval_s, service.reroute_scan)))
        tasks.append(asyncio.create_task(_every(ops_overview.REFRESH_S, lambda: ops_overview.refresh_overview(app))))
        if settings.suggestion_interval_s > 0:

            async def publish_suggestions() -> None:
                await link.publish(await service.suggest()) if link else None

            tasks.append(asyncio.create_task(_every(settings.suggestion_interval_s, publish_suggestions)))
        if link:

            async def publish_traffic() -> None:
                await link.publish(service.traffic_messages())

            async def publish_off_route() -> None:
                await link.publish(await service.scan_off_route())

            tasks += [
                asyncio.create_task(_every(settings.offroute_interval_s, publish_off_route)),
                asyncio.create_task(_every(1.0, link.check_silence)),
                asyncio.create_task(link.run()),
                asyncio.create_task(link.worker()),
                asyncio.create_task(_every(settings.traffic_interval_s, publish_traffic)),
            ]
        if mqtt:  # live mode only: unit tests that build the app without a broker never touch the network
            if (adsb_task := start_adsb(app)) is not None:
                tasks.append(adsb_task)
            planner.adsb = watchdogs.adsb = getattr(app.state, "adsb", None)  # forecast input and a watchdog
            if (feed := getattr(app.state, "adsb", None)) is not None:  # C-03: a paired pilot screen counts as a viewer
                ops_viewer = feed.viewers

                def viewed() -> bool:
                    return bool(ops_viewer and ops_viewer()) or pilot_screen_flying(service, hub)

                feed.viewers = viewed
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if store is None:
                await st.close()

    app = FastAPI(title="AeroLink backend", version="0.1.0", lifespan=lifespan)
    app.add_middleware(ViewerActivity)  # ADS-B polls fast only while someone is looking
    app.include_router(router)
    app.include_router(pilot_router)
    app.include_router(adsb_router)
    app.include_router(ops_router)
    app.include_router(clearance_router)
    app.include_router(reroute_router)
    app.include_router(ai_router)
    app.include_router(ops_overview.router)  # R1: /api/ops/overview and /api/ops/flights/{id}/overview
    return app
