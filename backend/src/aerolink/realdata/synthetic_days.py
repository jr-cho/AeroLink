"""Made-up flight days in the E5.1 output format, for tests and dry runs. NOT REAL DATA.

    uv run python -m aerolink.realdata.synthetic_days /tmp/fake-adsblol

Writes ``segments-*.parquet`` and ``report-*.json`` files that look exactly like the real
ones (random helicopter and light-aircraft flights over Central Florida), so every E5.2-E5.5
command can be tried with ``--data /tmp/fake-adsblol`` before the real download finishes.
Never put them in ``backend/data/adsblol``.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from aerolink.realdata.fetch import write_day
from aerolink.realdata.segments import Segment


def make_days(out: Path, days: int = 5, flights_per_day: int = 25, seed: int = 0) -> list[str]:
    rng = np.random.default_rng(seed)
    first = date(2026, 11, 10)
    written = []
    for d in range(days):
        day = first + timedelta(days=d)
        midnight = 1762750800 + d * 86400  # 2026-11-10 00:00 EST
        segments = []
        for k in range(flights_per_day):
            n = int(rng.integers(400, 1200))
            t0 = midnight + int(rng.integers(7 * 3600, 19 * 3600))
            lat, lon = rng.uniform(27.5, 28.6), rng.uniform(-82.6, -81.3)
            heading, speed, alt = rng.uniform(0, 360), rng.uniform(25, 70), rng.uniform(500, 2500)
            turn = 0.0
            rows = []
            for i in range(n):
                if rng.random() < 0.01:
                    turn = rng.normal(0, 1.5)
                if rng.random() < 0.02:
                    turn = 0.0
                heading = (heading + turn) % 360
                speed = float(np.clip(speed + rng.normal(0, 0.3), 22, 80))
                alt += rng.normal(0, 1)
                lat += speed * np.cos(np.radians(heading)) / 111_000
                lon += speed * np.sin(np.radians(heading)) / (111_000 * np.cos(np.radians(lat)))
                rows.append((t0 + i, lat, lon, alt, speed / 0.514444, heading, 0.0))
            a = np.array(rows)
            data = {c: a[:, j] for j, c in enumerate(["t", "lat", "lon", "alt_ft", "gs_kt", "track_deg", "vrate_fpm"])}
            rotor = k % 2 == 1
            segments.append(
                Segment(f"f{k:05x}", 0, None, "R44" if rotor else "C172", "A7" if rotor else "A1", None, data)
            )
        write_day(segments, Counter(segments_kept=len(segments)), day, "synthetic-test-data", out)
        written.append(day.isoformat())
    return written


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/fake-adsblol")
    print(f"wrote {len(make_days(target))} made-up days to {target}")
