"""Turn the cleaned adsb.lol segments into forecasting samples (PLAN E5.2, E5.3).

A *sample* is one moment in one real flight: the previous 60 s of positions (what a
forecaster sees) and the true position 30, 60, 120 and 300 s later (what it should
predict). Samples are taken every ``stride_s`` seconds along each segment.

Days, not samples, are split into training and held-out sets: two samples from the same
flight are almost identical, so a random split would leak the answer into training.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from aerolink.forecast.features import CATEGORY_TO_VEHICLE, HISTORY_S, POSITION_HORIZONS_S

FT_TO_M = 0.3048
DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "adsblol"
FLORIDA_UTC_OFFSET = timezone(timedelta(hours=-5))  # EST; good enough for an hour-of-day feature


@dataclass
class PositionSamples:
    """Arrays with one row per sample (N) and one column per horizon (H)."""

    day: np.ndarray  # (N,) "YYYY-MM-DD"
    segment_id: np.ndarray  # (N,)
    t: np.ndarray  # (N,) unix seconds of the last history fix
    vehicle: np.ndarray  # (N,) "rotor" / "light" / "" (unknown)
    hour: np.ndarray  # (N,) local hour of day
    history: np.ndarray  # (N, HISTORY_S, 3) lat, lon, alt_m; oldest first
    future: np.ndarray  # (N, H, 3) true lat, lon, alt_m at each horizon; NaN if past segment end
    horizons_s: list[int]

    def __len__(self) -> int:
        return len(self.t)

    def subset(self, mask: np.ndarray) -> PositionSamples:
        return PositionSamples(
            day=self.day[mask],
            segment_id=self.segment_id[mask],
            t=self.t[mask],
            vehicle=self.vehicle[mask],
            hour=self.hour[mask],
            history=self.history[mask],
            future=self.future[mask],
            horizons_s=self.horizons_s,
        )


def list_days(data_dir: Path = DATA_DIR) -> list[str]:
    return sorted(p.stem.removeprefix("segments-") for p in data_dir.glob("segments-*.parquet"))


def split_days(days: list[str], held_out_fraction: float = 0.3) -> tuple[list[str], list[str]]:
    """Chronological split: the latest days are held out (at least one, if there are two or more)."""
    days = sorted(days)
    if len(days) < 2:
        raise ValueError(f"need at least 2 days to split, have {len(days)}")
    n_held = min(len(days) - 1, max(1, round(len(days) * held_out_fraction)))
    return days[:-n_held], days[-n_held:]


def _segments(path: Path):
    table = pq.read_table(path, columns=["segment_id", "category", "t", "lat", "lon", "alt_ft"])
    seg = np.array(table["segment_id"].to_pylist())
    cat = np.array([c or "" for c in table["category"].to_pylist()])
    cols = {c: table[c].to_numpy().astype(float) for c in ["t", "lat", "lon", "alt_ft"]}
    order = np.lexsort((cols["t"], seg))
    seg, cat = seg[order], cat[order]
    cols = {c: v[order] for c, v in cols.items()}
    starts = np.flatnonzero(np.r_[True, seg[1:] != seg[:-1]])
    ends = np.r_[starts[1:], len(seg)]
    for a, b in zip(starts, ends, strict=True):
        xyz = np.column_stack([cols["lat"][a:b], cols["lon"][a:b], cols["alt_ft"][a:b] * FT_TO_M])
        yield str(seg[a]), str(cat[a]), cols["t"][a:b], xyz


def build_position_samples(
    days: list[str],
    data_dir: Path = DATA_DIR,
    stride_s: int = 30,
    horizons_s: list[int] | None = None,
) -> PositionSamples:
    """Samples from every segment of the given days (1 Hz data, so index = seconds)."""
    horizons = horizons_s or POSITION_HORIZONS_S
    rows: dict[str, list] = {k: [] for k in ["day", "seg", "t", "veh", "hour", "hist", "fut"]}
    for day in days:
        for seg_id, category, t, xyz in _segments(data_dir / f"segments-{day}.parquet"):
            if np.isnan(xyz[:, 2]).any():  # altitude gaps: fill from neighbours
                ok = ~np.isnan(xyz[:, 2])
                if ok.sum() < 2:
                    continue
                xyz[:, 2] = np.interp(np.arange(len(xyz)), np.flatnonzero(ok), xyz[ok, 2])
            n = len(t)
            for i in range(HISTORY_S - 1, n - min(horizons), stride_s):
                fut = np.full((len(horizons), 3), np.nan)
                for j, h in enumerate(horizons):
                    if i + h < n:
                        fut[j] = xyz[i + h]
                local = datetime.fromtimestamp(float(t[i]), UTC).astimezone(FLORIDA_UTC_OFFSET)
                rows["day"].append(day)
                rows["seg"].append(seg_id)
                rows["t"].append(float(t[i]))
                rows["veh"].append(CATEGORY_TO_VEHICLE.get(category, ""))
                rows["hour"].append(local.hour + local.minute / 60.0)
                rows["hist"].append(xyz[i - HISTORY_S + 1 : i + 1])
                rows["fut"].append(fut)
    if not rows["t"]:
        empty = np.zeros((0,))
        return PositionSamples(
            empty.astype(str),
            empty.astype(str),
            empty,
            empty.astype(str),
            empty,
            np.zeros((0, HISTORY_S, 3)),
            np.zeros((0, len(horizons), 3)),
            horizons,
        )
    return PositionSamples(
        day=np.array(rows["day"]),
        segment_id=np.array(rows["seg"]),
        t=np.array(rows["t"]),
        vehicle=np.array(rows["veh"]),
        hour=np.array(rows["hour"]),
        history=np.stack(rows["hist"]),
        future=np.stack(rows["fut"]),
        horizons_s=horizons,
    )
