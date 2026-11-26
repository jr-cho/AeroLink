# E5.1 Real-Data Pipeline: Spec

Source: `docs/Carla/M5_GUIDE.md` Part 5, `docs/PLAN.md` E5.1, ARCHITECTURE §10.

## Goal

A reproducible dataset of real low-altitude flights (helicopters and light aircraft) over the Central Florida focus region, built from adsb.lol daily releases. The forecasters are benchmarked on it (E5.2), the trained model learns from it (E5.3), and off-route detection uses it (E5.5). The simulator can also replay it as background traffic.

## Status

Implemented in commit `087ebd0` on `e5.1-real-data`: all stories are ticked in PLAN.md, and 14 days (2026-11-09 to 2026-11-22) have been processed.

## Scope

| In | Out |
|---|---|
| Streaming daily GitHub releases (`.tar.aa`, `.tar.ab`, ...) without storing raw data | Storing or redistributing raw dumps |
| Parsing readsb trace JSON (gzip or plain) | Live ADS-B ingestion (M6) |
| Region, aircraft and point filters, with every rejection counted | Training or evaluating models (E5.2+) |
| Splitting into segments and resampling to 1 Hz | |
| One Parquet file and one JSON report per day, plus the ODbL attribution | |
| Replay as simulator `BackgroundAircraft` | |

## Modules (`backend/src/aerolink/realdata/`)

| Module | Responsibility | Public surface |
|---|---|---|
| `config.py` | Every filter rule in one place | `FOCUS_LAT`, `FOCUS_LON`, `FilterConfig` (frozen dataclass, `in_region(lat, lon)`) |
| `parse.py` | Decode and parse one trace file | `decode(bytes) -> dict`, `parse_trace(dict) -> Trace`, `Point`, `Trace` (`.military`) |
| `segments.py` | Filters, run splitting, resampling | `COLUMNS`, `Segment` (`segment_id`, `duration_s`), `aircraft_rejection`, `point_rejection`, `resample`, `extract_segments(trace, cfg, counts)` |
| `source.py` | Find a release and stream its parts | `INSTANCES`, `tag_for`, `release_assets`, `find_release(day)`, `ConcatStream`, `open_parts(urls)`, `iter_trace_files(stream)` |
| `fetch.py` | CLI, Parquet writer, reports | `process_stream`, `to_table`, `write_day`, `summarize`, `run_day`, `main`, `ATTRIBUTION` |
| `replay.py` | Map-match segments onto the corridor network | `match_segment(network, t, lat, lon, radius_m=1500)`, `load_replay(path, network, scenario_start, duration_s, limit=0)` |

## Filter rules (`FilterConfig` defaults)

- Region: lat 27.3–28.8, lon −82.8 to −81.1.
- Barometric altitude: 100–3,000 ft. Points on the ground are dropped.
- Ground speed: 40–160 kt.
- Emitter category: A1 or A7 is kept, and so is an unknown category. Any other category drops the aircraft.
- Position source: ADS-B or ADS-R only. MLAT, TIS-B and unknown sources are dropped.
- Military (`dbFlags & 1`) is dropped, and so is any airline callsign (`^[A-Z]{3}[0-9]`).
- Tracks are split at the new-leg flag (bit 2) and at gaps over 30 s. Segments shorter than 60 s are dropped.
- Resampling: 1 Hz linear interpolation, with track angles unwrapped and duplicate timestamps removed.

Rules are applied cheapest first (region, then aircraft, then point, then segment). Every rejection increments a named counter (`aircraft_dropped_*`, `points_dropped_*`, `segments_dropped_too_short`).

## Outputs

- `backend/data/adsblol/segments-YYYY-MM-DD.parquet`: one row per second per segment. Columns are `day, segment_id, icao, registration, type_code, category, callsign` (string), `t` (int64, unix s), `lat, lon` (float64, rounded to 6 decimals, about 5 cm), and `alt_ft` (1 ft), `gs_kt`, `track_deg` (0.1) and `vrate_fpm` (1 fpm), all float32. Files are zstd level 9, after guide step 5h. The schema metadata `license` holds the ODbL attribution.
- `backend/data/adsblol/report-YYYY-MM-DD.json`: `{day, source, attribution, counts}`.
- `backend/data/adsblol/README.md`: ODbL attribution and column docs.
- `backend/eval-results/realdata-e5.1.json`: combined funnel across all days.

## CLI

```
uv run python -m aerolink.realdata.fetch --days 14 [--end YYYY.MM.DD] [--out DIR] [--summary-file F]
uv run python -m aerolink.realdata.fetch --summary
uv run python -m aerolink.realdata.fetch --from-tar day.tar --end YYYY.MM.DD     # offline test
```

- By default the last day is today minus 3 days. Days that already have a report are skipped, so the command can be restarted.
- It uses `GITHUB_TOKEN`/`GH_TOKEN` for the API when set, retries anonymously on 401/403, and never sends a token to the asset download host.

## Simulator integration

- `BackgroundTrafficConfig.replay_file: str = ""` in `sim/world_config.py`.
- `sim/runner.py`: when `world.background_traffic` is on and `replay_file` is set, the runner loads real flights with `load_replay`, with `count > 0` as the cap, instead of `generate_tracks`.
- Replayed aircraft get the ID `rd-<icao>-NNN` and keep their local time of day relative to the scenario start.

## Acceptance criteria

1. One command reproduces the dataset.
2. The sample count after filtering is reported (funnel printout and `realdata-e5.1.json`), and the guide's targets are met: at least 100 aircraft kept and at least 50 flight hours.
3. `tests/test_realdata.py` passes (11 tests, no network access).
4. The cleaned data and attribution are committed. No single file is over GitHub's 100 MB limit (see the open issue in PLAN.md).
5. `ruff check`, `ruff format --check` and `ty check` are clean.
