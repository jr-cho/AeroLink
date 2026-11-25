"""Real-data pipeline (PLAN E5.1) on synthetic files in the exact readsb trace format.

No network access: a fake day is built as a tar of gzip trace files, split into
`.tar.aa` / `.tar.ab` parts the way adsb.lol publishes them, and streamed back.
"""

import dataclasses
import gzip
import io
import json
import tarfile
from collections import Counter
from datetime import date
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from aerolink.network import Network
from aerolink.realdata.config import FilterConfig
from aerolink.realdata.fetch import main, process_stream, summarize, write_day
from aerolink.realdata.parse import decode, parse_trace
from aerolink.realdata.replay import load_replay, match_segment
from aerolink.realdata.segments import extract_segments, resample
from aerolink.realdata.source import ConcatStream
from aerolink.sim.runner import run_simulation
from aerolink.sim.scenarios import load_scenario

BASE = 1763640000  # 2025-11-20T12:00:00Z
CFG = FilterConfig()


def point(dt, lat, lon, alt=1500, gs=90, track=90.0, flags=0, details=None, source="adsb_icao"):
    return [dt, lat, lon, alt, gs, track, flags, 0, details, source, None, None, None, None]


def trace(icao="a1b2c3", points=None, t="R44", flags=0, category="A7", callsign="N123AB"):
    pts = points if points is not None else [point(i, 28.0, -82.0 + i * 0.0003) for i in range(0, 300, 2)]
    if pts and category is not None:
        pts[0][8] = {"category": category, "flight": f"{callsign:<8}"}
    return {"icao": icao, "r": "N123AB", "t": t, "dbFlags": flags, "timestamp": BASE, "trace": pts}


def segments_of(doc):
    counts: Counter = Counter()
    return extract_segments(parse_trace(doc), CFG, counts), counts


# --- parsing ----------------------------------------------------------------------------
def test_decode_reads_gzip_and_plain_json():
    doc = trace()
    assert decode(gzip.compress(json.dumps(doc).encode())) == doc
    assert decode(json.dumps(doc).encode()) == doc


def test_parse_reads_points_category_and_callsign():
    tr = parse_trace(trace())
    assert tr.icao == "a1b2c3" and tr.category == "A7" and tr.callsign == "N123AB"
    p = tr.points[1]
    assert p.t == BASE + 2 and p.alt_ft == 1500 and p.source == "adsb_icao" and not p.on_ground


# --- filters ----------------------------------------------------------------------------
def test_helicopter_in_region_is_kept_and_resampled_to_1hz():
    segs, counts = segments_of(trace())
    assert len(segs) == 1 and counts["aircraft_kept"] == 1
    t = segs[0].data["t"]
    assert np.all(np.diff(t) == 1.0) and segs[0].duration_s >= 290


def test_aircraft_level_rules():
    assert segments_of(trace(points=[point(i, 25.8, -80.2) for i in range(0, 300, 2)]))[1][
        "aircraft_dropped_outside_region"
    ]
    assert segments_of(trace(flags=1))[1]["aircraft_dropped_military"]
    assert segments_of(trace(callsign="SWA1234"))[1]["aircraft_dropped_airline_callsign"]
    assert segments_of(trace(category="A3"))[1]["aircraft_dropped_category_not_light_or_rotor"]
    assert segments_of(trace(category=None))[1]["aircraft_kept"]  # unknown category is kept


def test_point_rules_drop_mlat_ground_high_and_slow():
    bad = [
        point(0, 28.0, -82.0, source="mlat"),
        point(2, 28.0, -82.0, alt="ground"),
        point(4, 28.0, -82.0, alt=5000),
        point(6, 28.0, -82.0, gs=20),
    ]
    _, counts = segments_of(trace(points=bad + [point(i, 28.0, -82.0) for i in range(8, 300, 2)]))
    for why in ("source", "altitude", "speed"):
        assert counts[f"points_dropped_point_{why}"] >= 1


def test_tracks_split_at_gaps_and_new_legs_and_short_pieces_dropped():
    first = [point(i, 28.0, -82.0) for i in range(0, 120, 2)]
    after_gap = [point(i, 28.0, -82.0) for i in range(200, 320, 2)]  # 80 s gap
    leg = [point(i, 28.0, -82.0, flags=2 if i == 400 else 0) for i in range(320, 500, 2)]
    short = [point(i, 28.0, -82.0) for i in range(600, 640, 2)]  # 40 s: too short
    segs, counts = segments_of(trace(points=first + after_gap + leg + short))
    # 0-118 s | gap | 200-398 s | new leg | 400-498 s | gap | 600-638 s (too short)
    assert len(segs) == 3 and counts["segments_dropped_too_short"] == 1


def test_resample_unwraps_track_across_north():
    tr = parse_trace(trace(points=[point(0, 28, -82, track=350), point(10, 28, -82, track=10)]))
    out = resample(tr.points, 1.0)
    assert np.all((out["track_deg"] <= 10) | (out["track_deg"] >= 350))  # never swings through 180


# --- streaming a published day -------------------------------------------------------------
def fake_day(docs: list[dict]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for i, doc in enumerate(docs):
            data = gzip.compress(json.dumps(doc).encode())
            info = tarfile.TarInfo(f"./traces/{i:02x}/trace_full_{doc['icao']}.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        info = tarfile.TarInfo("./heatmap/00.bin.ttf")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"xyz"))  # not a trace: ignored
    return buf.getvalue()


def test_split_parts_stream_as_one_tar_and_write_parquet(tmp_path: Path):
    day = fake_day([trace(icao="a00001"), trace(icao="a00002", flags=1), trace(icao="a00003", category="A5")])
    half = len(day) // 2
    stream = io.BufferedReader(ConcatStream([io.BytesIO(day[:half]), io.BytesIO(day[half:])]))
    segments, counts = process_stream(stream, CFG)
    assert counts["files_read"] == 3 and counts["aircraft_kept"] == 1 and len(segments) == 1

    write_day(segments, counts, date(2025, 11, 20), "test", tmp_path)
    table = pq.read_table(tmp_path / "segments-2025-11-20.parquet")
    assert table.num_rows == len(segments[0].data["t"])
    assert b"ODbL" in table.schema.metadata[b"license"]
    summary = summarize(tmp_path)
    assert summary["totals"]["aircraft_kept"] == 1 and summary["days"][0]["day"] == "2025-11-20"


def test_cli_processes_a_local_tar(tmp_path: Path):
    (tmp_path / "day.tar").write_bytes(fake_day([trace()]))
    out, summary = tmp_path / "out", tmp_path / "summary.json"
    rc = main(
        [
            "--from-tar",
            str(tmp_path / "day.tar"),
            "--end",
            "2025.11.20",
            "--out",
            str(out),
            "--summary-file",
            str(summary),
        ]
    )
    assert rc == 0 and (out / "segments-2025-11-20.parquet").exists()
    assert json.loads(summary.read_text())["totals"]["segments_kept"] == 1


# --- replay as background traffic -----------------------------------------------------------
def corridor_track(network: Network, start_unix: float):
    """A 1 Hz track flying the first corridor edge between two waypoints."""
    edge = next(e for (a, b), e in network.edges.items() if a.startswith("wp-") and b.startswith("wp-"))
    coords = np.array(edge.geometry)  # (lon, lat)
    n = 600
    s = np.linspace(0, 1, n)
    idx = np.linspace(0, len(coords) - 1, len(coords))
    lon = np.interp(s * (len(coords) - 1), idx, coords[:, 0])
    lat = np.interp(s * (len(coords) - 1), idx, coords[:, 1])
    return edge, start_unix + np.arange(n, dtype=float), lat, lon


def test_match_segment_follows_a_corridor(fl_network: Network):
    edge, t, lat, lon = corridor_track(fl_network, BASE)
    nodes, times = match_segment(fl_network, t, lat, lon)
    assert len(nodes) >= 2 and fl_network.graph.has_edge(nodes[0], nodes[1])
    assert times == sorted(times)


def test_replay_feeds_the_simulator(fl_network: Network, tmp_path: Path):
    # Scenario starts 2026-11-20 08:00 local (UTC-5); fly the corridor from 08:05.
    start_unix = 1795611600 + 300  # 2026-11-20T13:05:00Z
    edge, t, lat, lon = corridor_track(fl_network, start_unix)
    n = len(t)
    table = {
        "day": ["2026-11-20"] * n,
        "segment_id": ["abc123-1-0"] * n,
        "icao": ["abc123"] * n,
        "registration": [None] * n,
        "type_code": ["R44"] * n,
        "category": ["A7"] * n,
        "callsign": [None] * n,
        "t": t.astype(int),
        "lat": lat,
        "lon": lon,
        "alt_ft": np.full(n, 1500.0),
        "gs_kt": np.full(n, 90.0),
        "track_deg": np.zeros(n),
        "vrate_fpm": np.zeros(n),
    }
    import pyarrow as pa

    pq.write_table(pa.table(table), tmp_path / "segments.parquet")
    aircraft = load_replay(tmp_path / "segments.parquet", fl_network, "2026-11-20T08:00:00-05:00", 3600)
    assert len(aircraft) == 1 and 300 <= aircraft[0].times_s[0] < aircraft[0].times_s[-1] <= 900

    base = load_scenario(Path(__file__).resolve().parents[1] / "scenarios" / "rogue_aircraft.yaml")
    scenario = dataclasses.replace(
        base,
        start_time="2026-11-20T08:00:00-05:00",
        duration_s=900,
        background_traffic=dataclasses.replace(
            base.background_traffic, replay_file=str(tmp_path / "segments.parquet"), count=0
        ),
    )
    result = run_simulation(fl_network, scenario)
    assert any(m["aircraft_id"].startswith("rd-abc123") for m in result.telemetry)
