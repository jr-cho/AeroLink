"""adsb.lol live API client: one point/radius query, parsed and filtered with the E5.1 rules.

Endpoint (public, no key): ``GET {ADSB_URL}/v2/point/{lat}/{lon}/{radius_nm}`` (radius <= 250 nm).
Response: ``{"ac": [readsb aircraft json ...], "now": ms, ...}``. ``type`` is the position source
(``adsb_icao``, ``mlat``, ``tisb_*``), ``alt_baro`` is ft or ``"ground"``, ``gs`` kt, ``baro_rate`` fpm.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any

import httpx

from aerolink.realdata.config import FOCUS_LAT, FOCUS_LON, FilterConfig

ATTRIBUTION = "Aircraft data from adsb.lol, licensed under the Open Database License (ODbL) 1.0. https://adsb.lol"
USER_AGENT = "AeroLink/0.1 (adsb-live)"  # generic on purpose: no personal identity in requests
DEFAULT_URL = "https://api.adsb.lol"
NM_PER_DEG = 60.0


@dataclass(frozen=True)
class AdsbSettings:
    enabled: bool = True
    url: str = DEFAULT_URL
    poll_active_s: float = 30.0  # while someone is looking (/api/adsb, /api/ops/*, /ws/ops in the last viewer_window_s)
    poll_idle_s: float = 300.0  # otherwise
    daily_budget: int = 450  # requests per rolling 24 h; adsb.lol allows 500
    viewer_window_s: float = 120.0
    timeout_s: float = 10.0
    max_backoff_s: float = 600.0
    ttl_s: float = 90.0  # an aircraft with no report for this long leaves the set
    stale_after_s: float = 60.0  # failing for longer than this (or two intervals): feed is "down" (before: "stale")

    @classmethod
    def from_env(cls) -> AdsbSettings:
        e, d = os.environ, cls()
        return cls(
            enabled=e.get("ADSB_ENABLED", "true").lower() not in ("0", "false", "no"),
            url=(e.get("ADSB_URL") or d.url).rstrip("/"),
            poll_active_s=max(5.0, float(e.get("ADSB_POLL_ACTIVE_SECONDS") or d.poll_active_s)),
            poll_idle_s=max(5.0, float(e.get("ADSB_POLL_IDLE_SECONDS") or d.poll_idle_s)),
            daily_budget=max(1, int(e.get("ADSB_DAILY_BUDGET") or d.daily_budget)),
        )


class RateLimited(Exception):
    """HTTP 429 from the feed; ``retry_after_s`` is the server's ``Retry-After`` (None when absent)."""

    def __init__(self, retry_after_s: float | None):
        super().__init__(f"rate limited (retry after {retry_after_s}s)" if retry_after_s else "rate limited")
        self.retry_after_s = retry_after_s


def query_circle(cfg: FilterConfig | None = None) -> tuple[float, float, float]:
    """Centre and radius (nm) covering the focus box (the region filter trims the corners)."""
    cfg = cfg or FilterConfig()
    lat0, lat1 = cfg.lat if cfg else FOCUS_LAT
    lon0, lon1 = cfg.lon if cfg else FOCUS_LON
    clat, clon = (lat0 + lat1) / 2, (lon0 + lon1) / 2
    dy = (lat1 - lat0) / 2 * NM_PER_DEG
    dx = (lon1 - lon0) / 2 * NM_PER_DEG * math.cos(math.radians(clat))
    return clat, clon, min(250.0, math.ceil(math.hypot(dx, dy)) + 1)


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_response(doc: dict, cfg: FilterConfig | None = None) -> tuple[list[dict], int, int]:
    """(kept aircraft dicts, kept count, dropped count) after the E5.1 filters."""
    cfg = cfg or FilterConfig()
    kept: list[dict] = []
    dropped = 0
    for ac in doc.get("ac") or []:
        row = _accept(ac, cfg)
        if row is None:
            dropped += 1
        else:
            kept.append(row)
    return kept, len(kept), dropped


def _accept(ac: dict, cfg: FilterConfig) -> dict | None:
    lat, lon, gs, alt = _num(ac.get("lat")), _num(ac.get("lon")), _num(ac.get("gs")), ac.get("alt_baro")
    hexid = ac.get("hex")
    if lat is None or lon is None or gs is None or not hexid or hexid.startswith("~"):
        return None  # no position/speed, or a non-ICAO (TIS-B) address
    if alt == "ground" or not isinstance(alt, (int, float)) or isinstance(alt, bool):
        return None
    if not cfg.in_region(lat, lon) or not (cfg.altitude_ft[0] <= alt <= cfg.altitude_ft[1]):
        return None
    if not (cfg.ground_speed_kt[0] <= gs <= cfg.ground_speed_kt[1]):
        return None
    if ac.get("type") not in cfg.keep_sources:  # drops mlat, tisb, mode-s only
        return None
    if cfg.drop_military and int(ac.get("dbFlags") or 0) & 1:
        return None
    category = ac.get("category") or None
    if category is None and not cfg.keep_unknown_category:
        return None
    if category is not None and category not in cfg.keep_categories:
        return None
    callsign = (ac.get("flight") or "").strip() or None
    if callsign and cfg.airline_callsign.match(callsign):
        return None
    return {
        "icao": hexid.lower(),
        "callsign": callsign,
        "category": category,
        "lat": lat,
        "lon": lon,
        "alt_ft": float(alt),
        "gs_kt": gs,
        "track_deg": _num(ac.get("track")),
        "vert_rate_fpm": _num(ac.get("baro_rate")),
        "seen_s": _num(ac.get("seen_pos")) or _num(ac.get("seen")) or 0.0,
    }


async def fetch_point(
    client: httpx.AsyncClient, base_url: str, timeout_s: float, cfg: FilterConfig | None = None
) -> dict:
    """One request for the focus region; raises on any HTTP/network/JSON error."""
    lat, lon, radius = query_circle(cfg)
    r = await client.get(
        f"{base_url}/v2/point/{lat:.3f}/{lon:.3f}/{radius:.0f}",
        headers={"User-Agent": USER_AGENT},
        timeout=timeout_s,
    )
    if r.status_code == 429:
        raise RateLimited(_retry_after(r.headers.get("retry-after")))
    r.raise_for_status()
    doc = r.json()
    remaining = r.headers.get("x-ratelimit-remaining")
    if remaining is not None and str(remaining).strip().isdigit():
        doc["_ratelimit_remaining"] = int(remaining)  # the feed reads (and ignores otherwise) this key
    return doc


def _retry_after(value: str | None) -> float | None:
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:  # an HTTP date: not worth parsing, the backoff covers it
        return None
