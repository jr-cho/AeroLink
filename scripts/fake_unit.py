#!/usr/bin/env python3
"""A fake AeroLink unit over MQTT (PLAN E6.3, for E7 and manual tests).

    cd backend && uv run python ../scripts/fake_unit.py --id dev-001 --password "$PW" \
        --route ev-sef,ev-orl [--host 127.0.0.1 --port 1883] [--tls --ca ../mosquitto/certs/ca.crt]

Behaves like the firmware: a retained ``online`` on ``status`` plus a Last Will ``offline``, 1 Hz ``telemetry``
along a route, a ``health`` heartbeat every 10 s. Options to provoke what the backend must notice:

    --no-fix          GNSS fix none: telemetry still flows (degraded, not offline)
    --weak-signal     Wi-Fi RSSI -88 dBm (degraded)
    --silent-after S  stop publishing after S seconds WITHOUT disconnecting (a pulled power lead: the Last Will
                      does not fire; only the backend's silence watchdog notices)
    --gap S AT        pause telemetry for S seconds starting AT seconds in (a new track segment on return)
    --toggle-after S  switch passenger <-> medical after S seconds (a demo unit's button)
    --duration S      stop after S seconds (default: run until Ctrl+C)

The route is ``--route node,node,...`` (corridor nodes of ``airspace/central-fl-v1.geojson``, joined by their
corridor geometry or, when no corridor links them, a straight line) or ``--track file.json`` (a list of
``{"lat", "lon", "alt_msl_m"?, "speed_mps"?}`` points replayed at 1 Hz). Credentials come from ``--password`` or the
``MQTT_PASSWORD`` environment variable; this script never reads ``.env``.

``FakeUnit`` builds the messages without a broker, so tests can use it directly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import ssl
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
DEFAULT_NETWORK = REPO / "airspace" / "central-fl-v1.geojson"
CRUISE_MPS = 40.0
R_EARTH_M = 6_371_000.0


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH_M * math.asin(math.sqrt(h))


def _bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2, dl = math.radians(a[0]), math.radians(b[0]), math.radians(b[1] - a[1])
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def corridor_path(network_file: Path, nodes: list[str]) -> list[tuple[float, float]]:
    """(lat, lon) points through ``nodes``, following a corridor's geometry where one links consecutive nodes."""
    feats = json.loads(network_file.read_text())["features"]
    point = {
        f["properties"]["id"]: (f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0])
        for f in feats
        if f["properties"].get("kind") == "node"
    }
    lines = {
        (f["properties"]["from"], f["properties"]["to"]): [(c[1], c[0]) for c in f["geometry"]["coordinates"]]
        for f in feats
        if f["properties"].get("kind") == "corridor"
    }
    unknown = [n for n in nodes if n not in point]
    if unknown:
        raise SystemExit(f"unknown node(s): {', '.join(unknown)}")
    out: list[tuple[float, float]] = [point[nodes[0]]]
    for a, b in zip(nodes, nodes[1:], strict=False):
        if (a, b) in lines:
            out += lines[(a, b)][1:]
        elif (b, a) in lines:
            out += lines[(b, a)][::-1][1:]
        else:
            out.append(point[b])
    return out


class FakeUnit:
    """Message builder + 1 Hz track. ``t`` is the unit's own time in seconds since it started."""

    def __init__(
        self,
        aircraft_id: str,
        points: list[tuple[float, float]],
        vehicle_type: str = "passenger",
        speed_mps: float = CRUISE_MPS,
        no_fix: bool = False,
        weak_signal: bool = False,
        firmware: str = "fake-0.1",
    ):
        self.id, self.vehicle_type, self.speed = aircraft_id, vehicle_type, speed_mps
        self.no_fix, self.weak_signal, self.firmware = no_fix, weak_signal, firmware
        self.points = points
        self.legs = [_dist(a, b) for a, b in zip(points, points[1:], strict=False)]
        self.total = sum(self.legs)

    def position(self, t: float) -> tuple[float, float, float]:
        """(lat, lon, course) after ``t`` seconds; it holds at the last point once the route is flown."""
        d = min(self.speed * t, self.total)
        for (a, b), leg in zip(zip(self.points, self.points[1:], strict=False), self.legs, strict=False):
            if d <= leg or leg == 0:
                f = 0.0 if leg == 0 else d / leg
                return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, _bearing(a, b)
            d -= leg
        last = self.points[-1]
        return last[0], last[1], _bearing(self.points[-2], last) if len(self.points) > 1 else 0.0

    def moving(self, t: float) -> bool:
        return self.speed * t < self.total

    def telemetry(self, t: float, now: datetime | None = None) -> bytes:
        lat, lon, course = self.position(t)
        fix = (
            {"type": "none", "hdop": 99.0, "satellites": 0}
            if self.no_fix
            else {"type": "3d", "hdop": 0.9, "satellites": 11}
        )
        return json.dumps(
            {
                "schema_version": "4.0",
                "message_id": str(uuid4()),
                "aircraft_id": self.id,
                "vehicle_type": self.vehicle_type,
                "timestamp": (now or datetime.now(UTC)).isoformat(),
                "position": {"lat": lat, "lon": lon, "alt_msl_m": 60.0},
                "motion": {
                    "ground_speed_mps": self.speed if self.moving(t) else 0.0,
                    "course_deg": course % 360.0,
                    "vertical_speed_mps": 0.0,
                },
                "fix": {**fix, "replayed": False},
                "active_flight_id": None,
            }
        ).encode()

    def health(self, t: float, now: datetime | None = None) -> bytes:
        return json.dumps(
            {
                "schema_version": "4.1",
                "message_id": str(uuid4()),
                "aircraft_id": self.id,
                "timestamp": (now or datetime.now(UTC)).isoformat(),
                "uptime_s": round(t, 1),
                "reset_reason": "power_on",
                "wifi_rssi_dbm": -88 if self.weak_signal else -58,
                "gnss": {
                    "fix": "none" if self.no_fix else "3d",
                    "satellites": 0 if self.no_fix else 11,
                    "hdop": 99.0 if self.no_fix else 0.9,
                },
                "free_heap_bytes": 180_000,
                "firmware_version": self.firmware,
            }
        ).encode()

    def toggle_type(self) -> str:
        self.vehicle_type = "medical" if self.vehicle_type == "passenger" else "passenger"
        return self.vehicle_type


def track_points(path: Path) -> tuple[list[tuple[float, float]], float | None]:
    rows = json.loads(path.read_text())
    speed = next((r["speed_mps"] for r in rows if "speed_mps" in r), None)
    return [(float(r["lat"]), float(r["lon"])) for r in rows], speed


async def run(a: argparse.Namespace) -> None:
    import aiomqtt

    if a.track:
        points, speed = track_points(Path(a.track))
    else:
        points, speed = corridor_path(Path(a.network), a.route.split(",")), None
    unit = FakeUnit(a.id, points, a.vehicle_type, speed or a.speed, a.no_fix, a.weak_signal)
    topic = f"aerolink/{a.namespace}/{a.id}"
    tls = ssl.create_default_context(cafile=a.ca) if a.tls else None
    port = a.port or (8883 if a.tls else 1883)
    will = aiomqtt.Will(f"{topic}/status", payload=b"offline", qos=1, retain=True)
    async with aiomqtt.Client(
        a.host,
        port,
        username=a.user or a.id,
        password=a.password or os.environ.get("MQTT_PASSWORD"),
        identifier=f"fake-{a.id}",
        will=will,
        tls_context=tls,
        keepalive=5,
    ) as client:
        await client.publish(f"{topic}/status", b"online", qos=1, retain=True)
        print(f"{a.id}: online on {a.host}:{port}, {len(points)} route points, {unit.total / 1000:.1f} km")
        t, last_health, silent = 0.0, -10.0, False
        while a.duration is None or t < a.duration:
            if a.silent_after is not None and t >= a.silent_after and not silent:
                silent = True
                print(f"{a.id}: going silent (still connected)")
            gap = a.gap and a.gap[1] <= t < a.gap[1] + a.gap[0]
            if a.toggle_after is not None and int(t) == int(a.toggle_after):
                print(f"{a.id}: type -> {unit.toggle_type()}")
                a.toggle_after = None
            if not silent:
                if not gap:
                    await client.publish(f"{topic}/telemetry", unit.telemetry(t), qos=1)
                if t - last_health >= 10.0:
                    await client.publish(f"{topic}/health", unit.health(t), qos=1)
                    last_health = t
            await asyncio.sleep(1.0)
            t += 1.0
        if not silent:
            await client.publish(f"{topic}/status", b"offline", qos=1, retain=True)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--id", required=True, help="aircraft id = MQTT username")
    p.add_argument("--user", help="MQTT username (default: the id)")
    p.add_argument("--password", help="default: $MQTT_PASSWORD")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, help="default 1883, or 8883 with --tls")
    p.add_argument("--tls", action="store_true")
    p.add_argument("--ca", help="CA certificate file for --tls")
    p.add_argument("--namespace", choices=["aircraft", "sim"], default="aircraft")
    p.add_argument("--vehicle-type", choices=["passenger", "medical"], default="passenger")
    p.add_argument("--network", default=str(DEFAULT_NETWORK), help="corridor network GeoJSON")
    p.add_argument("--route", default="ev-sef,ev-orl", help="comma-separated node ids")
    p.add_argument("--track", help="JSON list of {lat, lon[, speed_mps]} points instead of --route")
    p.add_argument("--speed", type=float, default=CRUISE_MPS)
    p.add_argument("--no-fix", action="store_true")
    p.add_argument("--weak-signal", action="store_true")
    p.add_argument("--silent-after", type=float)
    p.add_argument("--gap", nargs=2, type=float, metavar=("SECONDS", "AT"))
    p.add_argument("--toggle-after", type=float)
    p.add_argument("--duration", type=float)
    return p


if __name__ == "__main__":
    try:
        asyncio.run(run(parser().parse_args()))
    except KeyboardInterrupt:
        sys.exit(0)
