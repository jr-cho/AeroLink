"""L-FIX C-09: the public per-second data carries pseudonyms, not registrations or real callsigns.

The historical rewrite must not change what the benchmarks read: segment ids keep their order, so sampling and
grouping give the same results (the E5.2 guard is checked by re-running the benchmark, not in this file).
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from aerolink.realdata.pseudonym import (
    KeyedPseudonyms,
    monotone_pseudonyms,
    rewrite_file,
    rewrite_segment_id,
)


def _table(rows: Sequence[tuple[str, str, str | None, int]]) -> pa.Table:
    """rows: (icao, registration, callsign, t) with one segment per icao (segment id = icao-t-0)."""
    return pa.table(
        {
            "day": ["2026-11-09"] * len(rows),
            "segment_id": [f"{i}-{t}-0" for i, _, _, t in rows],
            "icao": [i for i, _, _, _ in rows],
            "registration": [r for _, r, _, _ in rows],
            "type_code": ["B738"] * len(rows),
            "category": ["A3"] * len(rows),
            "callsign": [c for _, _, c, _ in rows],
            "t": [t for _, _, _, t in rows],
            "lat": [28.0] * len(rows),
            "lon": [-81.5] * len(rows),
            "alt_ft": [1000.0] * len(rows),
            "gs_kt": [120.0] * len(rows),
        }
    )


def test_monotone_pseudonyms_keep_the_order_of_the_originals_including_seven_character_codes():
    icaos = ["a1b2c3", "~ff0011", "00ffab", "abc123", "abc1234", "0a0b0c"]
    m = monotone_pseudonyms(icaos, "AC")
    assert sorted(icaos) == sorted(m, key=lambda k: m[k])  # the same order, by construction
    assert all(len(v) == len("AC0001") for v in m.values())
    segs = sorted(f"{i}-1700000000-0" for i in icaos)  # the order the benchmarks sort by
    new = [rewrite_segment_id(s, s.split("-")[0], m) for s in segs]
    assert new == sorted(new)  # the rewritten ids keep that order


def test_rewrite_keeps_row_order_drops_registration_and_pseudonymizes(tmp_path: Path):
    rng = random.Random(7)
    rows: list[tuple[str, str, str | None, int]] = []
    for k in range(40):
        icao = f"{rng.randrange(16**6):06x}" if k % 5 else f"~{rng.randrange(16**6):06x}"
        rows.append((icao, f"N{k}AB", f"AAL{k}", 1700000000 + k))
    path = tmp_path / "segments-2026-11-09.parquet"
    pq.write_table(_table(rows), path)
    before = pq.read_table(path)
    seg_before = before["segment_id"].to_pylist()
    icao_map = monotone_pseudonyms([r[0] for r in rows], "AC")
    call_map = {c: f"CS{i:04d}" for i, c in enumerate(sorted({r[2] for r in rows if r[2] is not None}), 1) if c}
    n = rewrite_file(path, icao_map, call_map)
    after = pq.read_table(path)
    assert n == len(rows) == after.num_rows
    assert "registration" not in after.column_names
    text = " ".join(after["icao"].to_pylist() + after["callsign"].to_pylist() + after["segment_id"].to_pylist())
    assert all(r[0] not in text and r[1] not in text and r[2] not in text for r in rows)
    # the readers' view: the same sequence of ids in sorted order, and identical times and positions per row
    order_before = sorted(range(len(seg_before)), key=lambda k: seg_before[k])
    order_after = sorted(range(len(after)), key=lambda k: after["segment_id"].to_pylist()[k])
    assert order_before == order_after
    for col in ("t", "lat", "lon", "alt_ft", "gs_kt", "category", "type_code"):
        assert before[col].to_pylist() == after[col].to_pylist(), col


def test_rewrite_refuses_a_mapping_that_would_reorder_the_ids(tmp_path: Path):
    rows = [("aaaaaa", "N1", "C1", 1), ("bbbbbb", "N2", "C2", 2)]
    path = tmp_path / "segments-2026-11-10.parquet"
    pq.write_table(_table(rows), path)
    reversed_map = {"aaaaaa": "AC0002", "bbbbbb": "AC0001"}  # wrong on purpose: reverses the order
    with pytest.raises(ValueError, match="change the order"):
        rewrite_file(path, reversed_map, {"C1": "CS0001", "C2": "CS0002"})
    assert pq.read_table(path)["icao"].to_pylist() == ["aaaaaa", "bbbbbb"]  # the file is untouched


def test_keyed_pseudonyms_are_stable_within_a_run_and_unlinkable_across_runs():
    a, b = KeyedPseudonyms(key=b"k" * 32), KeyedPseudonyms(key=b"j" * 32)
    assert a.icao("abc123") == a.icao("abc123") and a.icao("abc123") != a.icao("abc124")
    assert a.icao("abc123") != b.icao("abc123")  # another run, another key: not linkable
    assert a.icao("abc123").startswith("AC") and "abc123" not in a.icao("abc123").lower()
    assert a.callsign(None) is None and (a.callsign("AAL12") or "").startswith("CS")
    assert a.segment_id("abc123-1700-3", "abc123") == a.icao("abc123") + "-1700-3"
