"""In-process pub/sub that carries a unit's messages to its paired screens (ARCHITECTURE §11.2).

Keyed by aircraft id. Every outbound MQTT message for the unit (`route-response`,
`flight-status`, `traffic`) is also handed here, so a screen sees exactly what the unit gets.
Device events (`online`, `offline`, `vehicle-type`) are published here too.

Each WebSocket subscribes with a bounded queue; a slow screen loses its oldest events rather
than slowing the backend down (the `state` message on reconnect repairs any gap).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from aerolink.service.topics import OUTBOUND, parse

log = logging.getLogger("aerolink.hub")
Outbound = tuple[str, bytes]  # same shape as aerolink.service.core.Outbound
QUEUE_SIZE = 200
OPS = "*ops*"  # the operations channel's key; never a valid aircraft id


class Hub:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        # U-API §4: the app sets this; its list is added to a unit's screen ``traffic`` payload only (not to MQTT)
        self.adsb_source: Callable[[str], dict[str, Any]] | None = None
        self.vehicle_types: dict[str, str] = {}  # last registered type seen per unit, to spot changes
        self.online: dict[str, bool] = {}  # last status event per unit

    def subscribe(self, aircraft_id: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(QUEUE_SIZE)
        self._subs.setdefault(aircraft_id, set()).add(q)
        return q

    def unsubscribe(self, aircraft_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        subs = self._subs.get(aircraft_id)
        if subs:
            subs.discard(q)
            if not subs:
                del self._subs[aircraft_id]

    def subscribers(self, aircraft_id: str) -> int:
        return len(self._subs.get(aircraft_id, ()))

    def publish(self, aircraft_id: str, type_: str, payload: dict[str, Any]) -> None:
        event = {"type": type_, "payload": payload}
        for q in list(self._subs.get(aircraft_id, ())):
            if q.full():
                q.get_nowait()  # drop the oldest
            q.put_nowait(event)

    def subscribe_ops(self) -> asyncio.Queue[dict[str, Any]]:
        """Operations channel (``/ws/ops``): alerts, acknowledgements and watchdog changes."""
        return self.subscribe(OPS)

    def unsubscribe_ops(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self.unsubscribe(OPS, q)

    def publish_ops(self, type_: str, payload: dict[str, Any]) -> None:
        self.publish(OPS, type_, payload)

    def publish_messages(self, messages: list[Outbound]) -> None:
        """Hand outbound MQTT messages (topic, JSON bytes) to the screens of the unit they address."""
        for topic, payload in messages:
            t = parse(topic)
            if t is None or t.kind not in OUTBOUND or t.source != "aircraft" or t.aircraft_id not in self._subs:
                continue
            try:
                data = json.loads(payload)
                if t.kind == "traffic" and self.adsb_source is not None and isinstance(data, dict):
                    data.update(
                        self.adsb_source(t.aircraft_id)
                    )  # adsb + adsb_state: the screens' copy only (MQTT is not)
                self.publish(t.aircraft_id, t.kind, data)
            except ValueError:
                log.warning("not publishing non-JSON payload on %s", topic)
