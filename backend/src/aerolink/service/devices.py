"""Device registry and provisioning (PLAN E8.3, ARCHITECTURE §14.3, §14.5).

    python -m aerolink.service.devices add dev-001 medical --callsign N123AL [--demo] --site https://aerolink.example
    python -m aerolink.service.devices rotate dev-001 --site https://aerolink.example
    python -m aerolink.service.devices set-callsign dev-001 N456AL
    python -m aerolink.service.devices list

``add`` registers the unit (its MQTT username = aircraft id) with its vehicle type and its radio callsign (2-10
characters: A-Z, 0-9, ``-``; unique; the unit is bound to its aircraft, so is the callsign: pilots never type it),
generates a pairing code, stores only its scrypt hash, and writes the unit's QR label (SVG and PNG, the callsign
printed under the code) for ``https://<site>/device/<id>?code=<code>``. The code is shown once. ``rotate`` makes a new
code and ends every screen session. ``set-callsign`` changes the callsign (the QR code itself stays valid, but print
the label again with ``rotate``). The broker login is separate: ``mosquitto/add-device.sh <id>``.

``<site>`` comes from ``--site`` or the ``AEROLINK_SITE`` environment variable.

Note: there is deliberately no ``--burn-efuse`` here. Burning the vehicle-type eFuse is a
firmware-provisioning step (M8, ``firmware/tools/provision.py``) and is permanent.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import struct
import time
import zlib
from pathlib import Path
from typing import Any

import segno

from aerolink.service import pairing
from aerolink.service.settings import REPO, Settings
from aerolink.service.store import Store

DEFAULT_LABEL_DIR = REPO / "labels"
CALLSIGN_RE = re.compile(r"^[A-Z0-9-]{2,10}$")
CALLSIGN_CACHE_S = 5.0


def validate_callsign(raw: str) -> str:
    """The callsign in upper case, or ValueError. 2-10 characters: A-Z, 0-9, '-'."""
    c = raw.strip().upper()
    if not CALLSIGN_RE.match(c):
        raise ValueError("callsign must be 2-10 characters: A-Z, 0-9 or '-'")
    return c


def callsign_of(aircraft_id: str, source: str, registry: dict[str, str | None]) -> str:
    """What ATC sees for an aircraft: the unit's registry callsign, SIM-P03 for the simulated sim-p03, else the id."""
    return registry.get(aircraft_id) or (aircraft_id.upper() if source == "sim" else aircraft_id)


async def callsign_map(app_state: Any) -> dict[str, str | None]:
    """Registry callsigns by unit id, re-read at most every few seconds (the CLI edits the registry from outside)."""
    cached = getattr(app_state, "callsign_cache", None)
    if cached and time.monotonic() - cached[0] < CALLSIGN_CACHE_S:
        return cached[1]
    rows = await app_state.service.store.list_devices()
    value = {d["id"]: d.get("callsign") for d in rows}
    app_state.callsign_cache = (time.monotonic(), value)
    return value


# 5x7 pixel font for the label text (A-Z, 0-9, '-'): no font files or image library needed.
_FONT = {
    "0": "01110 10001 10011 10101 11001 10001 01110", "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111", "3": "11110 00001 00001 01110 00001 00001 11110",
    "4": "00010 00110 01010 10010 11111 00010 00010", "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110", "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110", "9": "01110 10001 10001 01111 00001 00010 01100",
    "A": "01110 10001 10001 11111 10001 10001 10001", "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01110 10001 10000 10000 10000 10001 01110", "D": "11100 10010 10001 10001 10001 10010 11100",
    "E": "11111 10000 10000 11110 10000 10000 11111", "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01110 10001 10000 10111 10001 10001 01111", "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "01110 00100 00100 00100 00100 00100 01110", "J": "00111 00010 00010 00010 00010 10010 01100",
    "K": "10001 10010 10100 11000 10100 10010 10001", "L": "10000 10000 10000 10000 10000 10000 11111",
    "M": "10001 11011 10101 10101 10001 10001 10001", "N": "10001 10001 11001 10101 10011 10001 10001",
    "O": "01110 10001 10001 10001 10001 10001 01110", "P": "11110 10001 10001 11110 10000 10000 10000",
    "Q": "01110 10001 10001 10001 10101 10010 01101", "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110", "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110", "V": "10001 10001 10001 10001 10001 01010 00100",
    "W": "10001 10001 10001 10101 10101 10101 01010", "X": "10001 10001 01010 00100 01010 10001 10001",
    "Y": "10001 10001 01010 00100 00100 00100 00100", "Z": "11111 00001 00010 00100 01000 10000 11111",
    "-": "00000 00000 00000 11111 00000 00000 00000",
}  # fmt: skip


def pairing_url(site: str, device_id: str, code: str) -> str:
    site = site.strip().rstrip("/")
    if "://" not in site:
        site = f"https://{site}"
    return f"{site}/device/{device_id}?code={code}"


def label_raster(url: str, callsign: str | None, scale: int = 10, border: int = 4) -> list[bytearray]:
    """The label as rows of 0/1 pixels: the QR code and, centred under it, the callsign in a bitmap font."""
    modules = segno.make(url, error="m").matrix
    n = len(modules)
    width = (n + 2 * border) * scale
    text = (callsign or "").upper()
    px = 0
    if text:  # glyph pixel: as big as fits the width (at most 1.2 modules), 6 columns per character
        px = max(1, min(int(scale * 1.2), (width - 2 * scale) // (6 * len(text))))
    text_h = 7 * px + 2 * scale if text else 0
    height = (n + 2 * border) * scale + text_h
    rows = [bytearray(width) for _ in range(height)]
    for y, line in enumerate(modules):
        for x, bit in enumerate(line):
            if bit:
                for dy in range(scale):
                    r = rows[(y + border) * scale + dy]
                    r[(x + border) * scale : (x + border + 1) * scale] = b"\x01" * scale
    if text:
        total = len(text) * 6 * px - px
        x0, y0 = (width - total) // 2, (n + 2 * border) * scale + scale  # below the QR code's own quiet zone
        for ci, ch in enumerate(text):
            for gy, glyph_row in enumerate(_FONT[ch].split()):
                for gx, bit in enumerate(glyph_row):
                    if bit == "1":
                        for dy in range(px):
                            start = x0 + (ci * 6 + gx) * px
                            rows[y0 + gy * px + dy][start : start + px] = b"\x01" * px
    return rows


def _png(rows: list[bytearray]) -> bytes:
    """8-bit greyscale PNG (1 = black), written with zlib only."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + bytes(0 if v else 255 for v in r) for r in rows)
    head = struct.pack(">IIBBBBB", len(rows[0]), len(rows), 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def _svg(rows: list[bytearray], display_scale: float = 0.8) -> str:
    """One path of horizontal runs; ``shape-rendering`` keeps the edges sharp for printing."""
    w, h = len(rows[0]), len(rows)
    d = []
    for y, r in enumerate(rows):
        x = 0
        while x < w:
            if r[x]:
                start = x
                while x < w and r[x]:
                    x += 1
                d.append(f"M{start} {y}h{x - start}v1h-{x - start}z")
            else:
                x += 1
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{round(w * display_scale)}" '
        f'height="{round(h * display_scale)}" shape-rendering="crispEdges"><rect width="{w}" height="{h}" fill="#fff"/>'
        f'<path d="{"".join(d)}" fill="#000"/></svg>\n'
    )


def write_label(url: str, device_id: str, out_dir: Path, callsign: str | None = None) -> list[Path]:
    """QR label as SVG (print) and PNG (screen), the callsign printed under the code. The files contain the pairing
    code: keep them private."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = label_raster(url, callsign)
    svg, png = out_dir / f"{device_id}.svg", out_dir / f"{device_id}.png"
    svg.write_text(_svg(rows))
    png.write_bytes(_png(rows))
    return [svg, png]


async def set_new_code(store: Store, device_id: str) -> str | None:
    """New pairing code for a registered device; ends its sessions. None if unknown."""
    code = pairing.new_code()
    code_hash, salt = await asyncio.to_thread(pairing.make_hash, code)
    return code if await store.set_pairing(device_id, code_hash, salt) else None


async def _callsign_taken(store: Store, callsign: str, device_id: str) -> bool:
    return any(d.get("callsign") == callsign and d["id"] != device_id for d in await store.list_devices())


async def provision(
    store: Store, device_id: str, vehicle_type: str, demo: bool, site: str, out_dir: Path, callsign: str | None = None
) -> dict[str, Any]:
    """Register the unit, make its pairing code and label. ValueError: bad or already used callsign.

    The CLI always passes a callsign (``--callsign`` is required); None only keeps older callers and tests working.
    """
    if callsign is not None:
        callsign = validate_callsign(callsign)
        if await _callsign_taken(store, callsign, device_id):
            raise ValueError(f"callsign {callsign} already belongs to another unit")
    await store.register_device(device_id, vehicle_type, demo=demo, callsign=callsign)
    code = await set_new_code(store, device_id)
    assert code is not None
    url = pairing_url(site, device_id, code)
    return {
        "device_id": device_id,
        "callsign": callsign,
        "code": code,
        "url": url,
        "label_files": write_label(url, device_id, out_dir, callsign),
    }


def _site(a: argparse.Namespace) -> str:
    site = a.site or os.environ.get("AEROLINK_SITE", "")
    if not site:
        raise SystemExit("error: give --site https://<host> or set AEROLINK_SITE (it goes into the QR code)")
    return site


async def _main(a: argparse.Namespace) -> None:
    store = Store.connect(Settings.from_env().database_url)
    try:
        if a.cmd == "add":
            try:
                r = await provision(store, a.device_id, a.vehicle_type, a.demo, _site(a), a.out_dir, a.callsign)
            except ValueError as e:
                raise SystemExit(f"error: {e}") from e
            print(f"registered {a.device_id} ({r['callsign']}) as {a.vehicle_type}{' (demo)' if a.demo else ''}")
            _print_result(r, a.device_id)
        elif a.cmd == "rotate":
            code = await set_new_code(store, a.device_id)
            if code is None:
                raise SystemExit(f"error: {a.device_id} is not registered")
            url = pairing_url(_site(a), a.device_id, code)
            callsign = ((await store.get_device(a.device_id)) or {}).get("callsign")
            print(f"new pairing code for {a.device_id}; every paired screen was signed out")
            _print_result(
                {"code": code, "url": url, "label_files": write_label(url, a.device_id, a.out_dir, callsign)},
                a.device_id,
            )
        elif a.cmd == "set-callsign":
            try:
                callsign = validate_callsign(a.callsign)
                if await _callsign_taken(store, callsign, a.device_id):
                    raise ValueError(f"callsign {callsign} already belongs to another unit")
            except ValueError as e:
                raise SystemExit(f"error: {e}") from e
            if not await store.set_callsign(a.device_id, callsign):
                raise SystemExit(f"error: {a.device_id} is not registered")
            print(f"{a.device_id} is now {callsign} (re-print its label with `rotate`)")
        else:
            for d in await store.list_devices():
                print(
                    f"{d['id']:<16} {d.get('callsign') or '-':<10} {d['vehicle_type']:<10} "
                    f"demo={d['demo']!s:<5} online={d['online']}"
                )
    finally:
        await store.close()


def _print_result(r: dict[str, Any], device_id: str) -> None:
    print(f"pairing code (shown once): {r['code']}")
    print(f"pairing link: {r['url']}")
    print("QR label: " + ", ".join(str(p) for p in r["label_files"]) + "  (contains the code; keep private)")
    print(f"MQTT login:   mosquitto/add-device.sh {device_id}   # then: docker compose kill -s SIGHUP mosquitto")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="register a unit and print its pairing label")
    add.add_argument("device_id")
    add.add_argument("vehicle_type", choices=["passenger", "medical"])
    add.add_argument("--callsign", required=True, help="radio callsign, 2-10 characters A-Z 0-9 -, e.g. N123AL")
    add.add_argument("--demo", action="store_true", help="allow the demo button to change the type")
    rot = sub.add_parser("rotate", help="new pairing code; ends all screen sessions")
    rot.add_argument("device_id")
    for p in (add, rot):
        p.add_argument("--site", help="public site, e.g. https://aerolink.example (default: $AEROLINK_SITE)")
        p.add_argument("--out-dir", type=Path, default=DEFAULT_LABEL_DIR, help="where to write the QR label")
    cs = sub.add_parser("set-callsign", help="change a unit's callsign")
    cs.add_argument("device_id")
    cs.add_argument("callsign")
    sub.add_parser("list")
    asyncio.run(_main(ap.parse_args(argv)))


if __name__ == "__main__":
    main()
