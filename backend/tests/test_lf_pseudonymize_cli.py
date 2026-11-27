"""L-FIX: the pseudonymize CLI rewrites a whole directory once and refuses a second run (no mixed mappings)."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from aerolink.realdata.pseudonymize import already_pseudonymized, main


def _write(path: Path, rows: list[tuple[str, str, str | None]]) -> None:
    pq.write_table(
        pa.table(
            {
                "day": ["2026-11-09"] * len(rows),
                "segment_id": [f"{i}-100-0" for i, _, _ in rows],
                "icao": [i for i, _, _ in rows],
                "registration": [r for _, r, _ in rows],
                "type_code": ["B738"] * len(rows),
                "category": ["A3"] * len(rows),
                "callsign": [c for _, _, c in rows],
                "t": [100] * len(rows),
                "lat": [28.0] * len(rows),
                "lon": [-81.5] * len(rows),
                "alt_ft": [1000.0] * len(rows),
                "gs_kt": [120.0] * len(rows),
            }
        ),
        path,
    )


def test_cli_pseudonymizes_the_directory_then_refuses_to_run_again(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    _write(tmp_path / "segments-2026-11-09.parquet", [("abc123", "N1AB", "AAL1"), ("~ff0011", "N2CD", None)])
    _write(tmp_path / "segments-2026-11-10.parquet", [("abc123", "N1AB", "AAL9")])
    assert main(["--data", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "pseudonymized 2 files, 3 rows" in out and "mapping not kept" in out
    for f in tmp_path.glob("segments-*.parquet"):
        t = pq.read_table(f)
        assert "registration" not in t.column_names
        assert already_pseudonymized(f)
        assert all(x.startswith("AC") for x in t["icao"].to_pylist())
    # the same aircraft gets the same pseudonym in both files
    a = pq.read_table(tmp_path / "segments-2026-11-09.parquet")["icao"].to_pylist()[0]
    b = pq.read_table(tmp_path / "segments-2026-11-10.parquet")["icao"].to_pylist()[0]
    assert a == b
    with pytest.raises(SystemExit, match="already pseudonymized"):
        main(["--data", str(tmp_path)])
