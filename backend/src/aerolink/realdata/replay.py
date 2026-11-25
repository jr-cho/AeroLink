"""Replay real flights as simulator background traffic (PLAN E5.1, E3.3 T6).

The simulator's background aircraft fly along network nodes (`BackgroundAircraft`:
a node path plus the time at each node). A real 1 Hz segment is turned into one by
map matching: for every second, the nearest network node within `radius_m`; the
consecutive node visits that are joined by a corridor edge become the path, with the
time of closest approach at each node. Flights that never follow a corridor between
two nodes are skipped: they can't meet AeroLink traffic in the traffic index anyway.

Real flights keep their time of day: a segment flown 08:10-08:25 local appears in a
scenario starting at 08:00 local from t = 600 s.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from aerolink.network import Network
from aerolink.sim.background import BackgroundAircraft

FT_TO_M = 0.3048
KT_TO_MPS = 0.514444


def _node_xy(network: Network) -> tuple[list[str], np.ndarray]:
    ids = sorted(network.nodes)
    lat0 = math.radians(28.0)
    xy = np.array(
        [
            [
                math.radians(network.nodes[n]["lon"]) * 6371000 * math.cos(lat0),
                math.radians(network.nodes[n]["lat"]) * 6371000,
            ]
            for n in ids
        ]
    )
    return ids, xy


def match_segment(
    network: Network,
    t: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    radius_m: float = 1500.0,
) -> tuple[list[str], list[float]]:
    """Longest run of corridor edges the track follows: (node path, unix time at each node)."""
    ids, xy = _node_xy(network)
    lat0 = math.radians(28.0)
    pts = np.column_stack([np.radians(lon) * 6371000 * math.cos(lat0), np.radians(lat) * 6371000])
    d = np.linalg.norm(pts[:, None, :] - xy[None, :, :], axis=2)
    nearest = d.argmin(axis=1)
    near = d[np.arange(len(pts)), nearest] <= radius_m

    visits: list[tuple[str, float, float]] = []  # (node, time of closest approach, distance)
    for i in np.flatnonzero(near):
        node, dist = ids[nearest[i]], float(d[i, nearest[i]])
        if visits and visits[-1][0] == node:
            if dist < visits[-1][2]:
                visits[-1] = (node, float(t[i]), dist)
        else:
            visits.append((node, float(t[i]), dist))

    best: list[tuple[str, float, float]] = []
    run: list[tuple[str, float, float]] = []
    for v in visits:
        if run and network.graph.has_edge(run[-1][0], v[0]) and v[1] > run[-1][1]:
            run.append(v)
        else:
            run = [v]
        if len(run) > len(best):
            best = list(run)
    if len(best) < 2:
        return [], []
    return [v[0] for v in best], [v[1] for v in best]


def load_replay(
    path: Path,
    network: Network,
    scenario_start: str,
    duration_s: float,
    limit: int = 0,
) -> list[BackgroundAircraft]:
    """Background aircraft from one day's segments Parquet, in the scenario's time window.

    `scenario_start` is the scenario's ISO start time with its UTC offset; only flights
    whose matched path starts and ends inside [0, duration_s] of that time of day are used.
    """
    start = datetime.fromisoformat(scenario_start)
    utc_offset = start.utcoffset()
    offset_s = utc_offset.total_seconds() if utc_offset is not None else 0.0
    start_sod = start.hour * 3600 + start.minute * 60 + start.second

    table = pq.read_table(path, columns=["segment_id", "t", "lat", "lon", "alt_ft", "gs_kt"])
    seg_ids = np.array(table["segment_id"].to_pylist())
    cols = {c: table[c].to_numpy().astype(float) for c in ["t", "lat", "lon", "alt_ft", "gs_kt"]}
    order = np.lexsort((cols["t"], seg_ids))  # by segment, then time
    seg_ids = seg_ids[order]
    cols = {c: v[order] for c, v in cols.items()}
    starts = np.flatnonzero(np.r_[True, seg_ids[1:] != seg_ids[:-1]])
    ends = np.r_[starts[1:], len(seg_ids)]

    out: list[BackgroundAircraft] = []
    for a, b in zip(starts, ends, strict=True):
        nodes, times = match_segment(network, cols["t"][a:b], cols["lat"][a:b], cols["lon"][a:b])
        if not nodes:
            continue
        local = [((tt + offset_s) % 86400) - start_sod for tt in times]
        if local[0] < 0 or local[-1] > duration_s:
            continue
        out.append(
            BackgroundAircraft(
                aircraft_id=f"rd-{str(seg_ids[a]).split('-')[0]}-{len(out):03d}",
                path=nodes,
                times_s=local,
                alt_msl_m=float(np.nanmedian(cols["alt_ft"][a:b])) * FT_TO_M,
                speed_mps=float(np.nanmean(cols["gs_kt"][a:b])) * KT_TO_MPS,
            )
        )
        if limit and len(out) >= limit:
            break
    return out
