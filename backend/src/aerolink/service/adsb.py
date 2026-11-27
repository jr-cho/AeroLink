"""Wiring for live ADS-B: ``GET /api/adsb`` and the poll task (shape documented in ``aerolink.adsb_live``)."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from aerolink.adsb_live import AdsbFeed, AdsbSettings
from aerolink.service.hub import OPS

router = APIRouter()
VIEWER_PREFIXES = ("/api/adsb", "/api/ops/", "/ws/ops")  # using any of these means someone is looking at traffic


class ViewerActivity:
    """ASGI middleware: a request or socket on a viewer path switches the ADS-B poll to the active rate."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket") and scope.get("path", "").startswith(VIEWER_PREFIXES):
            feed = getattr(scope["app"].state, "adsb", None) if "app" in scope else None
            if hasattr(feed, "touch"):
                feed.touch()
        await self.app(scope, receive, send)


def start_adsb(app: Any) -> asyncio.Task | None:
    """Create ``app.state.adsb`` and start polling; the planner may read ``app.state.adsb`` if present."""
    app.state.adsb = AdsbFeed(AdsbSettings.from_env())
    hub = getattr(app.state, "hub", None)
    if hub is not None:  # an open /ws/ops socket keeps the feed at the active rate however long it stays open
        app.state.adsb.viewers = lambda: hub.subscribers(OPS) > 0
    return asyncio.create_task(app.state.adsb.run()) if app.state.adsb.settings.enabled else None


@router.get("/api/adsb")
async def get_adsb(request: Request) -> dict[str, Any]:
    feed: AdsbFeed | None = getattr(request.app.state, "adsb", None)
    if hasattr(feed, "touch"):
        feed.touch()
    return (feed or AdsbFeed(AdsbSettings(enabled=False))).snapshot()
