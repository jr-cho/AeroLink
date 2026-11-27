"""Event log and operator alerts (PLAN E6.3, E6.9).

Every notable thing a unit or a watchdog does is persisted as an event; an alert is an event nobody acknowledged
yet. New events are pushed to the operations channel (``/ws/ops``) through the hub. Recording never raises: losing
an event must not stop ingest.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from aerolink.service.db import events as events_table
from aerolink.service.hub import Hub
from aerolink.service.settings import Settings
from aerolink.service.store import Store

log = logging.getLogger("aerolink.events")
audit = logging.getLogger("aerolink.audit")

EVENT_KINDS = (
    "unit_online",
    "unit_offline",
    "unit_no_signal",
    "unit_degraded",
    "unit_healthy",
    "gnss_fix_lost",
    "gnss_fix_gained",
    "vehicle_type_changed",
    "callsign_mismatch",
    "off_route",
    "low_energy_emergency",
    "medical_dispatch",
    "watchdog",
    "alert_ack",
    "clearance",
    "reroute",
    "ai_ack",
)
SEVERITIES = ("info", "warn", "critical")


def public(row: dict[str, Any]) -> dict[str, Any]:
    """JSON-ready event (ISO times)."""
    out = dict(row)
    for k in ("at", "acked_at"):
        if out.get(k) is not None:
            t = out[k]
            out[k] = (t if t.tzinfo else t.replace(tzinfo=UTC)).isoformat()
    return out


class EventLog:
    def __init__(self, store: Store, clock: Callable[[], datetime], hub: Hub | None = None):
        self.store, self.clock, self.hub = store, clock, hub

    async def record(
        self,
        kind: str,
        *,
        aircraft_id: str | None = None,
        severity: str = "info",
        message: str = "",
        detail: dict[str, Any] | None = None,
        acked_by: str | None = None,
    ) -> dict[str, Any] | None:
        assert kind in EVENT_KINDS and severity in SEVERITIES, (kind, severity)
        at = self.clock()
        try:
            values: dict[str, Any] = dict(
                at=at,
                kind=kind,
                severity=severity,
                aircraft_id=aircraft_id,
                message=message[:256],
                detail=detail or {},
                acked_at=at if acked_by else None,
                acked_by=acked_by,
            )
            values["id"] = await self.store.add_event(**values)
        except Exception:  # noqa: BLE001 - the log is best effort
            log.exception("could not record %s event", kind)
            return None
        row = public(values)
        if self.hub is not None:
            self.hub.publish_ops("alert", row)
        return row

    async def ack(self, event_id: int, operator: str) -> dict[str, Any] | None:
        """Acknowledge an alert. None if unknown. A second acknowledgement changes nothing (the first stays)."""
        row = await self.store.get_event(event_id)
        if row is None:
            return None
        if await self.store.ack_event(event_id, operator, self.clock()):
            audit.info("alert %s (%s) acknowledged by %s", event_id, row["kind"], operator)
            row = await self.store.get_event(event_id) or row
            await self.record(
                "alert_ack",
                aircraft_id=row["aircraft_id"],
                message=f"{operator} acknowledged alert {event_id} ({row['kind']})",
                detail={"alert_id": event_id, "operator": operator},
                acked_by=operator,
            )
            if self.hub is not None:
                self.hub.publish_ops("ack", public(row))
        return public(row)


# --- admin CLI: clear test alerts before a demo ---------------------------------------------------------------
async def purge(store: Store, kind: str | None, before: datetime, dry_run: bool = False) -> int:
    """Delete events older than ``before`` (of one ``kind``, or every kind when None). Returns how many matched."""
    cond = events_table.c.at < before
    if kind:
        cond = sa.and_(cond, events_table.c.kind == kind)
    async with store.engine.begin() as c:
        if dry_run:
            return int((await c.execute(sa.select(sa.func.count()).select_from(events_table).where(cond))).scalar_one())
        return int((await c.execute(sa.delete(events_table).where(cond))).rowcount)


def _when(text: str) -> datetime:
    t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)  # no zone given: UTC


async def _main(a: argparse.Namespace) -> None:
    store = Store.connect(Settings.from_env().database_url)
    try:
        n = await purge(store, a.kind, a.before, a.dry_run)
        print(f"{'would delete' if a.dry_run else 'deleted'} {n} event(s)" + (f" of kind {a.kind}" if a.kind else ""))
    finally:
        await store.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Event log admin. Run it where DATABASE_URL points at the right database.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("purge", help="delete events older than a time (clears test alerts before a demo)")
    p.add_argument("--kind", choices=EVENT_KINDS, help="only this kind (default: every kind)")
    p.add_argument("--before", type=_when, required=True, help="ISO time; older events go (UTC if no zone)")
    p.add_argument("--dry-run", action="store_true", help="only count")
    asyncio.run(_main(ap.parse_args(argv)))


if __name__ == "__main__":
    main()
