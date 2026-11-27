"""Unit health (contract v4.1 ``health`` message) and the healthy / degraded / offline status (PLAN E6.9).

A unit publishes ``health`` every 10 s. The monitor keeps the latest one plus a short history per unit and derives:

* ``offline``   the unit is not connected (Last Will, or the silence watchdog in ``mqtt.py``);
* ``degraded``  connected but impaired: no GNSS fix for more than 10 s, Wi-Fi weaker than -80 dBm, or the health
                heartbeat is older than 30 s while telemetry still flows (a unit that never sent one is not judged,
                so v4.0 firmware stays healthy);
* ``healthy``   everything else.

Times are the service clock (injectable), so tests need no sleeping.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from aerolink.contracts import Health

HISTORY = 60  # ten minutes of heartbeats
TELEMETRY_FLOWING_S = 5.0  # telemetry newer than this counts as "still flowing"
UnitStatus = Literal["healthy", "degraded", "offline"]


@dataclass
class UnitState:
    aircraft_id: str
    status: UnitStatus = "healthy"
    reasons: list[str] = field(default_factory=list)
    health: dict[str, Any] | None = None  # the latest health message, as sent
    health_at: datetime | None = None  # when it arrived (service clock)
    history: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=HISTORY))
    telemetry_at: datetime | None = None  # latest telemetry of any quality
    fix: str | None = None  # fix type of that telemetry
    no_fix_since: datetime | None = None
    heard_at: datetime | None = None  # any message at all


class UnitMonitor:
    def __init__(self, health_stale_s: float = 30.0, no_fix_s: float = 10.0, rssi_dbm: float = -80.0):
        self.health_stale_s, self.no_fix_s, self.rssi_dbm = health_stale_s, no_fix_s, rssi_dbm
        self.units: dict[str, UnitState] = {}

    def _unit(self, aircraft_id: str) -> UnitState:
        return self.units.setdefault(aircraft_id, UnitState(aircraft_id))

    def note_health(self, msg: Health, now: datetime) -> UnitState:
        u = self._unit(msg.aircraft_id)
        u.health, u.health_at, u.heard_at = msg.model_dump(mode="json"), now, now
        u.history.append({"at": now.isoformat(), **u.health})
        return u

    def note_telemetry(self, aircraft_id: str, fix: str, now: datetime) -> tuple[UnitState, str | None]:
        """Record a telemetry sample. Returns the unit and ``"lost"`` / ``"gained"`` when the fix changed."""
        u = self._unit(aircraft_id)
        before = u.fix
        u.telemetry_at = u.heard_at = now
        u.fix = fix
        if fix == "none":
            u.no_fix_since = u.no_fix_since or now
        else:
            u.no_fix_since = None
        change = None
        if before is not None and (before == "none") != (fix == "none"):
            change = "lost" if fix == "none" else "gained"
        return u, change

    def evaluate(self, aircraft_id: str, online: bool, now: datetime) -> tuple[UnitStatus, list[str]]:
        u = self.units.get(aircraft_id)
        if not online:
            return "offline", []
        if u is None:
            return "healthy", []
        reasons: list[str] = []
        if u.no_fix_since is not None and (now - u.no_fix_since).total_seconds() > self.no_fix_s:
            reasons.append("no_gnss_fix")
        if u.health is not None and u.health["wifi_rssi_dbm"] < self.rssi_dbm:
            reasons.append("weak_signal")
        if (
            u.health_at is not None
            and (now - u.health_at).total_seconds() > self.health_stale_s
            and u.telemetry_at is not None
            and (now - u.telemetry_at).total_seconds() <= TELEMETRY_FLOWING_S
        ):
            reasons.append("health_stale")
        return ("degraded" if reasons else "healthy"), reasons

    def view(self, aircraft_id: str, online: bool, now: datetime) -> dict[str, Any]:
        """Additive fields for ``/api/devices``, ``/api/devices/{id}`` and the WebSocket ``health`` event."""
        status, reasons = self.evaluate(aircraft_id, online, now)
        u = self.units.get(aircraft_id)
        heard = u.heard_at if u else None
        age = round(max(0.0, (now - heard).total_seconds()), 1) if heard else None
        h_age = round(max(0.0, (now - u.health_at).total_seconds()), 1) if u and u.health_at else None
        return {
            "status": status,
            "status_reasons": reasons,
            "health": u.health if u else None,
            "health_age_s": h_age,
            "age_s": age,  # since the unit's last message of any kind
            "stale": age is None or age > TELEMETRY_FLOWING_S * 2,
        }
