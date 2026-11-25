"""Build the real-data set: one Parquet file of clean segments per day (PLAN E5.1).

    uv run python -m aerolink.realdata.fetch --days 14
    uv run python -m aerolink.realdata.fetch --days 1 --end 2026.11.20     # one day
    uv run python -m aerolink.realdata.fetch --summary                      # counts so far

Days already processed are skipped, so an interrupted run can simply be restarted.
Output (the cleaned, filtered data is small and committed, under ODbL; raw data never is):
    backend/data/adsblol/segments-YYYY-MM-DD.parquet
    backend/data/adsblol/report-YYYY-MM-DD.json
The combined counts go to backend/eval-results/realdata-e5.1.json.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from aerolink.realdata.config import FilterConfig
from aerolink.realdata.parse import decode, parse_trace
from aerolink.realdata.segments import COLUMNS, Segment, extract_segments
from aerolink.realdata.source import find_release, iter_trace_files, open_parts

BACKEND = Path(__file__).resolve().parents[3]
DEFAULT_OUT = BACKEND / "data" / "adsblol"
DEFAULT_SUMMARY = BACKEND / "eval-results" / "realdata-e5.1.json"
ROUNDING = {"lat": 6, "lon": 6, "alt_ft": 0, "gs_kt": 1, "track_deg": 1, "vrate_fpm": 0}
ATTRIBUTION = "Contains data from adsb.lol, licensed under the Open Database License (ODbL 1.0)."


def process_stream(
    stream: io.BufferedIOBase, cfg: FilterConfig, progress: bool = False
) -> tuple[list[Segment], Counter]:
    counts: Counter = Counter()
    segments: list[Segment] = []
    started = time.monotonic()
    for _, raw in iter_trace_files(stream):
        counts["files_read"] += 1
        try:
            trace = parse_trace(decode(raw))
        except (ValueError, OSError):
            counts["files_unreadable"] += 1
            continue
        segments.extend(extract_segments(trace, cfg, counts))
        if progress and counts["files_read"] % 20000 == 0:
            rate = counts["files_read"] / max(1e-9, time.monotonic() - started)
            print(
                f"  {counts['files_read']:,} aircraft read ({rate:,.0f}/s), "
                f"{counts['aircraft_kept']} kept, {counts['segments_kept']} segments",
                flush=True,
            )
    return segments, counts


def to_table(segments: list[Segment], day: date) -> pa.Table:
    cols: dict[str, list] = {
        k: [] for k in ["day", "segment_id", "icao", "registration", "type_code", "category", "callsign", *COLUMNS]
    }
    for s in segments:
        n = len(s.data["t"])
        for key, value in (
            ("day", day.isoformat()),
            ("segment_id", s.segment_id),
            ("icao", s.icao),
            ("registration", s.registration),
            ("type_code", s.type_code),
            ("category", s.category),
            ("callsign", s.callsign),
        ):
            cols[key].extend([value] * n)
        for c in COLUMNS:
            cols[c].extend(s.data[c].tolist())
    schema = pa.schema(
        [(k, pa.string()) for k in ["day", "segment_id", "icao", "registration", "type_code", "category", "callsign"]]
        + [("t", pa.int64())]
        + [(c, pa.float64() if c in ("lat", "lon") else pa.float32()) for c in COLUMNS[1:]]
    )
    cols["t"] = [int(x) for x in cols["t"]]
    for c, digits in ROUNDING.items():  # ~5 cm on lat/lon; keeps the files small
        cols[c] = [round(v, digits) for v in cols[c]]
    return pa.table(cols, schema=schema).replace_schema_metadata({"license": ATTRIBUTION})


def write_day(segments: list[Segment], counts: Counter, day: date, source: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        to_table(segments, day),
        out / f"segments-{day.isoformat()}.parquet",
        compression="zstd",
        compression_level=9,
    )
    report = {
        "day": day.isoformat(),
        "source": source,
        "attribution": ATTRIBUTION,
        "counts": dict(sorted(counts.items())),
    }
    (out / f"report-{day.isoformat()}.json").write_text(json.dumps(report, indent=2) + "\n")


def summarize(out: Path) -> dict:
    reports = [json.loads(p.read_text()) for p in sorted(out.glob("report-*.json"))]
    total: Counter = Counter()
    for r in reports:
        total.update(r["counts"])
    return {
        "attribution": ATTRIBUTION,
        "filters": "aerolink.realdata.config.FilterConfig defaults",
        "days": [
            {
                "day": r["day"],
                "source": r["source"],
                "aircraft_kept": r["counts"].get("aircraft_kept", 0),
                "segments_kept": r["counts"].get("segments_kept", 0),
            }
            for r in reports
        ],
        "totals": dict(sorted(total.items())),
    }


def print_summary(summary: dict) -> None:
    t = summary["totals"]
    print(f"\nDays processed: {len(summary['days'])}")
    for d in summary["days"]:
        print(f"  {d['day']} ({d['source']}): {d['aircraft_kept']} aircraft, {d['segments_kept']} segments")
    hours = t.get("segment_seconds", 0) / 3600
    print("\nFunnel (all days):")
    for key in [
        "files_read",
        "aircraft_candidate",
        "points_in_region",
        "points_kept",
        "aircraft_kept",
        "segments_kept",
    ]:
        print(f"  {key:<22} {t.get(key, 0):>12,}")
    print(f"  {'flight_hours':<22} {hours:>12,.1f}")
    dropped = {k: v for k, v in t.items() if "dropped" in k}
    if dropped:
        print("\nWhy things were dropped:")
        for k, v in sorted(dropped.items(), key=lambda kv: -kv[1]):
            print(f"  {k:<45} {v:>12,}")


def run_day(day: date, cfg: FilterConfig, out: Path) -> bool:
    if (out / f"report-{day.isoformat()}.json").exists():
        print(f"{day}: already done, skipping")
        return True
    found = find_release(day)
    if found is None:
        print(f"{day}: no release published (yet); skipping")
        return False
    instance, urls = found
    print(f"{day}: streaming {len(urls)} parts from {instance} ...", flush=True)
    started = time.monotonic()
    with open_parts(urls) as stream:
        segments, counts = process_stream(stream, cfg, progress=True)
    write_day(segments, counts, day, instance, out)
    print(
        f"{day}: done in {(time.monotonic() - started) / 60:.1f} min: {counts['aircraft_kept']} aircraft, "
        f"{counts['segments_kept']} segments ({counts['segment_seconds'] / 3600:.1f} flight hours)",
        flush=True,
    )
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=14, help="how many days, counting back from --end")
    ap.add_argument("--end", help="last day, YYYY.MM.DD (default: 3 days ago, so uploads are complete)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--summary-file", type=Path, default=DEFAULT_SUMMARY)
    ap.add_argument("--summary", action="store_true", help="only print and save the combined counts")
    ap.add_argument("--from-tar", type=Path, help="process a local tar instead of downloading (testing)")
    a = ap.parse_args(argv)
    cfg = FilterConfig()

    if a.from_tar:
        day = datetime.strptime(a.end, "%Y.%m.%d").date() if a.end else date.today()
        with a.from_tar.open("rb") as f:
            segments, counts = process_stream(f, cfg)
        write_day(segments, counts, day, f"file:{a.from_tar.name}", a.out)
    elif not a.summary:
        end = datetime.strptime(a.end, "%Y.%m.%d").date() if a.end else date.today() - timedelta(days=3)
        for i in range(a.days):
            run_day(end - timedelta(days=i), cfg, a.out)

    summary = summarize(a.out)
    if not summary["days"]:
        print("No days processed yet.")
        return 1
    print_summary(summary)
    a.summary_file.parent.mkdir(parents=True, exist_ok=True)
    a.summary_file.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"\nwrote {a.summary_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
