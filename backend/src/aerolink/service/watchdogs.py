"""Server watchdogs (PLAN E6.9): is each part of the system doing its job right now?

Each check yields ``ok | warn | fail`` with its value, threshold and the time its state last changed. Results are
re-evaluated every WATCHDOG_INTERVAL_SECONDS by a loop and cached, so ``/healthz`` stays cheap; ``/healthz?deep=1``
re-evaluates on demand. A state change is written to the event log and pushed to ``/ws/ops``.

Thresholds are chosen so a stopped sim, broker or ADS-B feed shows ``fail`` within 30 s (loop period 5 s).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from aerolink.service.core import Service
from aerolink.service.hub import Hub
from aerolink.service.mqtt import MqttLink
from aerolink.service.settings import Settings

log = logging.getLogger("aerolink.watchdog")
SIM_WARN_S, SIM_FAIL_S = 10.0, 20.0
ADSB_WARN_S, ADSB_FAIL_S = 10.0, 20.0
DB_SLOW_S = 2.0
RANK = {"ok": 0, "warn": 1, "fail": 2}


def _result(name: str, state: str, value: Any, threshold: str, detail: str) -> dict[str, Any]:
    return {"name": name, "state": state, "value": value, "threshold": threshold, "detail": detail}


def _by_age(age: float, warn: float, fail: float) -> str:
    return "fail" if age > fail else "warn" if age > warn else "ok"


class Watchdogs:
    def __init__(
        self,
        service: Service,
        settings: Settings,
        link: MqttLink | None = None,
        hub: Hub | None = None,
        adsb: Any = None,
        registry: Any = None,
    ):
        self.service, self.settings, self.link, self.hub = service, settings, link, hub
        self.adsb, self.registry = adsb, registry
        self.results: list[dict[str, Any]] = []
        self.changed: dict[str, datetime] = {}
        self.started = service.clock()
        self._adsb_ref: float | None = None

    # --- the checks ---------------------------------------------------------------------------------
    def check_mqtt(self) -> dict[str, Any]:
        link = self.link
        if link is None:
            return _result("mqtt", "ok", None, "connected", "MQTT disabled in this process")
        if not link.connected:
            return _result("mqtt", "fail", None, "connected", "not connected to the broker")
        age = None if link.last_message is None else round(link.mono() - link.last_message, 1)
        return _result("mqtt", "ok", age, "connected", "connected; seconds since the last message in value")

    def check_forecast(self, now: datetime) -> dict[str, Any]:
        every = self.settings.forecast_interval_s
        at = self.service.forecast_at
        age = round((now - at).total_seconds(), 1) if at else round((now - self.started).total_seconds(), 1)
        warn, fail = 3 * every, 6 * every
        return _result(
            "forecast", _by_age(age, warn, fail), age, f"warn > {warn:g} s, fail > {fail:g} s", "seconds since refresh"
        )

    def check_sim(self, now: datetime) -> dict[str, Any]:
        at = self.service.sim_heard_at
        if at is None:
            return _result("simulator", "ok", None, f"fail > {SIM_FAIL_S:g} s", "no simulator has connected")
        age = round((now - at).total_seconds(), 1)
        return _result(
            "simulator",
            _by_age(age, SIM_WARN_S, SIM_FAIL_S),
            age,
            f"fail > {SIM_FAIL_S:g} s",
            "seconds since a sim message",
        )

    def check_adsb(self) -> dict[str, Any]:
        feed = self.adsb
        if feed is None or not feed.settings.enabled:
            return _result("adsb", "ok", None, f"fail > {ADSB_FAIL_S:g} s", "live ADS-B disabled")
        if feed.last_ok is None and self._adsb_ref is None:
            self._adsb_ref = feed.clock()  # a feed that never worked is measured from when we first looked
        ref = feed.last_ok if feed.last_ok is not None else self._adsb_ref
        age = round(feed.clock() - ref, 1)
        interval = feed.expected_interval()  # polling is adaptive (30 s viewed, 5 min idle): one interval is normal
        warn, fail = ADSB_WARN_S + interval, ADSB_FAIL_S + interval
        state = "ok" if feed.failures == 0 and feed.last_ok is not None else _by_age(age, warn, fail)
        if state == "ok" and age > fail:
            state = "fail"  # polling stopped without reporting a failure
        detail = f"feed {feed.state()}" + (f": {feed.error}" if feed.error else "")
        return _result("adsb", state, age, f"fail > {fail:g} s without a good poll", detail)

    async def check_db(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            await self.service.store.write_probe()
        except Exception as e:  # noqa: BLE001
            return _result("database", "fail", None, "write probe", f"{type(e).__name__}: {e}"[:200])
        took = round(time.perf_counter() - t0, 3)
        return _result(
            "database", "warn" if took > DB_SLOW_S else "ok", took, f"warn > {DB_SLOW_S:g} s", "write probe seconds"
        )

    def check_models(self) -> dict[str, Any]:
        reg = self.registry
        if reg is None:
            return _result("models", "ok", None, "loaded", "no registry")
        want = self.settings.forecaster
        if not self.settings.preload_models:
            ok = reg.loaded(want)
            return _result("models", "ok" if ok else "fail", None, "default forecaster loaded", "preload off")
        missing = [n for n in ("cv", "chronos", "aerolink-gbm") if not reg.loaded(n)]
        missing += [n for n, m in (("demand", reg.demand), ("offroute", reg.offroute)) if m is None]
        if not reg.loaded(want):
            return _result("models", "fail", missing, "default forecaster loaded", f"{want} is not loaded")
        return _result(
            "models",
            "warn" if missing else "ok",
            missing,
            "all models loaded",
            f"not loaded: {', '.join(missing)}" if missing else "all loaded",
        )

    # --- run ------------------------------------------------------------------------------------------
    async def evaluate(self) -> list[dict[str, Any]]:
        now = self.service.clock()
        out = [
            self.check_mqtt(),
            self.check_forecast(now),
            self.check_sim(now),
            self.check_adsb(),
            await self.check_db(),
            self.check_models(),
        ]
        before = {r["name"]: r["state"] for r in self.results}
        for r in out:
            old = before.get(r["name"])
            if old is None or old != r["state"]:
                self.changed[r["name"]] = now
                if old is not None or r["state"] != "ok":
                    await self._announce(r, old)
            r["since"] = self.changed[r["name"]].isoformat()
            r["checked_at"] = now.isoformat()
        self.results = out
        return out

    async def _announce(self, r: dict[str, Any], old: str | None) -> None:
        sev = {"ok": "info", "warn": "warn", "fail": "critical"}[r["state"]]
        log.warning("watchdog %s: %s -> %s (%s)", r["name"], old, r["state"], r["detail"])
        await self.service.events.record(
            "watchdog",
            severity=sev,
            message=f"watchdog {r['name']} {old or 'start'} -> {r['state']}: {r['detail']}",
            detail={"name": r["name"], "from": old, "to": r["state"], "value": r["value"]},
        )
        if self.hub is not None:
            self.hub.publish_ops("watchdog", r)

    def overall(self) -> str:
        """The worst state; ``starting`` until the first evaluation."""
        if not self.results:
            return "starting"
        return max((r["state"] for r in self.results), key=RANK.__getitem__)
