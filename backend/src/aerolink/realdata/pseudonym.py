"""Pseudonyms for real-aircraft identifiers in the public per-second data (L-FIX, finding C-09).

The committed ``segments-*.parquet`` files carried ICAO addresses, registrations and callsigns. ``registration`` is
dropped; ``icao`` and ``callsign`` are replaced by pseudonyms, and the ``segment_id`` (which starts with the ICAO
address) is rewritten the same way.

Two schemes, for two jobs:

* ``monotone_pseudonyms``: the historical files. Pseudonyms are assigned in sorted order of the original values, so
  sorting the rewritten segment ids gives the same order as sorting the originals. The benchmarks sort and sample
  by these ids, so an order-preserving rewrite is what keeps their results bit-identical (the L-FIX guard).
* ``KeyedPseudonyms``: new collection days. A per-run random key (never stored) gives each aircraft a pseudonym
  that cannot be linked across runs or to the public registry. Order is not preserved; this is for new data only.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from collections.abc import Iterable, Mapping
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

ICAO_PREFIX = "AC"
CALLSIGN_PREFIX = "CS"
WIDTH = 4  # AC0001 ... AC9999; more aircraft than that would need a wider scheme (checked)


def monotone_pseudonyms(values: Iterable[str], prefix: str) -> dict[str, str]:
    """``value -> prefix + index`` in sorted order of the values (fixed width, so the order is kept as strings)."""
    uniq = sorted(set(values))
    if len(uniq) >= 10**WIDTH:
        raise ValueError(f"{len(uniq)} values do not fit the {prefix}{'0' * WIDTH} scheme")
    return {v: f"{prefix}{i:0{WIDTH}d}" for i, v in enumerate(uniq, 1)}


def rewrite_segment_id(segment_id: str, icao: str, icao_map: Mapping[str, str]) -> str:
    """Replace the leading ICAO address of ``icao-t0-index`` with its pseudonym; the rest is kept."""
    if not segment_id.startswith(icao + "-"):
        raise ValueError(f"segment id {segment_id!r} does not start with its icao {icao!r}")
    return icao_map[icao] + segment_id[len(icao) :]


def rewrite_file(path: Path, icao_map: Mapping[str, str], callsign_map: Mapping[str, str]) -> int:
    """Rewrite one segments file in place: drop ``registration``, pseudonymize ``icao``, ``callsign`` and
    ``segment_id``. Atomic (temp file, then replace). Raises if the rewrite would change the order of segment ids.
    Returns the row count."""
    table = pq.read_table(path)
    icao = table["icao"].to_pylist()
    seg = table["segment_id"].to_pylist()
    new_seg = [rewrite_segment_id(s, i, icao_map) for s, i in zip(seg, icao, strict=True)]
    order_old = sorted(range(len(seg)), key=lambda k: seg[k])
    if [new_seg[k] for k in order_old] != sorted(new_seg):
        raise ValueError(f"{path.name}: rewrite would change the order of segment ids; refusing")
    cols = {name: table[name] for name in table.column_names if name != "registration"}
    cols["icao"] = pa.array([icao_map[i] for i in icao], type=pa.string())
    cols["segment_id"] = pa.array(new_seg, type=pa.string())
    cols["callsign"] = pa.array(
        [None if c is None else callsign_map[c] for c in table["callsign"].to_pylist()], type=pa.string()
    )
    out = pa.table(cols).replace_schema_metadata(table.schema.metadata)
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(out, tmp, compression="zstd", compression_level=9)
    os.replace(tmp, path)
    return out.num_rows


class KeyedPseudonyms:
    """Keyed pseudonyms for new data: HMAC-SHA256 with a random per-run key that is never written anywhere."""

    def __init__(self, key: bytes | None = None):
        self._key = key or secrets.token_bytes(32)

    def _tag(self, prefix: str, value: str) -> str:
        digest = hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()
        return f"{prefix}{digest[:10].upper()}"

    def icao(self, value: str) -> str:
        return self._tag(ICAO_PREFIX, value)

    def callsign(self, value: str | None) -> str | None:
        return None if value is None else self._tag(CALLSIGN_PREFIX, value)

    def segment_id(self, segment_id: str, icao: str) -> str:
        return self.icao(icao) + segment_id[len(icao) :]
