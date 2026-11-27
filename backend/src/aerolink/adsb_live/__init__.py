"""Live ADS-B traffic from the adsb.lol API (ODbL). Background traffic only: never routed.

``GET /api/adsb`` response shape::

    {"attribution": "...ODbL...",
     "feed": {"state": "ok"|"stale"|"down"|"rate_limited"|"disabled", "last_ok_at": iso|null,
              "error": str|null, "kept": int, "dropped": int, "poll_interval_s": float (seconds between polls now:
              30 s while viewed, 5 min idle, longer when the daily budget runs low or after errors),
              "budget_remaining": int (requests left in the rolling 24 h),
              "age_s": float|null (since the last good poll)},
     "aircraft": [{"icao", "callsign", "category", "lat", "lon", "alt_ft", "gs_kt",
                   "track_deg", "vert_rate_fpm", "seen_s", "source": "adsb"}]}
"""

from aerolink.adsb_live.client import ATTRIBUTION, AdsbSettings, RateLimited, fetch_point, parse_response
from aerolink.adsb_live.state import AdsbFeed, LiveAircraft, background_mass

__all__ = [
    "ATTRIBUTION",
    "AdsbFeed",
    "AdsbSettings",
    "LiveAircraft",
    "RateLimited",
    "background_mass",
    "fetch_point",
    "parse_response",
]
