"""Read readsb trace files (one aircraft, one day) from adsb.lol globe_history.

Format: readsb README-json, "trace" section. Top level: icao, r (registration),
t (type designator), desc, dbFlags, timestamp, trace. Each trace point:

    [0] seconds after timestamp   [1] lat   [2] lon
    [3] altitude ft | "ground" | null      [4] ground speed kt | null
    [5] track deg | null          [6] flags (1 stale, 2 new leg, 4 geom vrate, 8 geom alt)
    [7] vertical rate fpm | null  [8] details object | null (flight, category, ...)
    [9] position source ("adsb_icao", "mlat", ...) | null   (files from 2022 on)
    [10] geometric altitude ...   (later fields unused)
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass

FLAG_NEW_LEG = 2


@dataclass(frozen=True)
class Point:
    t: float  # unix seconds
    lat: float
    lon: float
    alt_ft: float | None  # barometric; None when on the ground or unknown
    on_ground: bool
    gs_kt: float | None
    track_deg: float | None
    vrate_fpm: float | None
    new_leg: bool
    source: str | None


@dataclass(frozen=True)
class Trace:
    icao: str
    registration: str | None
    type_code: str | None
    db_flags: int
    category: str | None  # first category seen in the details objects
    callsign: str | None  # first callsign seen
    points: list[Point]

    @property
    def military(self) -> bool:
        return bool(self.db_flags & 1)


def decode(raw: bytes) -> dict:
    """Trace files end in .json but are usually gzip inside: always try to decompress."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_trace(doc: dict) -> Trace:
    base = float(doc.get("timestamp", 0.0))
    category = callsign = None
    points: list[Point] = []
    for p in doc.get("trace", []):
        if len(p) < 7 or p[1] is None or p[2] is None:
            continue
        details = p[8] if len(p) > 8 and isinstance(p[8], dict) else None
        if details:
            category = category or (details.get("category") or None)
            flight = (details.get("flight") or "").strip()
            callsign = callsign or (flight or None)
        alt = p[3]
        points.append(
            Point(
                t=base + float(p[0]),
                lat=float(p[1]),
                lon=float(p[2]),
                alt_ft=_num(alt),
                on_ground=alt == "ground",
                gs_kt=_num(p[4]),
                track_deg=_num(p[5]),
                vrate_fpm=_num(p[7]) if len(p) > 7 else None,
                new_leg=bool((p[6] or 0) & FLAG_NEW_LEG),
                source=p[9] if len(p) > 9 and isinstance(p[9], str) else None,
            )
        )
    return Trace(
        icao=str(doc.get("icao", "")).lower(),
        registration=doc.get("r"),
        type_code=doc.get("t"),
        db_flags=int(doc.get("dbFlags") or 0),
        category=category,
        callsign=callsign,
        points=points,
    )
