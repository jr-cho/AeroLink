"""Thin MQTT layer (aiomqtt): subscribe, feed Service.handle, publish what it returns.

Telemetry and status are handled inline (cheap). Route requests and selections go to a
worker queue so a slow route computation never stalls 1 Hz ingest (PLAN E6.1).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

import aiomqtt

from aerolink.service.core import Outbound, Service
from aerolink.service.hub import Hub
from aerolink.service.settings import Settings
from aerolink.service.topics import SUBSCRIPTIONS, parse

log = logging.getLogger("aerolink.mqtt")
SLOW_KINDS = ("/route-request", "/route-selection")


def payload_bytes(message: aiomqtt.Message) -> bytes:
    p = message.payload
    if isinstance(p, bytes | bytearray):
        return bytes(p)
    return b"" if p is None else str(p).encode()


class MqttLink:
    def __init__(
        self,
        settings: Settings,
        service: Service,
        hub: Hub | None = None,
        mono: Callable[[], float] = time.monotonic,
    ):
        self.mono = mono
        self.heard: dict[str, float] = {}  # unit -> monotonic time of its last message (the heartbeat)
        self.silent: set[str] = set()  # units the watchdog marked offline
        self.last_message: float | None = None  # monotonic time of the last message from the broker (watchdog)
        self.settings = settings
        self.service = service
        self.hub = hub or Hub()
        self.client: aiomqtt.Client | None = None
        self.work: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue(maxsize=1000)

    @property
    def connected(self) -> bool:
        return self.client is not None

    async def publish(self, messages: list[Outbound]) -> None:
        """Send to the unit over MQTT and to its paired screens over the hub (same messages)."""
        self.hub.publish_messages(messages)
        if self.client is None:
            return
        for topic, payload in messages:
            await self.client.publish(topic, payload, qos=1)

    async def _dispatch(self, topic: str, payload: bytes) -> None:
        try:
            await self._heard(topic, payload)
            await self.publish(await self.service.handle(topic, payload))
            await self._device_events(topic, payload)
        except Exception:  # one bad message must not kill the link
            log.exception("handling %s failed", topic)

    async def _heard(self, topic: str, payload: bytes) -> None:
        """Any telemetry or `status online` from a unit proves it is alive, valid or not (checked before validation).

        A telemetry message rejected for a bad GNSS fix still counts. A unit the watchdog had marked
        offline comes back at once.
        """
        t = parse(topic)
        if t is None or t.source != "aircraft":
            return
        if t.kind in ("telemetry", "health") or (t.kind == "status" and payload.strip().lower() == b"online"):
            self.heard[t.aircraft_id] = self.mono()
            if t.aircraft_id in self.silent:
                self.silent.discard(t.aircraft_id)
                await self.service.store.update_device(t.aircraft_id, online=True, last_seen=self.service.clock())
                self.service.live.note_reconnect(t.aircraft_id)  # it was gone: its next sample starts a new segment
                await self._set_online(t.aircraft_id, True)
        elif t.kind == "status":  # the Last Will: the fast path, nothing left for the watchdog
            self.heard.pop(t.aircraft_id, None)
            self.silent.discard(t.aircraft_id)

    async def check_silence(self) -> list[str]:
        """Mark units offline that have been silent too long; run about once a second. Returns their ids."""
        limit, now = self.settings.unit_silence_s, self.mono()
        gone = [u for u, at in self.heard.items() if now - at > limit]
        for aircraft_id in gone:
            del self.heard[aircraft_id]
            self.silent.add(aircraft_id)
            await self.service.store.update_device(aircraft_id, online=False)
            await self._set_online(aircraft_id, False, reason="no_signal")
        return gone

    async def _device_events(self, topic: str, payload: bytes) -> None:
        """Tell paired screens about online/offline (status + Last Will) and demo type changes.

        Runs after ``Service.handle`` so the store already holds the new state when a screen reacts.
        """
        t = parse(topic)
        if t is None or t.source != "aircraft":
            return
        if t.kind == "status":
            online = payload.strip().lower() == b"online"
            await self._set_online(t.aircraft_id, online)
        elif t.kind == "telemetry":
            await self._check_type_change(t.aircraft_id, payload)

    async def _set_online(self, aircraft_id: str, online: bool, reason: str | None = None) -> None:
        before = self.hub.online.get(aircraft_id)
        self.hub.online[aircraft_id] = online
        at = (self.service.clock() if self.service else datetime.now(UTC)).isoformat()
        payload = {"at": at, **({"reason": reason} if reason else {})}
        self.hub.publish(aircraft_id, "online" if online else "offline", payload)
        if self.service.events.hub is None:
            self.service.events.hub = self.hub  # tests build the link without the app
        if before != online:
            kind = "unit_online" if online else ("unit_no_signal" if reason == "no_signal" else "unit_offline")
            await self.service.events.record(
                kind,
                aircraft_id=aircraft_id,
                severity="info" if online else "warn",
                message=f"{aircraft_id} {kind.removeprefix('unit_').replace('_', ' ')}",
                detail={"reason": reason} if reason else {},
            )
        await self.service.check_unit(aircraft_id, online)  # the status changes with it; screens hear a `health` event

    async def _check_type_change(self, aircraft_id: str, payload: bytes) -> None:
        if self.hub.online.get(aircraft_id) is False:  # telemetry after a Last Will: the unit is back
            await self._set_online(aircraft_id, True)
        try:
            claimed = json.loads(payload).get("vehicle_type")
        except (ValueError, AttributeError):
            return
        known = self.hub.vehicle_types.get(aircraft_id)
        if claimed is None or claimed == known:
            return
        dev = await self.service.store.get_device(aircraft_id)
        if dev is None:
            return
        self.hub.vehicle_types[aircraft_id] = dev["vehicle_type"]
        if known is not None and dev["vehicle_type"] != known:
            self.hub.publish(aircraft_id, "vehicle-type", {"vehicle_type": dev["vehicle_type"], "demo": dev["demo"]})

    async def worker(self) -> None:
        while True:
            topic, payload = await self.work.get()
            await self._dispatch(topic, payload)
            self.work.task_done()

    async def run(self) -> None:
        """Connect, subscribe and consume forever; reconnect with backoff."""
        s, backoff = self.settings, 1.0
        while True:
            try:
                async with aiomqtt.Client(
                    s.mqtt_host,
                    s.mqtt_port,
                    username=s.mqtt_user,
                    password=s.mqtt_password or None,
                    identifier="aerolink-backend",
                    keepalive=10,  # a hung broker is noticed in about 15 s (the MQTT watchdog reads `connected`)
                ) as client:
                    for sub in SUBSCRIPTIONS:
                        await client.subscribe(sub, qos=1)
                    self.client, backoff = client, 1.0
                    log.info("connected to mqtt://%s:%s as %s", s.mqtt_host, s.mqtt_port, s.mqtt_user)
                    async for m in client.messages:
                        self.last_message = self.mono()
                        topic, payload = m.topic.value, payload_bytes(m)
                        if topic.endswith(SLOW_KINDS):
                            await self.work.put((topic, payload))
                        else:
                            await self._dispatch(topic, payload)
            except aiomqtt.MqttError as e:
                self.client = None
                log.warning("mqtt: %s; retrying in %.0f s", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)
