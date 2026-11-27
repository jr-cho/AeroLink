"""In-memory live ADS-B set, feed health, and the forecast hook. Never touches the device registry."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from aerolink.adsb_live.client import ATTRIBUTION, AdsbSettings, RateLimited, fetch_point, parse_response
from aerolink.network import Network
from aerolink.offroute.detector import reachable_mass
from aerolink.realdata.config import FilterConfig

log = logging.getLogger("aerolink.adsb")
KT_TO_MPS = 0.514444
WINDOW_S = 86_400.0  # the provider's rate-limit window (500 requests per 24 h)


@dataclass
class LiveAircraft:
    data: dict[str, Any]
    last_seen: float  # monotonic clock of the poll that reported it


Fetch = Callable[[], Awaitable[dict]]


class AdsbFeed:
    """Poll loop + state. ``fetch`` and ``clock`` are injectable so tests need no network or sleeping."""

    def __init__(
        self,
        settings: AdsbSettings | None = None,
        fetch: Fetch | None = None,
        clock: Callable[[], float] = time.monotonic,
        filters: FilterConfig | None = None,
    ):
        self.settings = settings or AdsbSettings()
        self._fetch = fetch
        self.clock = clock
        self.filters = filters or FilterConfig()
        self.aircraft: dict[str, LiveAircraft] = {}
        self.last_ok: float | None = None
        self.last_ok_at: datetime | None = None
        self.error: str | None = None
        self.kept = self.dropped = 0
        self.failures = 0
        self._state = "disabled" if not self.settings.enabled else "down"
        self.requests: deque[float] = deque()  # clock time of every request (failed ones count), rolling 24 h
        self.last_attempt: float | None = None
        self.last_view: float | None = None
        self.viewers: Callable[[], bool] | None = None  # extra "someone is looking" source (open /ws/ops sockets)
        self.rate_limited_until: float | None = None
        self.header_remaining: tuple[float, int] | None = None  # (clock, x-ratelimit-remaining) from the last reply
        self._wake = asyncio.Event()

    # --- activity and budget ---------------------------------------------------------------

    def touch(self) -> None:
        """A viewer used ``/api/adsb``, ``/api/ops/*`` or ``/ws/ops``: poll at the active rate for a while."""
        was = self.viewed()
        self.last_view = self.clock()
        if not was:
            self._wake.set()  # a sleeping idle wait should re-plan now

    def viewed(self) -> bool:
        if self.viewers is not None and self.viewers():
            return True
        return self.last_view is not None and self.clock() - self.last_view <= self.settings.viewer_window_s

    def _prune(self, now: float) -> None:
        while self.requests and now - self.requests[0] >= WINDOW_S:
            self.requests.popleft()

    def budget_remaining(self) -> int:
        """Requests still allowed in the rolling 24 h (the lower of our count and the server's header)."""
        now = self.clock()
        self._prune(now)
        left = self.settings.daily_budget - len(self.requests)
        if self.header_remaining is not None and now - self.header_remaining[0] < WINDOW_S:
            left = min(left, self.header_remaining[1])
        return max(0, left)

    # --- poll ---------------------------------------------------------------------------

    async def poll_once(self) -> bool:
        """One poll; returns success. Updates the set and logs only on state changes. Never exceeds the budget."""
        now = self.clock()
        if self.budget_remaining() <= 0:
            self.error = "daily request budget used up"
            self._log_state()
            return False
        self.requests.append(now)
        self.last_attempt = now
        try:
            if self._fetch is None:  # only `run()` installs the default fetcher; a bare poll_once needs an injected one
                raise RuntimeError("no fetch function configured")
            doc = await self._fetch()
            rows, self.kept, self.dropped = parse_response(doc, self.filters)
        except RateLimited as exc:
            self.failures += 1
            self.error = str(exc)
            self.rate_limited_until = now + max(exc.retry_after_s or 0.0, self._backoff())
            self._log_state()
            return False
        except Exception as exc:  # noqa: BLE001 - any failure means "feed unavailable", never a crash
            self.failures += 1
            self.error = f"{type(exc).__name__}: {exc}"[:200]
            self._log_state()
            return False
        self.failures, self.error, self.rate_limited_until = 0, None, None
        remaining = doc.get("_ratelimit_remaining")
        if isinstance(remaining, int):
            self.header_remaining = (now, remaining)
        self.last_ok, self.last_ok_at = now, datetime.now(UTC)
        for row in rows:
            self.aircraft[row["icao"]] = LiveAircraft(row, now)
        self._expire(now)
        self._log_state()
        return True

    def _backoff(self) -> float:
        s = self.settings
        return min(s.max_backoff_s, s.poll_active_s * (2**self.failures)) if self.failures else 0.0

    def expected_interval(self) -> float:
        """Seconds between good polls when all is well: active/idle base, stretched as the daily budget runs low."""
        s = self.settings
        delay = s.poll_active_s if self.viewed() else s.poll_idle_s
        frac = self.budget_remaining() / s.daily_budget
        if frac < 0.5:
            delay = min(WINDOW_S, delay / (2 * max(frac, 1e-9)))
        return delay

    def next_delay(self) -> float:
        """Seconds from the last attempt to the next one.

        ``expected_interval``; doubled per consecutive failure up to the cap; held until a 429's ``Retry-After``;
        and when no budget is left, until the oldest request leaves the 24 h window.
        """
        now = self.clock()
        delay = self.expected_interval()
        if self.budget_remaining() <= 0:
            oldest = self.requests[0] if self.requests else now
            return max(delay, oldest + WINDOW_S - (self.last_attempt if self.last_attempt is not None else now) + 1.0)
        delay = max(delay, self._backoff())
        if self.rate_limited_until is not None and self.last_attempt is not None:
            delay = max(delay, self.rate_limited_until - self.last_attempt)
        return delay

    def due_in(self) -> float:
        """Seconds until the next poll is due (0 = now)."""
        if self.last_attempt is None:
            return 0.0
        return max(0.0, self.last_attempt + self.next_delay() - self.clock())

    async def run(self) -> None:
        if not self.settings.enabled:
            return
        async with httpx.AsyncClient() as client:
            if self._fetch is None:
                self._fetch = lambda: fetch_point(client, self.settings.url, self.settings.timeout_s, self.filters)
            while True:  # one request in flight: the next starts only after this one ends
                wait = self.due_in()
                if wait > 0:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), wait)
                        continue  # a viewer arrived: re-plan with the active interval
                    except TimeoutError:
                        pass
                await self.poll_once()

    # --- state --------------------------------------------------------------------------

    def _expire(self, now: float) -> None:
        for k in [k for k, a in self.aircraft.items() if now - a.last_seen > self.settings.ttl_s]:
            del self.aircraft[k]

    def state(self) -> str:
        if not self.settings.enabled:
            return "disabled"
        if self.rate_limited_until is not None and self.clock() < self.rate_limited_until:
            return "rate_limited"
        if self.failures == 0 and self.last_ok is not None:
            return "ok"
        grace = max(self.settings.stale_after_s, 2 * self.next_delay())
        if self.last_ok is not None and self.clock() - self.last_ok <= grace:
            return "stale"
        return "down"

    def _log_state(self) -> None:
        s = self.state()
        if s != self._state:
            log.warning("ADS-B feed %s -> %s%s", self._state, s, f" ({self.error})" if self.error else "")
            self._state = s

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        self._expire(now)
        rows = [
            {**a.data, "seen_s": round(a.data["seen_s"] + (now - a.last_seen), 1), "source": "adsb"}
            for a in self.aircraft.values()
        ]
        return {
            "attribution": ATTRIBUTION,
            "feed": {
                "state": self.state(),
                "last_ok_at": self.last_ok_at.isoformat() if self.last_ok_at else None,
                "error": self.error,
                "kept": self.kept,
                "dropped": self.dropped,
                "poll_interval_s": round(self.next_delay(), 1),
                "budget_remaining": self.budget_remaining(),
                "age_s": round(now - self.last_ok, 1) if self.last_ok is not None else None,
            },
            "aircraft": rows,
        }

    def background_mass(self, network: Network, t_s: float) -> list[tuple[float, str, float, float, float]]:
        """Forecast entries for the live aircraft; same representation as off-route mass."""
        return background_mass(self.snapshot()["aircraft"], network, t_s)


def background_mass(aircraft: list[dict], network: Network, t_s: float) -> list[tuple[float, str, float, float, float]]:
    """Spread each live ADS-B aircraft over reachable nodes (``reachable_mass``), like off-route traffic."""
    out: list[tuple[float, str, float, float, float]] = []
    for a in aircraft:
        out += reachable_mass(network, a["lat"], a["lon"], a["gs_kt"] * KT_TO_MPS, t_s)
    return out
