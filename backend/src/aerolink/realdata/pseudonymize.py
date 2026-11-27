"""Pseudonymize a directory of adsb.lol segment files before they are committed (L-FIX C-09).

    uv run python -m aerolink.realdata.pseudonymize --data backend/data/adsblol

Every ``segments-*.parquet`` in the directory is rewritten in one pass: ``registration`` is dropped and ``icao``,
``callsign`` and the icao part of ``segment_id`` become order-preserving pseudonyms (AC0001..., CS0001...), so the
benchmarks read the same results. The mapping is built in memory and discarded; nothing about it is written.

Run it once over the whole directory. It refuses files that are already pseudonymized (no ``registration`` column
and pseudonym-shaped icao codes), so a second run changes nothing and cannot mix two mappings.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pyarrow.parquet as pq

from aerolink.realdata.pseudonym import monotone_pseudonyms, rewrite_file

PSEUDONYM_ICAO = re.compile(r"^AC\d{4}$")


def already_pseudonymized(path: Path) -> bool:
    t = pq.read_table(path, columns=["icao"])
    if "registration" in pq.read_schema(path).names:
        return False
    return all(PSEUDONYM_ICAO.match(x) for x in set(t["icao"].to_pylist()) if x is not None)


def pseudonymize_dir(data: Path) -> tuple[int, int]:
    """Rewrite every segments file under ``data``. Returns (files, rows). Raises if any file is already done."""
    files = sorted(data.glob("segments-*.parquet"))
    if not files:
        raise SystemExit(f"no segments-*.parquet files in {data}")
    done = [f.name for f in files if already_pseudonymized(f)]
    if done:
        raise SystemExit(f"already pseudonymized (refusing to mix mappings): {', '.join(done[:3])} ...")
    icaos: set[str] = set()
    callsigns: set[str] = set()
    for f in files:
        t = pq.read_table(f, columns=["icao", "callsign"])
        icaos |= set(t["icao"].to_pylist())
        callsigns |= {c for c in t["callsign"].to_pylist() if c is not None}
    icao_map = monotone_pseudonyms(icaos, "AC")
    call_map = monotone_pseudonyms(callsigns, "CS")
    rows = sum(rewrite_file(f, icao_map, call_map) for f in files)
    return len(files), rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, required=True, help="directory with the segments-*.parquet files")
    a = ap.parse_args(argv)
    files, rows = pseudonymize_dir(a.data)
    print(f"pseudonymized {files} files, {rows:,} rows in {a.data} (mapping not kept)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
