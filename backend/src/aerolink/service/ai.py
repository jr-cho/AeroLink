"""AI outputs the screens show (UI-AI-PLAN, U-API contract): pure calculations, no I/O.

``route_segments`` turns a route's ETAs into per-leg predicted traffic (U-API §1): each leg carries the level the
forecast gives at the moment the aircraft reaches its far end, so the pilot's route line is coloured by the traffic it
will actually meet, not the traffic now.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from aerolink.service.live import distance_m


def route_segments(
    path: list[str],
    eta_by_node: Mapping[str, float],
    level_at: Callable[[str, str, float], str],
) -> list[dict[str, Any]]:
    """``[{from, to, eta_s, level}]`` for each leg of ``path`` whose far node has an ETA.

    ``eta_by_node`` holds epoch seconds (p50) of reaching each node; ``level_at(u, v, t)`` is the forecast level of
    the leg ``u -> v`` at epoch second ``t``. A leg without an ETA for its far end is skipped, never guessed.
    """
    out: list[dict[str, Any]] = []
    for u, v in zip(path, path[1:], strict=False):
        if v not in eta_by_node:
            continue
        t = float(eta_by_node[v])
        out.append({"from": u, "to": v, "eta_s": t, "level": level_at(u, v, t)})
    return out


# --- live ADS-B for the pilot (U-API §4) ---------------------------------------------------------------------
NM_M = 1852.0
ADSB_NEAR_M = 10 * NM_M  # aircraft within 10 NM of the unit
ADSB_MAX_SEEN_S = 60.0  # not heard from for longer than this: not shown (as the conflict maths does)
FT_PER_M = 1.0 / 0.3048


def nearby_adsb(aircraft: list[dict[str, Any]], lat: float, lon: float, unit_alt_m: float) -> list[dict[str, Any]]:
    """Live ADS-B aircraft within 10 NM of a unit, nearest first. Advisory only: never routed, never a gate.

    ``rel_alt_ft`` is the aircraft's altitude minus the unit's (null when the aircraft reports none).
    """
    unit_ft = unit_alt_m * FT_PER_M
    found: list[tuple[float, dict[str, Any]]] = []
    for a in aircraft:
        if a.get("lat") is None or a.get("lon") is None or float(a.get("seen_s") or 0.0) > ADSB_MAX_SEEN_S:
            continue
        d = distance_m(lat, lon, float(a["lat"]), float(a["lon"]))
        if d > ADSB_NEAR_M:
            continue
        alt = a.get("alt_ft")
        found.append(
            (
                d,
                {
                    "icao": a["icao"],
                    "callsign": a.get("callsign"),
                    "lat": a["lat"],
                    "lon": a["lon"],
                    "alt_ft": alt,
                    "gs_kt": a.get("gs_kt"),
                    "track_deg": a.get("track_deg"),
                    "rel_alt_ft": round(alt - unit_ft) if isinstance(alt, int | float) else None,
                },
            )
        )
    found.sort(key=lambda x: x[0])
    return [row for _, row in found]


LIVE_FLIGHT = ("planned", "active", "off_route")


def pilot_screen_flying(service: Any, hub: Any) -> bool:
    """A paired pilot screen is connected and its unit has a planned or active flight: someone is looking at traffic.

    The ADS-B poll is adaptive (30 s when viewed, 5 min idle): without this, a pilot screen alone would get the idle
    cadence and mostly empty traffic (regulatory finding C-03).
    """
    return any(hub.subscribers(aid) > 0 and row["status"] in LIVE_FLIGHT for aid, row in service.flights.items())


def feed_state(app_state: Any) -> str:
    """The live ADS-B feed's state (ok | stale | down | rate_limited | disabled) for the pilot screens."""
    feed = getattr(app_state, "adsb", None)
    return "disabled" if feed is None else feed.state()


def unit_adsb_view(app_state: Any, aircraft_id: str) -> dict[str, Any]:
    """What a unit's pilot screens get from the feed: the nearby list and the feed state (so a screen can say
    "Traffic data unavailable" instead of "None nearby")."""
    return {"adsb": unit_adsb(app_state, aircraft_id), "adsb_state": feed_state(app_state)}


def unit_adsb(app_state: Any, aircraft_id: str) -> list[dict[str, Any]]:
    """The ``adsb`` list of one unit's pilot screens: [] without a feed or a fix."""
    feed = getattr(app_state, "adsb", None)
    track = app_state.service.live.tracks.get(aircraft_id)
    if feed is None or track is None:
        return []
    pos = track.last.position
    return nearby_adsb(feed.snapshot()["aircraft"], pos.lat, pos.lon, pos.alt_msl_m)
