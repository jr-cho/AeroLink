"""Filter settings for the real-data pipeline (ARCHITECTURE §10.2).

One place for every rule, so the counts in the report can be traced back to a setting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Central Florida focus region (ARCHITECTURE §3.1), same box as aerolink.airspace.clip.
FOCUS_LAT = (27.3, 28.8)
FOCUS_LON = (-82.8, -81.1)


@dataclass(frozen=True)
class FilterConfig:
    lat: tuple[float, float] = FOCUS_LAT
    lon: tuple[float, float] = FOCUS_LON
    # Barometric altitude, ft. Top = top of FDOT's eVTOL band; bottom drops taxiing/hover-taxi.
    altitude_ft: tuple[float, float] = (100.0, 3000.0)
    ground_speed_kt: tuple[float, float] = (40.0, 160.0)
    # Emitter categories kept: A1 light (< 15,500 lb), A7 rotorcraft. Unknown category is kept
    # unless another rule drops it (many traces never send a category).
    keep_categories: frozenset[str] = frozenset({"A1", "A7"})
    keep_unknown_category: bool = True
    # Position sources kept (trace point index 9). MLAT, TIS-B and unknown sources are dropped.
    keep_sources: frozenset[str] = frozenset({"adsb_icao", "adsb_icao_nt", "adsr_icao", "adsb_other", "adsr_other"})
    drop_military: bool = True  # dbFlags & 1
    # Airline-style callsign: three letters then a digit (AAL123, SWA45, JBU9).
    airline_callsign: re.Pattern[str] = field(default_factory=lambda: re.compile(r"^[A-Z]{3}[0-9]"))
    max_gap_s: float = 30.0  # split a track at larger gaps
    min_segment_s: float = 60.0  # drop shorter segments: too short to forecast from
    resample_s: float = 1.0  # 1 Hz output

    def in_region(self, lat: float, lon: float) -> bool:
        return self.lat[0] <= lat <= self.lat[1] and self.lon[0] <= lon <= self.lon[1]
