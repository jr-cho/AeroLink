# Real low-altitude flights over Central Florida (adsb.lol)

Contains data from [adsb.lol](https://www.adsb.lol/docs/open-data/historical/), licensed under the
[Open Database License (ODbL 1.0)](https://opendatacommons.org/licenses/odbl/1-0/).

This folder is a **derived database**: helicopter and light-aircraft flight segments over the
Central Florida focus region (lat 27.3–28.8, lon −82.8 to −81.1), 100–3,000 ft, 40–160 kt,
ADS-B positions only, military and airline traffic removed, resampled to 1 Hz.
As required by the ODbL, it is released under the same licence (share-alike).

- `segments-YYYY-MM-DD.parquet`: one row per second per segment. Columns: `day`, `segment_id`,
  `icao`, `registration`, `type_code`, `category`, `callsign`, `t` (unix s), `lat`, `lon`,
  `alt_ft`, `gs_kt`, `track_deg`, `vrate_fpm`.
- `report-YYYY-MM-DD.json`: how many aircraft and points each filter removed that day.

Rebuild from the source with (from `backend/`):

    uv run python -m aerolink.realdata.fetch --days 14

Filters: `aerolink.realdata.config.FilterConfig` (ARCHITECTURE §10). The raw daily dumps are
not stored here; the command above streams them from adsb.lol's GitHub releases.