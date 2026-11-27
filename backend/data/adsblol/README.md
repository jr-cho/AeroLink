# Real low-altitude flights over Central Florida (adsb.lol)

Contains data from [adsb.lol](https://www.adsb.lol/docs/open-data/historical/), licensed under the
[Open Database License (ODbL 1.0)](https://opendatacommons.org/licenses/odbl/1-0/).

This folder is a **derived database**: helicopter and light-aircraft flight segments over the
Central Florida focus region (lat 27.3–28.8, lon −82.8 to −81.1), 100–3,000 ft, 40–160 kt,
ADS-B positions only, military and airline traffic removed, resampled to 1 Hz.
As required by the ODbL, it is released under the same licence (share-alike).

**Identifiers are pseudonymized.** The files carry no registrations. `icao` and `callsign` are stable pseudonyms
(`AC0001…`, `CS0001…`), the same for an aircraft in every file of this folder, and `segment_id` (which starts with the
ICAO address) is rewritten the same way. The mapping from pseudonym to real identifier is not kept anywhere, so the
files cannot be traced back to an aircraft or an owner from this repository. The rewrite preserves the order of
segment ids, so the benchmark results are unchanged (checked: `backend/eval-results/realdata-e5.2.json`).

- `segments-YYYY-MM-DD.parquet`: one row per second per segment. Columns: `day`, `segment_id`,
  `icao` (pseudonym), `type_code`, `category`, `callsign` (pseudonym), `t` (unix s), `lat`, `lon`,
  `alt_ft`, `gs_kt`, `track_deg`, `vrate_fpm`.
- `report-YYYY-MM-DD.json`: how many aircraft and points each filter removed that day.

Rebuild from the source with (from `backend/`):

    uv run python -m aerolink.realdata.fetch --days 14

`fetch` writes new days already pseudonymized. Each day file gets its own random key (never stored), so an aircraft's
pseudonym differs from one day to the next and cannot be linked across days or to the public registry.

To pseudonymize a whole folder of files that still carry real identifiers (for example an older copy), run once
over the complete folder (from `backend/`):

    uv run python -m aerolink.realdata.pseudonymize --data data/adsblol

It rewrites every `segments-*.parquet` in the folder and refuses files that are already pseudonymized, so it cannot
mix two mappings. The mapping is held in memory only and discarded.

Filters: `aerolink.realdata.config.FilterConfig` (ARCHITECTURE §10). The raw daily dumps are
not stored here; the command above streams them from adsb.lol's GitHub releases.
