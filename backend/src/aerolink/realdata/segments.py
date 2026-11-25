"""Filter a trace and cut it into clean 1 Hz segments (ARCHITECTURE §10.2 steps 3-6).

Order is cheapest first: region, aircraft-level rules, then per point, then segments.
Every rejection is counted so the report shows which rule removed what.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import numpy as np

from aerolink.realdata.config import FilterConfig
from aerolink.realdata.parse import Point, Trace

COLUMNS = ["t", "lat", "lon", "alt_ft", "gs_kt", "track_deg", "vrate_fpm"]


@dataclass(frozen=True)
class Segment:
    icao: str
    index: int  # segment number within this aircraft's day
    registration: str | None
    type_code: str | None
    category: str | None
    callsign: str | None
    data: dict[str, np.ndarray]  # COLUMNS, resampled to 1 Hz

    @property
    def segment_id(self) -> str:
        return f"{self.icao}-{int(self.data['t'][0])}-{self.index}"

    @property
    def duration_s(self) -> float:
        return float(self.data["t"][-1] - self.data["t"][0])


def aircraft_rejection(trace: Trace, cfg: FilterConfig) -> str | None:
    """Reason to drop the whole aircraft, or None to keep it."""
    if not any(cfg.in_region(p.lat, p.lon) for p in trace.points):
        return "outside_region"
    if cfg.drop_military and trace.military:
        return "military"
    if trace.callsign and cfg.airline_callsign.match(trace.callsign.upper()):
        return "airline_callsign"
    if trace.category is None:
        if not cfg.keep_unknown_category:
            return "category_unknown"
    elif trace.category not in cfg.keep_categories:
        return "category_not_light_or_rotor"
    return None


def point_rejection(p: Point, cfg: FilterConfig) -> str | None:
    if not cfg.in_region(p.lat, p.lon):
        return "point_outside_region"
    if p.source not in cfg.keep_sources:
        return "point_source"  # mlat, tisb, unknown
    if p.on_ground or p.alt_ft is None or not (cfg.altitude_ft[0] <= p.alt_ft <= cfg.altitude_ft[1]):
        return "point_altitude"
    if p.gs_kt is None or not (cfg.ground_speed_kt[0] <= p.gs_kt <= cfg.ground_speed_kt[1]):
        return "point_speed"
    return None


def _runs(points: list[Point], cfg: FilterConfig) -> list[list[Point]]:
    """Consecutive kept points, split at gaps and new-leg flags."""
    runs: list[list[Point]] = []
    current: list[Point] = []
    for p in points:
        if current and (p.t - current[-1].t > cfg.max_gap_s or p.new_leg):
            runs.append(current)
            current = []
        current.append(p)
    if current:
        runs.append(current)
    return runs


def resample(run: list[Point], step_s: float) -> dict[str, np.ndarray]:
    """Linear interpolation to a fixed step; track angles are unwrapped first."""
    t = np.array([p.t for p in run])
    keep = np.concatenate([[True], np.diff(t) > 0])  # drop duplicate timestamps
    t = t[keep]
    grid = np.arange(np.ceil(t[0]), np.floor(t[-1]) + 1e-9, step_s)
    out = {"t": grid}

    def interp(values: list[float | None], angle: bool = False) -> np.ndarray:
        v = np.array([np.nan if x is None else x for x in values], dtype=float)[keep]
        ok = ~np.isnan(v)
        if ok.sum() < 2:
            return np.full(grid.shape, np.nan)
        if angle:
            v[ok] = np.degrees(np.unwrap(np.radians(v[ok])))
        r = np.interp(grid, t[ok], v[ok])
        return np.mod(r, 360.0) if angle else r

    out["lat"] = interp([p.lat for p in run])
    out["lon"] = interp([p.lon for p in run])
    out["alt_ft"] = interp([p.alt_ft for p in run])
    out["gs_kt"] = interp([p.gs_kt for p in run])
    out["track_deg"] = interp([p.track_deg for p in run], angle=True)
    out["vrate_fpm"] = interp([p.vrate_fpm for p in run])
    return out


def extract_segments(trace: Trace, cfg: FilterConfig, counts: Counter) -> list[Segment]:
    """All usable segments of one aircraft's day; updates `counts` with what was dropped."""
    counts["aircraft_seen"] += 1
    reason = aircraft_rejection(trace, cfg)
    if reason:
        counts[f"aircraft_dropped_{reason}"] += 1
        return []
    counts["aircraft_candidate"] += 1
    kept: list[Point] = []
    for p in trace.points:
        if not cfg.in_region(p.lat, p.lon):
            continue  # most of a long flight is elsewhere; not worth counting
        counts["points_in_region"] += 1
        why = point_rejection(p, cfg)
        if why:
            counts[f"points_dropped_{why}"] += 1
        else:
            kept.append(p)
    counts["points_kept"] += len(kept)
    segments: list[Segment] = []
    for run in _runs(kept, cfg):
        if run[-1].t - run[0].t < cfg.min_segment_s:
            counts["segments_dropped_too_short"] += 1
            continue
        segments.append(
            Segment(
                icao=trace.icao,
                index=len(segments),
                registration=trace.registration,
                type_code=trace.type_code,
                category=trace.category,
                callsign=trace.callsign,
                data=resample(run, cfg.resample_s),
            )
        )
    if segments:
        counts["aircraft_kept"] += 1
        counts["segments_kept"] += len(segments)
        counts["segment_seconds"] += int(sum(s.duration_s for s in segments))
    return segments
