#!/usr/bin/env bash
# Self-hosted basemap (ARCHITECTURE §13.3, PLAN E6.5). Run once from anywhere:
#   scripts/basemap.sh
# Produces (git-ignored) under basemap/:
#   florida.pmtiles          Protomaps/OpenStreetMap extract, zoom 0-14 (MAXZOOM=15 for ~905 MB), served at /basemap/florida.pmtiles
#   fonts/<stack>/<range>.pbf  glyphs (Noto Sans Regular/Medium/Italic) -> /basemap/fonts/{fontstack}/{range}.pbf
#   sprites/v4/light[@2x].{json,png}                                   -> /basemap/sprites/v4/light
# Bounding box: -83.9,24.3,-79.8,31.0 (min_lon,min_lat,max_lon,max_lat) = the Florida peninsula
# from the Keys to the Georgia line, west to Cedar Key / Tampa Bay; the panhandle is left out.
# Idempotent: existing outputs are kept (FORCE=1 rebuilds). Needs: pmtiles, curl, tar.
# Nothing here is fetched at runtime; the web app only ever talks to its own origin.
set -euo pipefail

BUILD_DATE="20261124"                                         # pinned daily build (HEAD-checked below)
ASSETS_COMMIT="028c18f713baecad011301ff7a69acc39bcc2ae7"      # protomaps/basemaps-assets
BBOX="-83.9,24.3,-79.8,31.0"
MAXZOOM="${MAXZOOM:-14}"   # 14 = ~475 MB; 15 = ~905 MB (MapLibre overzooms past the max)
SRC="https://build.protomaps.com/${BUILD_DATE}.pmtiles"

OUT="$(cd "$(dirname "$0")/.." && pwd)/basemap"
mkdir -p "$OUT"
command -v pmtiles >/dev/null || { echo "pmtiles not found (brew install pmtiles)" >&2; exit 1; }

if [[ -f "$OUT/florida.pmtiles" && "${FORCE:-0}" != 1 ]]; then
  echo "keep $OUT/florida.pmtiles"
else
  curl -fsI "$SRC" >/dev/null || { echo "pinned build $SRC does not exist; pick another date" >&2; exit 1; }
  # Extract to a temp name so an interrupted run never leaves a truncated florida.pmtiles.
  # The remote read can drop mid-transfer (HTTP/2 stream errors), so retry a few times.
  for attempt in 1 2 3 4; do
    rm -f "$OUT/florida.pmtiles.part"
    pmtiles extract "$SRC" "$OUT/florida.pmtiles.part" --bbox="$BBOX" --maxzoom="$MAXZOOM" && break
    [[ $attempt == 4 ]] && { echo "extract failed 4 times" >&2; exit 1; }
    echo "extract failed (attempt $attempt), retrying" >&2
  done
  mv "$OUT/florida.pmtiles.part" "$OUT/florida.pmtiles"
fi

if [[ -f "$OUT/sprites/v4/light.json" && -d "$OUT/fonts/Noto Sans Regular" && "${FORCE:-0}" != 1 ]]; then
  echo "keep fonts and sprites"
else
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  curl -fsSL "https://codeload.github.com/protomaps/basemaps-assets/tar.gz/${ASSETS_COMMIT}" -o "$TMP/assets.tgz"
  tar -xzf "$TMP/assets.tgz" -C "$TMP"
  A="$TMP/basemaps-assets-${ASSETS_COMMIT}"
  mkdir -p "$OUT/fonts" "$OUT/sprites/v4"
  for f in "Noto Sans Regular" "Noto Sans Medium" "Noto Sans Italic"; do
    rm -rf "$OUT/fonts/$f"; cp -R "$A/fonts/$f" "$OUT/fonts/$f"
  done
  cp "$A/fonts/OFL.txt" "$OUT/fonts/OFL.txt"
  cp "$A"/sprites/v4/light*.{json,png} "$OUT/sprites/v4/"
fi

pmtiles show "$OUT/florida.pmtiles" | head -12
du -sh "$OUT/florida.pmtiles" "$OUT/fonts" "$OUT/sprites"
