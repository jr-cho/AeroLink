"""F3: ``python -m aerolink.service.events purge`` clears test alerts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aerolink.service.events import _when, purge
from aerolink.service.store import Store

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def test_purge_deletes_only_matching_old_events(tmp_path: Path) -> None:
    async def go() -> None:
        store = Store.connect(f"sqlite+aiosqlite:///{tmp_path / 'e.db'}")
        await store.create_all()
        for kind, age_h in [("unit_no_signal", 5), ("unit_no_signal", 1), ("unit_offline", 5), ("watchdog", 5)]:
            await store.add_event(at=NOW - timedelta(hours=age_h), kind=kind, severity="warn", message=kind, detail={})
        cutoff = NOW - timedelta(hours=2)
        assert await purge(store, "unit_no_signal", cutoff, dry_run=True) == 1
        assert len(await store.events()) == 4  # a dry run changes nothing
        assert await purge(store, "unit_no_signal", cutoff) == 1
        left = sorted((e["kind"], e["at"].hour) for e in await store.events())
        assert left == [("unit_no_signal", 11), ("unit_offline", 7), ("watchdog", 7)]
        assert await purge(store, None, cutoff) == 2  # every kind
        assert [e["kind"] for e in await store.events()] == ["unit_no_signal"]
        await store.close()

    asyncio.run(go())


def test_before_accepts_iso_with_or_without_zone() -> None:
    assert _when("2026-11-26T12:00:00Z") == _when("2026-11-26T12:00:00") == NOW
