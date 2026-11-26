# M5: Real Data and Trained Models — The Complete Step-by-Step Guide

This one guide takes you through **all of Milestone 5** of AeroLink, from an empty Codespace to a pull request with every M5 story done:

| Epic | What you build | Part |
|---|---|---|
| **E5.1** Real-Data Pipeline | Download real helicopter and light-plane flights over Central Florida from adsb.lol and clean them | [Part 5](#part-5-e51-real-data-pipeline) |
| **E5.2** Real-Data Benchmark | Measure how well the forecasters (`cv`, `chronos`) predict those real flights | [Part 6](#part-6-e52-real-data-benchmark) |
| **E5.3** Trained Forecaster (`aerolink-gbm`) | Train AeroLink's own model on simulated + real flights, with calibrated uncertainty | [Part 7](#part-7-e53-trained-forecaster-aerolink-gbm) |
| **E5.4** Demand Forecaster | Predict flights before anyone plans them (synthetic demand data) | [Part 8](#part-8-e54-demand-forecaster) |
| **E5.5** Off-Route Detection | Spot aircraft that leave their predicted path, better than a fixed 300 m rule | [Part 9](#part-9-e55-off-route-detection) |
| **E5.6** Route Summary (LLM) | A two-sentence summary from an AI model, with every number checked | [Part 10](#part-10-e56-route-summary-llm) |
| **E5.7** Full Ablations | Measure what each AI piece adds, and publish the table | [Part 11](#part-11-e57-full-ablations-and-docs) |

**M5 is done when** (from `docs/PLAN.md`): *the full ablation table and the real-data results are committed and reproducible.* At the end of this guide both are.

## Where you are now (revised 2026-11-25)

This guide was revised after E5.1 was finished, so you don't repeat anything. Read this box first.

| Part | Status | What to do |
|---|---|---|
| 1–2 Codespace and setup | done | Skip in your existing Codespace. In a new one, do Part 2 only (`cd backend`, `pip install uv`, `uv sync`). |
| 3 Branch | done | Your branch is **`e5.1-real-data`** (not `m5-real-data-and-models`). Don't create a new one. |
| 4 Libraries | done | `pyarrow` was committed with E5.1. `mapie` is added but **not committed yet**: it goes into the E5.3 commit (7f). |
| 5 E5.1 | done, except **5h** | Code and 14 days of data are committed (`087ebd0`). Do only **[5h. Shrink the data, then push for the first time](#5h-shrink-the-data-then-push-for-the-first-time)**. |
| 6–11 E5.2–E5.7 | to do | Follow as written. Small notes were added where something changed. |
| 12 Final checks | to do | Follow as written. |

**Pushing:** your branch has never been pushed, and it can't be until 5h is done (one data file is over GitHub's 100 MB limit). Until then, **leave out the `git push` line** in every commit step. 5h pushes everything at once, and after that plain `git push` works.

**Decisions and reading:** everything that needs Joshua's OK is listed in [`docs/Carla/plans/SIGNOFF.md`](plans/SIGNOFF.md). Each epic also has a `SPEC.md` (what gets built) and a `PLAN.md` (steps and risks) under [`docs/Carla/plans/`](plans/).

**Already checked for you:** the existing code that the new files call into (`sim.demand`, `Network.nodes_for` / `oriented_geometry` / `route_distance_m`, `A1Advisor` and `A1RouteOption`, `Controller.background_aircraft`, `eval.harness.make_advisor`), the helipad and pad data in the network, the route-response example the E5.6 test uses, and the docs edits in 11d. All are present and match the guide, so no extra checks are needed.

---

Everything happens in your web browser in a **GitHub Codespace**: a computer in the cloud that GitHub lends you, with the project already on it. Nothing gets installed on your own computer.

**Time:** about 4–6 hours of hands-on steps in total, plus waiting for downloads and training (the 14-day download alone can take an hour or more; you can leave it running). You don't have to do it in one sitting: every epic ends with a commit and a push, so your work is saved on GitHub after each one.

## How to use this guide

- Do the parts **in order**. Later epics use code from earlier ones.
- Each step says **what it does** and **why**.
- Grey boxes are either **commands** (you paste them into the terminal and press Enter) or **file contents** (you paste them into a file). Each box says which.
- To copy a box: hover over it and click the copy icon in its top-right corner, or select all its text with the mouse.
- **Pasting into the terminal:** Ctrl + Shift + V, or right-click → Paste. The browser may ask for clipboard permission the first time; allow it.
- **Pasting into a file:** Ctrl + V. **Saving a file:** Ctrl + S. (On a Mac, use Cmd instead of Ctrl.)
- Commands that span several lines are pasted all at once.
- Every number you see in an "example output" box is an example. Your numbers will differ; what matters is the shape (which lines appear, `passed`, and so on).
- If anything looks different from what's described, stop and ask Joshua rather than guessing.

### How every new file is created

You'll create about 40 files by pasting. For **every** "new file" box in this guide, do the same four things:

1. Paste the **Open the file** command into the terminal and press Enter. A new, empty tab opens in the editor. (`code` is the editor's command for "open this file, creating it if needed".)
2. Click inside that empty tab, then paste the **File contents** box with **Ctrl + V**.
3. Press **Ctrl + S** to save. The dot on the tab's name disappears when it's saved.
4. Click back in the terminal for the next file.

For files you **replace** (they already exist), step 2 is different: click inside the file, press **Ctrl + A** (select everything), **Delete**, then paste. Replacing a whole file avoids indentation mistakes.

Don't worry about spacing at the very end of a file; a formatting command tidies that automatically.

---

## Part 1: Open a Codespace

> **Revised:** already done. Skip this part if you're in your existing Codespace.

1. **Give the Codespace more time before it sleeps.** Go to [github.com/settings/codespaces](https://github.com/settings/codespaces), find **Default idle timeout**, set it to **240** minutes, and click **Save**.
   - *Why:* by default a Codespace switches itself off after 30 quiet minutes. Downloads and training can run longer than that while you're not typing. The setting only applies to Codespaces created after you change it, so do this first.
2. **Open the AeroLink repository** on github.com ([github.com/jr-cho/AeroLink](https://github.com/jr-cho/AeroLink)).
3. Click the green **Code** button, then the **Codespaces** tab.
4. Click the **...** (three dots) next to the **+**, then **New with options...**
5. Leave **Branch** as `main`. Set **Machine type** to **4-core** (8-core if it's offered and you want training to go faster). Click **Create codespace**.
   - *Why:* the data pipeline reads tens of thousands of files per day and the training uses every core. More cores and memory make it faster and avoid running out of memory.
6. Wait 1 to 2 minutes. A code editor (VS Code) opens in your browser tab, with the project's files listed on the left. That list is called the **Explorer**.
7. **Open a terminal:** press **Ctrl + `** (the key above Tab), or use the menu **☰ → Terminal → New Terminal**. A panel opens at the bottom with a line ending in `$`.
   - *Why:* the terminal is where you type commands for the cloud computer.

---

## Part 2: Set up the project

All of AeroLink's Python code lives in the `backend` folder, and a tool called **uv** installs and runs it.

1. **Go into the backend folder.** Command:

```bash
cd backend
```

   *What it does:* `cd` means "change directory". Commands now run inside `backend/`, and the prompt shows `backend`. **Stay in this folder for the whole guide** unless a step says otherwise. If you ever open a new terminal, run `cd backend` first.

2. **Install uv.** Command:

```bash
pip install uv
```

   *What it does:* installs uv on the cloud computer. Takes about 10 seconds.

3. **Install AeroLink and everything it needs.** Command:

```bash
uv sync
```

   *What it does:* reads the project's list of libraries (`pyproject.toml` and `uv.lock`) and installs the exact versions. It includes machine-learning libraries, so expect **3 to 10 minutes** the first time. Lots of text scrolls past; that's normal.

4. **Check that it works.** Command:

```bash
uv run pytest -q -n 0 tests/test_smoke.py
```

   *What it does:* runs one tiny test. `uv run` means "run this with the project's libraries". You should see **`1 passed`**.

---

## Part 3: Make your own branch

> **Revised:** already done. Your branch is `e5.1-real-data`. **Don't run the `git checkout -b` command below.** Just make sure you're on it:
>
> ```bash
> git checkout e5.1-real-data
> ```
>
> Whether to rename it for the final pull request is decision D3 in `docs/Carla/plans/SIGNOFF.md`.

A **branch** is your own copy of the project to work in. Your changes stay separate from `main` until Joshua reviews and merges them, so nothing you try can break the main project. You'll use **one branch for all of M5** and save (commit) after each epic.

Command:

```bash
git checkout -b m5-real-data-and-models
```

*What it does:* creates a branch named `m5-real-data-and-models` and switches to it. You'll see `Switched to a new branch 'm5-real-data-and-models'`. The bottom-left corner of the editor now shows the branch name.

**Set your name for commits** (only needed once per Codespace; use your own name and the email on your GitHub account). Command:

```bash
git config user.name "Your Name"
git config user.email "you@example.com"
```

---

## Part 4: Add the new libraries

> **Revised:** already done. `pyarrow` was committed with E5.1, and `mapie` is now in `pyproject.toml` and `uv.lock` but not committed. **Don't run `uv add` again.** The two files are added to the E5.3 commit in 7f.

Command:

```bash
uv add pyarrow mapie
```

*What it does:* installs two libraries and records them in `pyproject.toml` and `uv.lock`, so everyone else on the project gets them too:

- **pyarrow** writes and reads **Parquet** files (a compact table format, like a spreadsheet built for millions of rows).
- **mapie** calibrates the trained model's uncertainty so "80% likely" really means 80% (E5.3).

The other libraries M5 needs (LightGBM, scikit-learn, Chronos-2, PyTorch) are already installed.

---

## Part 5: E5.1 Real-Data Pipeline

**What you're building:** a small program that downloads real helicopter and light-plane flights over Central Florida from [adsb.lol](https://www.adsb.lol/docs/open-data/historical/), keeps only the useful ones, and saves them as clean, second-by-second flight paths.

**Why it matters:** AeroLink predicts air traffic. To prove the predictions work on real aircraft, not only simulated ones, it needs real low-altitude flights to test and train on. Helicopters and small planes flying under 3,000 ft over Tampa, Lakeland and Orlando are the closest thing to future air taxis that exists today.

**Where the data comes from:** every day adsb.lol publishes every aircraft position it received worldwide, free and openly licensed (ODbL). One day is several GB. The program reads each day as a stream, keeps the flights you need, and never saves the big raw file.

> **Revised: E5.1 is done.** 5a–5g are complete (commit `087ebd0`: the code plus 14 days, 2026-11-09 to 11-22). **Skip straight to [5h](#5h-shrink-the-data-then-push-for-the-first-time).** 5a–5g stay below for reference.
>
> What the download produced: 8,215 aircraft, 36,593 segments, **4,906 flight hours**. That's about 100 times the guide's estimate, but it's real traffic: 91% of segments are A1 light aircraft (Central Florida has a lot of flight-school traffic), and unknown-category aircraft are only 0.1%. No filter change is needed. Because of that volume, the data folder is 792 MB, and one file is 108 MB. 5h fixes that.

### 5a. Create the folders

Command:

```bash
mkdir -p src/aerolink/realdata data/adsblol
```

*What it does:* `mkdir -p` creates folders (and any missing parent folders). `realdata` will hold the new code; `data/adsblol` will hold the cleaned flights.

### 5b. Create the 9 new files

#### New file: `backend/src/aerolink/realdata/__init__.py`

*What this file does:* Marks the folder as a Python package and states the data licence.

Open the file (command):

```bash
code src/aerolink/realdata/__init__.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Real low-altitude flight data from adsb.lol (PLAN E5.1, ARCHITECTURE §10).

Contains data from adsb.lol, licensed under the Open Database License (ODbL 1.0).
Raw data is never committed; `python -m aerolink.realdata.fetch` reproduces it.
"""
```

#### New file: `backend/src/aerolink/realdata/config.py`

*What this file does:* Every filter rule in one place: the Central Florida box, 100 to 3,000 ft, 40 to 160 knots, helicopters (A7) and light planes (A1) only, ADS-B positions only (no MLAT), no military, no airline callsigns, split at gaps over 30 s, drop pieces under 60 s.

Open the file (command):

```bash
code src/aerolink/realdata/config.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Filter settings for the real-data pipeline (ARCHITECTURE §10.2).

One place for every rule, so the counts in the report can be traced back to a setting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Central Florida focus region (ARCHITECTURE §3.1), same box as aerolink.airspace.clip.
FOCUS_LAT = (27.3, 28.8)
FOCUS_LON = (-82.8, -81.1)


@dataclass(frozen=True)
class FilterConfig:
    lat: tuple[float, float] = FOCUS_LAT
    lon: tuple[float, float] = FOCUS_LON
    # Barometric altitude, ft. Top = top of FDOT's eVTOL band; bottom drops taxiing/hover-taxi.
    altitude_ft: tuple[float, float] = (100.0, 3000.0)
    ground_speed_kt: tuple[float, float] = (40.0, 160.0)
    # Emitter categories kept: A1 light (< 15,500 lb), A7 rotorcraft. Unknown category is kept
    # unless another rule drops it (many traces never send a category).
    keep_categories: frozenset[str] = frozenset({"A1", "A7"})
    keep_unknown_category: bool = True
    # Position sources kept (trace point index 9). MLAT, TIS-B and unknown sources are dropped.
    keep_sources: frozenset[str] = frozenset({"adsb_icao", "adsb_icao_nt", "adsr_icao", "adsb_other", "adsr_other"})
    drop_military: bool = True  # dbFlags & 1
    # Airline-style callsign: three letters then a digit (AAL123, SWA45, JBU9).
    airline_callsign: re.Pattern[str] = field(default_factory=lambda: re.compile(r"^[A-Z]{3}[0-9]"))
    max_gap_s: float = 30.0  # split a track at larger gaps
    min_segment_s: float = 60.0  # drop shorter segments: too short to forecast from
    resample_s: float = 1.0  # 1 Hz output

    def in_region(self, lat: float, lon: float) -> bool:
        return self.lat[0] <= lat <= self.lat[1] and self.lon[0] <= lon <= self.lon[1]
```

#### New file: `backend/src/aerolink/realdata/parse.py`

*What this file does:* Opens one aircraft's daily file (usually gzip-compressed even though it ends in `.json`) and turns it into a list of positions: time, latitude, longitude, altitude, speed, heading, and where the position came from.

Open the file (command):

```bash
code src/aerolink/realdata/parse.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Read readsb trace files (one aircraft, one day) from adsb.lol globe_history.

Format: readsb README-json, "trace" section. Top level: icao, r (registration),
t (type designator), desc, dbFlags, timestamp, trace. Each trace point:

    [0] seconds after timestamp   [1] lat   [2] lon
    [3] altitude ft | "ground" | null      [4] ground speed kt | null
    [5] track deg | null          [6] flags (1 stale, 2 new leg, 4 geom vrate, 8 geom alt)
    [7] vertical rate fpm | null  [8] details object | null (flight, category, ...)
    [9] position source ("adsb_icao", "mlat", ...) | null   (files from 2022 on)
    [10] geometric altitude ...   (later fields unused)
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass

FLAG_NEW_LEG = 2


@dataclass(frozen=True)
class Point:
    t: float  # unix seconds
    lat: float
    lon: float
    alt_ft: float | None  # barometric; None when on the ground or unknown
    on_ground: bool
    gs_kt: float | None
    track_deg: float | None
    vrate_fpm: float | None
    new_leg: bool
    source: str | None


@dataclass(frozen=True)
class Trace:
    icao: str
    registration: str | None
    type_code: str | None
    db_flags: int
    category: str | None  # first category seen in the details objects
    callsign: str | None  # first callsign seen
    points: list[Point]

    @property
    def military(self) -> bool:
        return bool(self.db_flags & 1)


def decode(raw: bytes) -> dict:
    """Trace files end in .json but are usually gzip inside: always try to decompress."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_trace(doc: dict) -> Trace:
    base = float(doc.get("timestamp", 0.0))
    category = callsign = None
    points: list[Point] = []
    for p in doc.get("trace", []):
        if len(p) < 7 or p[1] is None or p[2] is None:
            continue
        details = p[8] if len(p) > 8 and isinstance(p[8], dict) else None
        if details:
            category = category or (details.get("category") or None)
            flight = (details.get("flight") or "").strip()
            callsign = callsign or (flight or None)
        alt = p[3]
        points.append(
            Point(
                t=base + float(p[0]),
                lat=float(p[1]),
                lon=float(p[2]),
                alt_ft=_num(alt),
                on_ground=alt == "ground",
                gs_kt=_num(p[4]),
                track_deg=_num(p[5]),
                vrate_fpm=_num(p[7]) if len(p) > 7 else None,
                new_leg=bool((p[6] or 0) & FLAG_NEW_LEG),
                source=p[9] if len(p) > 9 and isinstance(p[9], str) else None,
            )
        )
    return Trace(
        icao=str(doc.get("icao", "")).lower(),
        registration=doc.get("r"),
        type_code=doc.get("t"),
        db_flags=int(doc.get("dbFlags") or 0),
        category=category,
        callsign=callsign,
        points=points,
    )
```

#### New file: `backend/src/aerolink/realdata/segments.py`

*What this file does:* Applies the filters, cheapest first, and counts every rejection. Cuts each flight at gaps or landings, drops short pieces, and evens the rest out to exactly one position per second.

Open the file (command):

```bash
code src/aerolink/realdata/segments.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Filter a trace and cut it into clean 1 Hz segments (ARCHITECTURE §10.2 steps 3-6).

Order is cheapest first: region, aircraft-level rules, then per point, then segments.
Every rejection is counted so the report shows which rule removed what.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import numpy as np

from aerolink.realdata.config import FilterConfig
from aerolink.realdata.parse import Point, Trace

COLUMNS = ["t", "lat", "lon", "alt_ft", "gs_kt", "track_deg", "vrate_fpm"]


@dataclass(frozen=True)
class Segment:
    icao: str
    index: int  # segment number within this aircraft's day
    registration: str | None
    type_code: str | None
    category: str | None
    callsign: str | None
    data: dict[str, np.ndarray]  # COLUMNS, resampled to 1 Hz

    @property
    def segment_id(self) -> str:
        return f"{self.icao}-{int(self.data['t'][0])}-{self.index}"

    @property
    def duration_s(self) -> float:
        return float(self.data["t"][-1] - self.data["t"][0])


def aircraft_rejection(trace: Trace, cfg: FilterConfig) -> str | None:
    """Reason to drop the whole aircraft, or None to keep it."""
    if not any(cfg.in_region(p.lat, p.lon) for p in trace.points):
        return "outside_region"
    if cfg.drop_military and trace.military:
        return "military"
    if trace.callsign and cfg.airline_callsign.match(trace.callsign.upper()):
        return "airline_callsign"
    if trace.category is None:
        if not cfg.keep_unknown_category:
            return "category_unknown"
    elif trace.category not in cfg.keep_categories:
        return "category_not_light_or_rotor"
    return None


def point_rejection(p: Point, cfg: FilterConfig) -> str | None:
    if not cfg.in_region(p.lat, p.lon):
        return "point_outside_region"
    if p.source not in cfg.keep_sources:
        return "point_source"  # mlat, tisb, unknown
    if p.on_ground or p.alt_ft is None or not (cfg.altitude_ft[0] <= p.alt_ft <= cfg.altitude_ft[1]):
        return "point_altitude"
    if p.gs_kt is None or not (cfg.ground_speed_kt[0] <= p.gs_kt <= cfg.ground_speed_kt[1]):
        return "point_speed"
    return None


def _runs(points: list[Point], cfg: FilterConfig) -> list[list[Point]]:
    """Consecutive kept points, split at gaps and new-leg flags."""
    runs: list[list[Point]] = []
    current: list[Point] = []
    for p in points:
        if current and (p.t - current[-1].t > cfg.max_gap_s or p.new_leg):
            runs.append(current)
            current = []
        current.append(p)
    if current:
        runs.append(current)
    return runs


def resample(run: list[Point], step_s: float) -> dict[str, np.ndarray]:
    """Linear interpolation to a fixed step; track angles are unwrapped first."""
    t = np.array([p.t for p in run])
    keep = np.concatenate([[True], np.diff(t) > 0])  # drop duplicate timestamps
    t = t[keep]
    grid = np.arange(np.ceil(t[0]), np.floor(t[-1]) + 1e-9, step_s)
    out = {"t": grid}

    def interp(values: list[float | None], angle: bool = False) -> np.ndarray:
        v = np.array([np.nan if x is None else x for x in values], dtype=float)[keep]
        ok = ~np.isnan(v)
        if ok.sum() < 2:
            return np.full(grid.shape, np.nan)
        if angle:
            v[ok] = np.degrees(np.unwrap(np.radians(v[ok])))
        r = np.interp(grid, t[ok], v[ok])
        return np.mod(r, 360.0) if angle else r

    out["lat"] = interp([p.lat for p in run])
    out["lon"] = interp([p.lon for p in run])
    out["alt_ft"] = interp([p.alt_ft for p in run])
    out["gs_kt"] = interp([p.gs_kt for p in run])
    out["track_deg"] = interp([p.track_deg for p in run], angle=True)
    out["vrate_fpm"] = interp([p.vrate_fpm for p in run])
    return out


def extract_segments(trace: Trace, cfg: FilterConfig, counts: Counter) -> list[Segment]:
    """All usable segments of one aircraft's day; updates `counts` with what was dropped."""
    counts["aircraft_seen"] += 1
    reason = aircraft_rejection(trace, cfg)
    if reason:
        counts[f"aircraft_dropped_{reason}"] += 1
        return []
    counts["aircraft_candidate"] += 1
    kept: list[Point] = []
    for p in trace.points:
        if not cfg.in_region(p.lat, p.lon):
            continue  # most of a long flight is elsewhere; not worth counting
        counts["points_in_region"] += 1
        why = point_rejection(p, cfg)
        if why:
            counts[f"points_dropped_{why}"] += 1
        else:
            kept.append(p)
    counts["points_kept"] += len(kept)
    segments: list[Segment] = []
    for run in _runs(kept, cfg):
        if run[-1].t - run[0].t < cfg.min_segment_s:
            counts["segments_dropped_too_short"] += 1
            continue
        segments.append(
            Segment(
                icao=trace.icao,
                index=len(segments),
                registration=trace.registration,
                type_code=trace.type_code,
                category=trace.category,
                callsign=trace.callsign,
                data=resample(run, cfg.resample_s),
            )
        )
    if segments:
        counts["aircraft_kept"] += 1
        counts["segments_kept"] += len(segments)
        counts["segment_seconds"] += int(sum(s.duration_s for s in segments))
    return segments
```

#### New file: `backend/src/aerolink/realdata/source.py`

*What this file does:* Finds a day's release on GitHub and reads its parts (`.tar.aa`, `.tar.ab`, ...) one after another as a single stream. Only aircraft files are opened; nothing big is saved to disk.

Open the file (command):

```bash
code src/aerolink/realdata/source.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Stream one day of adsb.lol history straight from GitHub releases.

Each day is a GitHub release split into parts (`.tar.aa`, `.tar.ab`, ...). The parts are
read one after another as a single stream and fed to `tarfile` in streaming mode, so the
raw day (several GB) is never written to disk. Only `traces/` members are looked at.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from datetime import date

INSTANCES = ("readsb-prod-0", "readsb-prod-1", "readsb-staging-0")
API = "https://api.github.com/repos/adsblol/{repo}/releases/tags/{tag}"


def _request(url: str, accept: str, auth: bool) -> urllib.request.Request:
    headers = {"Accept": accept, "User-Agent": "aerolink-realdata"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if auth and token:  # set automatically in GitHub Codespaces; raises the API rate limit
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def tag_for(day: date, instance: str) -> str:
    return f"v{day:%Y.%m.%d}-planes-{instance}"


def release_assets(day: date, instance: str) -> list[str] | None:
    """Download URLs of the day's tar parts in order, or None if that release doesn't exist."""
    for repo in (f"globe_history_{day.year}", "globe_history"):
        url = API.format(repo=repo, tag=tag_for(day, instance))
        release = None
        for auth in (True, False):  # a scoped Codespaces token can be refused: retry anonymously
            try:
                with urllib.request.urlopen(_request(url, "application/vnd.github+json", auth), timeout=60) as r:
                    release = json.load(r)
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    break
                if e.code in (401, 403) and auth:
                    continue
                raise
        if release is None:
            continue
        parts = sorted(
            (a for a in release.get("assets", []) if ".tar" in a["name"]),
            key=lambda a: a["name"],
        )
        if parts:
            return [a["browser_download_url"] for a in parts]  # public downloads, no token needed
    return None


def find_release(day: date) -> tuple[str, list[str]] | None:
    """First instance that published this day (prod first, staging as fallback)."""
    for instance in INSTANCES:
        urls = release_assets(day, instance)
        if urls:
            return instance, urls
    return None


class ConcatStream(io.RawIOBase):
    """Read several byte streams back to back, as one file-like object."""

    def __init__(self, streams: Iterable[io.BufferedIOBase]):
        self._streams: Iterator[io.BufferedIOBase] = iter(streams)
        self._current: io.BufferedIOBase | None = next(self._streams, None)

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:  # type: ignore[override]
        while self._current is not None:
            n = self._current.readinto(b)
            if n:
                return n
            self._current.close()
            self._current = next(self._streams, None)
        return 0


def open_parts(urls: list[str]) -> io.BufferedReader:
    def responses():
        for url in urls:
            # No token here: the download redirects to a storage host that rejects extra auth.
            yield urllib.request.urlopen(_request(url, "application/octet-stream", auth=False), timeout=300)

    return io.BufferedReader(ConcatStream(responses()), buffer_size=1 << 20)


def iter_trace_files(stream: io.BufferedIOBase) -> Iterator[tuple[str, bytes]]:
    """(name, raw bytes) of every traces/ file in a tar stream, without seeking."""
    with tarfile.open(fileobj=stream, mode="r|*") as tar:
        for member in tar:
            if not member.isfile() or "traces/" not in member.name:
                continue
            f = tar.extractfile(member)
            if f is not None:
                yield member.name, f.read()
```

#### New file: `backend/src/aerolink/realdata/fetch.py`

*What this file does:* The command you run. Loops over the days, skips days already done, saves one Parquet file and one report per day, prints the totals, and writes them to `backend/eval-results/realdata-e5.1.json`.

Open the file (command):

```bash
code src/aerolink/realdata/fetch.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Build the real-data set: one Parquet file of clean segments per day (PLAN E5.1).

    uv run python -m aerolink.realdata.fetch --days 14
    uv run python -m aerolink.realdata.fetch --days 1 --end 2026.11.20     # one day
    uv run python -m aerolink.realdata.fetch --summary                      # counts so far

Days already processed are skipped, so an interrupted run can simply be restarted.
Output (the cleaned, filtered data is small and committed, under ODbL; raw data never is):
    backend/data/adsblol/segments-YYYY-MM-DD.parquet
    backend/data/adsblol/report-YYYY-MM-DD.json
The combined counts go to backend/eval-results/realdata-e5.1.json.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from aerolink.realdata.config import FilterConfig
from aerolink.realdata.parse import decode, parse_trace
from aerolink.realdata.segments import COLUMNS, Segment, extract_segments
from aerolink.realdata.source import find_release, iter_trace_files, open_parts

BACKEND = Path(__file__).resolve().parents[3]
DEFAULT_OUT = BACKEND / "data" / "adsblol"
DEFAULT_SUMMARY = BACKEND / "eval-results" / "realdata-e5.1.json"
ATTRIBUTION = "Contains data from adsb.lol, licensed under the Open Database License (ODbL 1.0)."


def process_stream(
    stream: io.BufferedIOBase, cfg: FilterConfig, progress: bool = False
) -> tuple[list[Segment], Counter]:
    counts: Counter = Counter()
    segments: list[Segment] = []
    started = time.monotonic()
    for _, raw in iter_trace_files(stream):
        counts["files_read"] += 1
        try:
            trace = parse_trace(decode(raw))
        except (ValueError, OSError):
            counts["files_unreadable"] += 1
            continue
        segments.extend(extract_segments(trace, cfg, counts))
        if progress and counts["files_read"] % 20000 == 0:
            rate = counts["files_read"] / max(1e-9, time.monotonic() - started)
            print(
                f"  {counts['files_read']:,} aircraft read ({rate:,.0f}/s), "
                f"{counts['aircraft_kept']} kept, {counts['segments_kept']} segments",
                flush=True,
            )
    return segments, counts


def to_table(segments: list[Segment], day: date) -> pa.Table:
    cols: dict[str, list] = {
        k: [] for k in ["day", "segment_id", "icao", "registration", "type_code", "category", "callsign", *COLUMNS]
    }
    for s in segments:
        n = len(s.data["t"])
        for key, value in (
            ("day", day.isoformat()),
            ("segment_id", s.segment_id),
            ("icao", s.icao),
            ("registration", s.registration),
            ("type_code", s.type_code),
            ("category", s.category),
            ("callsign", s.callsign),
        ):
            cols[key].extend([value] * n)
        for c in COLUMNS:
            cols[c].extend(s.data[c].tolist())
    schema = pa.schema(
        [(k, pa.string()) for k in ["day", "segment_id", "icao", "registration", "type_code", "category", "callsign"]]
        + [("t", pa.int64())]
        + [(c, pa.float64()) for c in COLUMNS[1:]]
    )
    cols["t"] = [int(x) for x in cols["t"]]
    return pa.table(cols, schema=schema).replace_schema_metadata({"license": ATTRIBUTION})


def write_day(segments: list[Segment], counts: Counter, day: date, source: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(to_table(segments, day), out / f"segments-{day.isoformat()}.parquet")
    report = {
        "day": day.isoformat(),
        "source": source,
        "attribution": ATTRIBUTION,
        "counts": dict(sorted(counts.items())),
    }
    (out / f"report-{day.isoformat()}.json").write_text(json.dumps(report, indent=2) + "\n")


def summarize(out: Path) -> dict:
    reports = [json.loads(p.read_text()) for p in sorted(out.glob("report-*.json"))]
    total: Counter = Counter()
    for r in reports:
        total.update(r["counts"])
    return {
        "attribution": ATTRIBUTION,
        "filters": "aerolink.realdata.config.FilterConfig defaults",
        "days": [
            {
                "day": r["day"],
                "source": r["source"],
                "aircraft_kept": r["counts"].get("aircraft_kept", 0),
                "segments_kept": r["counts"].get("segments_kept", 0),
            }
            for r in reports
        ],
        "totals": dict(sorted(total.items())),
    }


def print_summary(summary: dict) -> None:
    t = summary["totals"]
    print(f"\nDays processed: {len(summary['days'])}")
    for d in summary["days"]:
        print(f"  {d['day']} ({d['source']}): {d['aircraft_kept']} aircraft, {d['segments_kept']} segments")
    hours = t.get("segment_seconds", 0) / 3600
    print("\nFunnel (all days):")
    for key in [
        "files_read",
        "aircraft_candidate",
        "points_in_region",
        "points_kept",
        "aircraft_kept",
        "segments_kept",
    ]:
        print(f"  {key:<22} {t.get(key, 0):>12,}")
    print(f"  {'flight_hours':<22} {hours:>12,.1f}")
    dropped = {k: v for k, v in t.items() if "dropped" in k}
    if dropped:
        print("\nWhy things were dropped:")
        for k, v in sorted(dropped.items(), key=lambda kv: -kv[1]):
            print(f"  {k:<45} {v:>12,}")


def run_day(day: date, cfg: FilterConfig, out: Path) -> bool:
    if (out / f"report-{day.isoformat()}.json").exists():
        print(f"{day}: already done, skipping")
        return True
    found = find_release(day)
    if found is None:
        print(f"{day}: no release published (yet); skipping")
        return False
    instance, urls = found
    print(f"{day}: streaming {len(urls)} parts from {instance} ...", flush=True)
    started = time.monotonic()
    with open_parts(urls) as stream:
        segments, counts = process_stream(stream, cfg, progress=True)
    write_day(segments, counts, day, instance, out)
    print(
        f"{day}: done in {(time.monotonic() - started) / 60:.1f} min: {counts['aircraft_kept']} aircraft, "
        f"{counts['segments_kept']} segments ({counts['segment_seconds'] / 3600:.1f} flight hours)",
        flush=True,
    )
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=14, help="how many days, counting back from --end")
    ap.add_argument("--end", help="last day, YYYY.MM.DD (default: 3 days ago, so uploads are complete)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--summary-file", type=Path, default=DEFAULT_SUMMARY)
    ap.add_argument("--summary", action="store_true", help="only print and save the combined counts")
    ap.add_argument("--from-tar", type=Path, help="process a local tar instead of downloading (testing)")
    a = ap.parse_args(argv)
    cfg = FilterConfig()

    if a.from_tar:
        day = datetime.strptime(a.end, "%Y.%m.%d").date() if a.end else date.today()
        with a.from_tar.open("rb") as f:
            segments, counts = process_stream(f, cfg)
        write_day(segments, counts, day, f"file:{a.from_tar.name}", a.out)
    elif not a.summary:
        end = datetime.strptime(a.end, "%Y.%m.%d").date() if a.end else date.today() - timedelta(days=3)
        for i in range(a.days):
            run_day(end - timedelta(days=i), cfg, a.out)

    summary = summarize(a.out)
    if not summary["days"]:
        print("No days processed yet.")
        return 1
    print_summary(summary)
    a.summary_file.parent.mkdir(parents=True, exist_ok=True)
    a.summary_file.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"\nwrote {a.summary_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

#### New file: `backend/src/aerolink/realdata/replay.py`

*What this file does:* Lets the AeroLink simulator replay real flights as background traffic. It matches each real flight to the corridor network and keeps the ones that followed a corridor.

Open the file (command):

```bash
code src/aerolink/realdata/replay.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Replay real flights as simulator background traffic (PLAN E5.1, E3.3 T6).

The simulator's background aircraft fly along network nodes (`BackgroundAircraft`:
a node path plus the time at each node). A real 1 Hz segment is turned into one by
map matching: for every second, the nearest network node within `radius_m`; the
consecutive node visits that are joined by a corridor edge become the path, with the
time of closest approach at each node. Flights that never follow a corridor between
two nodes are skipped: they can't meet AeroLink traffic in the traffic index anyway.

Real flights keep their time of day: a segment flown 08:10-08:25 local appears in a
scenario starting at 08:00 local from t = 600 s.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from aerolink.network import Network
from aerolink.sim.background import BackgroundAircraft

FT_TO_M = 0.3048
KT_TO_MPS = 0.514444


def _node_xy(network: Network) -> tuple[list[str], np.ndarray]:
    ids = sorted(network.nodes)
    lat0 = math.radians(28.0)
    xy = np.array(
        [
            [
                math.radians(network.nodes[n]["lon"]) * 6371000 * math.cos(lat0),
                math.radians(network.nodes[n]["lat"]) * 6371000,
            ]
            for n in ids
        ]
    )
    return ids, xy


def match_segment(
    network: Network,
    t: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    radius_m: float = 1500.0,
) -> tuple[list[str], list[float]]:
    """Longest run of corridor edges the track follows: (node path, unix time at each node)."""
    ids, xy = _node_xy(network)
    lat0 = math.radians(28.0)
    pts = np.column_stack([np.radians(lon) * 6371000 * math.cos(lat0), np.radians(lat) * 6371000])
    d = np.linalg.norm(pts[:, None, :] - xy[None, :, :], axis=2)
    nearest = d.argmin(axis=1)
    near = d[np.arange(len(pts)), nearest] <= radius_m

    visits: list[tuple[str, float, float]] = []  # (node, time of closest approach, distance)
    for i in np.flatnonzero(near):
        node, dist = ids[nearest[i]], float(d[i, nearest[i]])
        if visits and visits[-1][0] == node:
            if dist < visits[-1][2]:
                visits[-1] = (node, float(t[i]), dist)
        else:
            visits.append((node, float(t[i]), dist))

    best: list[tuple[str, float, float]] = []
    run: list[tuple[str, float, float]] = []
    for v in visits:
        if run and network.graph.has_edge(run[-1][0], v[0]) and v[1] > run[-1][1]:
            run.append(v)
        else:
            run = [v]
        if len(run) > len(best):
            best = list(run)
    if len(best) < 2:
        return [], []
    return [v[0] for v in best], [v[1] for v in best]


def load_replay(
    path: Path,
    network: Network,
    scenario_start: str,
    duration_s: float,
    limit: int = 0,
) -> list[BackgroundAircraft]:
    """Background aircraft from one day's segments Parquet, in the scenario's time window.

    `scenario_start` is the scenario's ISO start time with its UTC offset; only flights
    whose matched path starts and ends inside [0, duration_s] of that time of day are used.
    """
    start = datetime.fromisoformat(scenario_start)
    utc_offset = start.utcoffset()
    offset_s = utc_offset.total_seconds() if utc_offset is not None else 0.0
    start_sod = start.hour * 3600 + start.minute * 60 + start.second

    table = pq.read_table(path, columns=["segment_id", "t", "lat", "lon", "alt_ft", "gs_kt"])
    seg_ids = np.array(table["segment_id"].to_pylist())
    cols = {c: table[c].to_numpy().astype(float) for c in ["t", "lat", "lon", "alt_ft", "gs_kt"]}
    order = np.lexsort((cols["t"], seg_ids))  # by segment, then time
    seg_ids = seg_ids[order]
    cols = {c: v[order] for c, v in cols.items()}
    starts = np.flatnonzero(np.r_[True, seg_ids[1:] != seg_ids[:-1]])
    ends = np.r_[starts[1:], len(seg_ids)]

    out: list[BackgroundAircraft] = []
    for a, b in zip(starts, ends, strict=True):
        nodes, times = match_segment(network, cols["t"][a:b], cols["lat"][a:b], cols["lon"][a:b])
        if not nodes:
            continue
        local = [((tt + offset_s) % 86400) - start_sod for tt in times]
        if local[0] < 0 or local[-1] > duration_s:
            continue
        out.append(
            BackgroundAircraft(
                aircraft_id=f"rd-{str(seg_ids[a]).split('-')[0]}-{len(out):03d}",
                path=nodes,
                times_s=local,
                alt_msl_m=float(np.nanmedian(cols["alt_ft"][a:b])) * FT_TO_M,
                speed_mps=float(np.nanmean(cols["gs_kt"][a:b])) * KT_TO_MPS,
            )
        )
        if limit and len(out) >= limit:
            break
    return out
```

#### New file: `backend/tests/test_realdata.py`

*What this file does:* 11 automatic checks. They build fake flights in adsb.lol's exact file format and prove every rule works, without downloading anything.

Open the file (command):

```bash
code tests/test_realdata.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
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
```

#### New file: `backend/data/adsblol/README.md`

*What this file does:* Credits adsb.lol and the data licence (required by the licence), and explains the saved files.

Open the file (command):

```bash
code data/adsblol/README.md
```

File contents (paste into the empty tab, then Ctrl + S):

```markdown
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
```

### 5c. Replace two simulator files

#### Replace the whole file: `backend/src/aerolink/sim/world_config.py`

*What changes:* The simulator's background-traffic settings get one new option, `replay_file`, so a scenario can use real flights.

1. Open the file (command):

```bash
code src/aerolink/sim/world_config.py
```

2. Click inside the file, press **Ctrl + A** (select everything), then **Delete**.
3. Paste the box below with **Ctrl + V**, then **Ctrl + S**.

File contents:

```python
"""World toggles and operator configuration (PLAN E3.3).

All toggles off gives exactly the E3.2 behaviour (verified by golden digest).
"""

from __future__ import annotations

from dataclasses import dataclass

# Behaviours planned in E3.3 but not implemented yet. Each story removes its
# toggle from this set when it lands.
NOT_BUILT: frozenset[str] = frozenset()


@dataclass(frozen=True)
class WorldToggles:
    """Operator/pilot behaviours that can be switched on or off per scenario.

    The default is all off, which reproduces the E3.2 simulator exactly.
    Toggles for behaviours that are not built yet are rejected, so a scenario
    can never claim a behaviour the simulator does not run.
    """

    separation: bool = False
    energy: bool = False
    pad_booking: bool = False
    arrival_metering: bool = False
    schedules: bool = False
    background_traffic: bool = False

    def __post_init__(self) -> None:
        on = sorted(name for name in NOT_BUILT if getattr(self, name))
        if on:
            raise ValueError(f"world toggles not implemented yet: {on}")
        # metering implies pad_booking; pad_booking implies separation
        if self.arrival_metering and not self.pad_booking:
            raise ValueError("arrival_metering requires pad_booking")
        if self.pad_booking and not self.separation:
            raise ValueError("pad_booking requires separation")


@dataclass(frozen=True)
class SeparationConfig:
    follow_target_distance_m: float = 600.0
    min_separation_m: float = 300.0
    min_vertical_separation_m: float = 150.0
    follow_lookahead_m: float = 2000.0  # leader search distance, across leg boundaries
    follow_time_constant_s: float = 30.0  # gap error closed per second = error / this
    junction_gap_s: float = 20.0
    junction_horizon_s: float = 120.0
    stack_distance_m: float = 400.0  # spacing between aircraft holding for the same pad
    terminal_radius_m: float = 3000.0  # departure release looks for traffic this close to the site
    release_gap_s: float = 60.0  # no departure this soon before a cleared arrival lands
    pad_area_m: float = 300.0  # pad sequencing area: pairs both inside it are not a loss of separation
    spacing_same_edge_m: float = 1000.0
    spacing_junction_s: float = 60.0

    def __post_init__(self) -> None:
        if not 0 < self.min_separation_m <= self.follow_target_distance_m:
            raise ValueError("min_separation_m must be positive and <= follow_target_distance_m")
        if self.min_vertical_separation_m <= 0:
            raise ValueError("min_vertical_separation_m must be positive")
        if self.junction_gap_s <= 0:
            raise ValueError("junction_gap_s must be positive")
        if self.spacing_same_edge_m <= 0 or self.spacing_junction_s <= 0:
            raise ValueError("spacing parameters must be positive")
        for name in (
            "follow_lookahead_m",
            "follow_time_constant_s",
            "junction_horizon_s",
            "stack_distance_m",
            "terminal_radius_m",
            "pad_area_m",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.release_gap_s < 0:
            raise ValueError("release_gap_s must be non-negative")


@dataclass(frozen=True)
class EnergyConfig:
    reserve_soc: float = 0.20
    charge_rate_soc_per_s: float = 0.001  # full charge in ~17 minutes of turnaround
    headwind_penalty_factor: float = 0.5  # extra cruise energy per headwind mps

    def __post_init__(self) -> None:
        if not 0.0 < self.reserve_soc < 1.0:
            raise ValueError("reserve_soc must be in (0, 1)")
        if self.charge_rate_soc_per_s <= 0:
            raise ValueError("charge_rate_soc_per_s must be positive")
        if self.headwind_penalty_factor < 0:
            raise ValueError("headwind_penalty_factor must be non-negative")


@dataclass(frozen=True)
class BackgroundTrafficConfig:
    """Synthetic non-cooperative traffic generator (E3.3 T6)."""

    count: int = 0
    start_time_s: float = 0.0
    duration_s: float = 3600.0
    altitude_msl_m: float = 150.0
    speed_mps: float = 50.0
    speed_sigma: float = 10.0
    seed_offset: int = 12345  # deterministic for benchmarking; change for randomness
    # Real-data replay (PLAN E5.1): a segments Parquet from backend/data/adsblol/. When set,
    # real flights on the corridors replace the synthetic tracks; `count` > 0 caps how many.
    replay_file: str = ""

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("count must be non-negative")
        if self.duration_s <= 0:
            raise ValueError("duration_s must be positive")
        if self.speed_mps <= 0:
            raise ValueError("speed_mps must be positive")
        if self.speed_sigma < 0:
            raise ValueError("speed_sigma must be non-negative")
        if self.altitude_msl_m < 0:
            raise ValueError("altitude_msl_m must be non-negative")


@dataclass(frozen=True)
class PadBookingConfig:
    slot_tolerance_s: float = 120.0
    medical_can_take_passenger_slot: bool = True

    def __post_init__(self) -> None:
        if self.slot_tolerance_s < 0:
            raise ValueError("slot_tolerance_s must be non-negative")
```

#### Replace the whole file: `backend/src/aerolink/sim/runner.py`

*What changes:* When a scenario sets `replay_file`, the simulator loads real flights instead of making up synthetic ones. (You'll replace this file once more in Part 11, with one extra change.)

1. Open the file (command):

```bash
code src/aerolink/sim/runner.py
```

2. Click inside the file, press **Ctrl + A** (select everything), then **Delete**.
3. Paste the box below with **Ctrl + V**, then **Ctrl + S**.

File contents:

```python
"""Fixed-step simulation loop (PLAN E3.2).

``run_simulation(network, scenario, seed)`` is a pure function: the same inputs
always give byte-identical telemetry and events (see ``SimulationResult.digest``).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import numpy as np

from aerolink.contracts import SCHEMA_VERSION
from aerolink.network import Network
from aerolink.sim.background import (
    BackgroundAircraft,
    BackgroundState,
    generate_tracks,
    step_background,
)
from aerolink.sim.clock import SimClock
from aerolink.sim.controller import Controller, ControllerConfig, RouteAdvisor, ShortestPathAdvisor
from aerolink.sim.fleet import create_fleet
from aerolink.sim.invariants import InvariantChecker, Violation
from aerolink.sim.pads import init_pads
from aerolink.sim.scenarios import Scenario
from aerolink.sim.state import WindField, WorldState
from aerolink.sim.telemetry import telemetry_record


@dataclass
class SimulationResult:
    scenario: Scenario
    seed: int
    telemetry: list[dict]  # noisy, contract-shaped Telemetry records
    events: list[dict]  # structured event log
    truth: list[dict]  # noise-free per-step state of every aircraft
    final_world: WorldState
    steps: int = 0
    stats: dict = field(default_factory=dict)
    violations: list[Violation] = field(default_factory=list)  # only with check_invariants=True

    def digest(self, precision: int = 6) -> str:
        """SHA-256 over telemetry + events + truth; equal iff runs are identical.

        Floats are rounded to ``precision`` decimal places so tiny platform
        differences in numpy/libm trig/exp don't break cross-platform tests.
        """
        h = hashlib.sha256()
        for part in (self.telemetry, self.events, self.truth):
            h.update(json.dumps(_round_floats(part, precision), sort_keys=True, separators=(",", ":")).encode())
        return h.hexdigest()


def _round_floats(obj, precision: int):
    """Recursively round floats in dicts/lists for stable digests."""
    if isinstance(obj, float):
        return round(obj, precision)
    if isinstance(obj, dict):
        return {k: _round_floats(v, precision) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, precision) for v in obj]
    return obj


def build_world(network: Network, scenario: Scenario, seed: int) -> WorldState:
    rng = np.random.default_rng(seed)
    pads = init_pads(network, scenario.pad_capacity, scenario.stand_capacity, default_stand_count=True)
    fleet = create_fleet(
        network,
        pads,
        rng,
        passenger=scenario.fleet.get("passenger", 0),
        medical=scenario.fleet.get("medical", 0),
    )
    wind = WindField(
        speed_mps=scenario.wind.speed_mps,
        direction_deg=scenario.wind.direction_deg,
        gust_fraction=scenario.wind.gust_fraction,
    )
    return WorldState(t=0.0, aircraft=fleet, pads=pads, rng=rng, wind=wind)


def run_simulation(
    network: Network,
    scenario: Scenario,
    seed: int | None = None,
    advisor: RouteAdvisor | None = None,
    record_truth: bool = True,
    check_invariants: bool = False,
) -> SimulationResult:
    """Run ``scenario`` for ``duration_s``. ``seed`` overrides ``scenario.seed``.

    ``check_invariants`` runs the read-only ``InvariantChecker`` after every
    step and stores what it finds in ``result.violations``; output is unchanged.
    """
    seed = scenario.seed if seed is None else seed
    clock = SimClock.from_iso(scenario.start_time, dt_s=scenario.dt_s)
    world = build_world(network, scenario, seed)
    background_aircraft: list[BackgroundAircraft] = []
    background_states: list[BackgroundState] = []
    if scenario.world.background_traffic:
        if scenario.background_traffic.replay_file:
            from aerolink.realdata.replay import load_replay  # real flights (PLAN E5.1)

            background_aircraft = load_replay(
                Path(scenario.background_traffic.replay_file),
                network,
                scenario.start_time,
                scenario.duration_s,
                limit=scenario.background_traffic.count,
            )
        else:
            background_aircraft = generate_tracks(network, scenario.background_traffic, world.rng)
    cfg = ControllerConfig(
        dt_s=scenario.dt_s,
        start_local_hour=clock.local_hour(),
        turnaround_s=scenario.pad_turnaround_time_s,
        max_hold_s=scenario.max_hold_s,
        suggestion_acceptance_rate=scenario.suggestion_acceptance_rate,
        destination_change_probability=scenario.destination_change_probability,
        cruise_speed_sigma=scenario.cruise_speed_sigma,
        demand=scenario.demand,
        world=scenario.world,
        separation=scenario.separation,
        energy=scenario.energy,
        pad_booking=scenario.pad_booking,
        schedule=scenario.schedule,
        background_traffic=scenario.background_traffic,
    )
    controller = Controller(
        network,
        world,
        clock,
        advisor or ShortestPathAdvisor(network, k=scenario.route_options),
        cfg,
        background_aircraft=background_aircraft,
    )
    telemetry: list[dict] = []
    truth: list[dict] = []
    steps = math.ceil(scenario.duration_s / scenario.dt_s - 1e-9)
    checker = (
        InvariantChecker(network, scenario.dt_s, separation=cfg.separation if cfg.world.separation else None)
        if check_invariants
        else None
    )
    if checker is not None:
        checker.check_step(world)
    for _ in range(steps):
        if scenario.world.background_traffic:
            background_states = step_background(background_aircraft, world.t, network)
        controller.step()
        if scenario.world.background_traffic:
            ts = controller.clock.now()
            for st in background_states:
                if st.finished:
                    continue
                telemetry.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "message_id": str(UUID(bytes=world.rng.bytes(16), version=4)),
                        "aircraft_id": st.aircraft_id,
                        "vehicle_type": "background",
                        "timestamp": ts.isoformat().replace("+00:00", "Z"),
                        "position": {
                            "lat": round(st.position.lat, 8),
                            "lon": round(st.position.lon, 8),
                            "alt_msl_m": round(st.position.alt_msl_m, 2),
                        },
                        "motion": {
                            "ground_speed_mps": round(st.velocity_mps, 2),
                            "course_deg": round(st.heading_deg, 2),
                            "vertical_speed_mps": round(st.vertical_speed_mps, 2),
                        },
                        "fix": {"type": "3d", "hdop": 0.9, "satellites": 11, "replayed": True},
                        "active_flight_id": None,
                    }
                )
                if record_truth:
                    truth.append(
                        {
                            "t": round(world.t, 6),
                            "aircraft_id": st.aircraft_id,
                            "status": "background",
                            "node": None,
                            "lat": st.position.lat,
                            "lon": st.position.lon,
                            "alt_msl_m": st.position.alt_msl_m,
                            "velocity_mps": st.velocity_mps,
                            "vertical_speed_mps": st.vertical_speed_mps,
                            "heading_deg": st.heading_deg,
                            "battery_soc": 1.0,
                        }
                    )
        if checker is not None:
            checker.check_step(world)
        ts = controller.clock.now()
        for aid in sorted(world.aircraft):
            ac = world.aircraft[aid]
            rec = telemetry_record(ac, ts, scenario.telemetry_noise)
            if rec is not None:
                telemetry.append(rec)
            if record_truth:
                truth.append(
                    {
                        "t": round(world.t, 6),
                        "aircraft_id": aid,
                        "status": ac.status,
                        "node": ac.current_node,
                        "lat": ac.position.lat,
                        "lon": ac.position.lon,
                        "alt_msl_m": ac.position.alt_msl_m,
                        "velocity_mps": ac.velocity_mps,
                        "vertical_speed_mps": ac.vertical_speed_mps,
                        "heading_deg": ac.heading_deg,
                        "battery_soc": ac.battery_soc,
                    }
                )
    if checker is not None:
        checker.check_events(world.events, world)
    kinds: dict[str, int] = {}
    for e in world.events:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    return SimulationResult(
        scenario=scenario,
        seed=seed,
        telemetry=telemetry,
        events=world.events,
        truth=truth,
        final_world=world,
        steps=steps,
        stats={"event_counts": dict(sorted(kinds.items())), "telemetry_records": len(telemetry)},
        violations=checker.violations if checker is not None else [],
    )
```

### 5d. Tidy up and run the checks

These are the same checks GitHub runs automatically on every change. If they pass here, they'll pass there.

1. **Tidy the formatting.** Command:

```bash
uv run ruff format
uv run ruff check --fix
```

   *What it does:* `ruff` fixes spacing and small style details that copy-paste can leave behind. The first line prints something like `2 files reformatted, 90 files left unchanged` (any numbers are fine). The second should end with **`All checks passed!`** or **`Found 1 error (1 fixed, 0 remaining).`**

2. **Check you changed only what was intended in the two simulator files.** Command:

```bash
git diff --stat src/aerolink/sim
```

   You should see **14** next to `runner.py`, **3** next to `world_config.py`, and a last line like:

```text
 2 files changed, 16 insertions(+), 1 deletion(-)
```

   If the numbers are bigger, part of a file was lost or doubled when pasting: redo 5c, then step 1 again.

3. **Run the new tests.** Command:

```bash
uv run pytest -q tests/test_realdata.py
```

   Expect **`11 passed`**.

4. **Style and type checks.** Command:

```bash
uv run ruff check
uv run ruff format --check
uv run ty check
```

   *What it does:* `ruff` looks for common mistakes; `ty` checks that values are used as the right types. Expect **`All checks passed!`**, then **`... files already formatted`**, then **`All checks passed!`**.

If anything fails, it's almost always a pasting slip. Read the error: it names the file and line. Compare that file with this guide, fix it, and run the check again.

### 5e. Commit the code (before the long download)

Saving now means the code is safe even if the download is interrupted. Command (paste the whole box at once):

```bash
git add src/aerolink/realdata tests/test_realdata.py data/adsblol/README.md src/aerolink/sim/runner.py src/aerolink/sim/world_config.py pyproject.toml uv.lock
git commit -F - <<'EOF'
feat: E5.1 real-data pipeline (adsb.lol)

* feat: stream adsb.lol daily releases from GitHub without saving raw data
* feat: parse readsb trace files; filter to Central Florida helicopters and light aircraft
* feat: split flights into 1 Hz segments; write daily Parquet and filter reports
* feat: replay real flights as simulator background traffic
* chore: add pyarrow and mapie
EOF
git push -u origin m5-real-data-and-models
```

*What it does:* `git add` picks the files, `git commit` saves a snapshot with that message, `git push` uploads your branch to GitHub (`-u` remembers where, so later you can just type `git push`).

### 5f. Download and clean the real data

**Try one day first.** It shows how long a day takes and whether the filters keep a sensible number of flights. Command:

```bash
uv run python -m aerolink.realdata.fetch --days 1
```

*What it does:* picks the day 3 days ago (adsb.lol needs a day or two to publish), finds it on GitHub, and streams it. Every 20,000 aircraft it prints a progress line. When the day is done it prints a summary like this (example numbers):

```text
Funnel (all days):
  files_read                   61,402
  aircraft_candidate              212
  points_in_region            148,930
  points_kept                  41,077
  aircraft_kept                    38
  segments_kept                    71
  flight_hours                   11.2
```

How to read it:

- **files_read:** aircraft worldwide that day (one file each).
- **aircraft_candidate:** aircraft that flew over Central Florida and passed the aircraft rules.
- **points_in_region / points_kept:** their positions over Central Florida, and how many passed the position rules (low enough, right speed, real ADS-B).
- **aircraft_kept / segments_kept:** what was saved. A segment is one continuous piece of a flight.
- **flight_hours:** total flying time saved.
- Under **Why things were dropped**, each line names a rule and how many it removed.

**Write down** how many minutes the day took. If it says `no release published (yet)`, ask for an earlier day: `uv run python -m aerolink.realdata.fetch --days 1 --end 2026.11.19` (any date a few days back).

**Collect 14 days.** Command:

```bash
uv run python -m aerolink.realdata.fetch --days 14
```

*What it does:* the same for the 14 days ending 3 days ago. The day you already did is skipped. Expect about **13 times the minutes your test day took**. Keep this browser tab open. If it stops (Codespace slept, Wi-Fi dropped), reopen the Codespace, run `cd backend`, and run the same command again: finished days are skipped.

> **While it downloads** you can carry on with the code for Part 6 in a **second terminal** (click the **+** at the top right of the terminal panel, then `cd backend`). Every part has a "try it with made-up data" step, so you don't have to wait. Just don't run Part 6's real benchmark (6e) until the download has finished.

**Check the results.**

1. **Enough data?** The totals are also saved to `backend/eval-results/realdata-e5.1.json`. Aim for **at least 100 aircraft_kept and 50 flight_hours**. If you're well under, add more days (the ones you have are skipped): `uv run python -m aerolink.realdata.fetch --days 28`
2. **Small enough to commit?** Command:

```bash
du -sh data/adsblol
ls -lh data/adsblol
```

   GitHub refuses any single file over 100 MB. If the total is under about 200 MB and no file is over 50 MB, carry on. Otherwise stop and tell Joshua.

### 5g. Commit the data

Command:

```bash
git add data/adsblol eval-results/realdata-e5.1.json
git commit -m "data: E5.1 cleaned adsb.lol flights (ODbL) and filter counts"
git push
```

### 5h. Shrink the data, then push for the first time

> **Revised: this is the only E5.1 step left.** Get Joshua's OK first (decision D1 in `docs/Carla/plans/SIGNOFF.md`).

**Why:** GitHub refuses any file over 100 MB, and `segments-2026-11-10.parquet` is 108 MB (792 MB in total). Rounding the numbers to what the data really measures (positions to about 5 cm, altitude to 1 ft, speed and track to 0.1) and using stronger compression shrinks every file to about a third, with no new download. This was tested: the 108 MB day becomes 38 MB, and all 11 E5.1 tests still pass.

1. **Shrink the files and update the pipeline** so future days are written the same way. Paste the whole box at once. It's safe to run twice.

```bash
uv run python - <<'PYEOF'
"""Shrink the E5.1 Parquet files: round, float32, zstd. Safe to run twice."""
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

FETCH = Path("src/aerolink/realdata/fetch.py")
DATA = Path("data/adsblol")
ROUND = {"lat": 6, "lon": 6, "alt_ft": 0, "gs_kt": 1, "track_deg": 1, "vrate_fpm": 0}

# 1. future days are written the same way
text = FETCH.read_text()
edits = [
    (
        '+ [(c, pa.float64()) for c in COLUMNS[1:]]',
        '+ [(c, pa.float64() if c in ("lat", "lon") else pa.float32()) for c in COLUMNS[1:]]',
    ),
    (
        '    cols["t"] = [int(x) for x in cols["t"]]\n',
        '    cols["t"] = [int(x) for x in cols["t"]]\n'
        "    for c, digits in ROUNDING.items():  # ~5 cm on lat/lon; keeps the files small\n"
        "        cols[c] = [round(v, digits) for v in cols[c]]\n",
    ),
    (
        'pq.write_table(to_table(segments, day), out / f"segments-{day.isoformat()}.parquet")',
        'pq.write_table(\n'
        '        to_table(segments, day),\n'
        '        out / f"segments-{day.isoformat()}.parquet",\n'
        '        compression="zstd",\n'
        '        compression_level=9,\n'
        '    )',
    ),
    (
        'ATTRIBUTION = "Contains data',
        'ROUNDING = {"lat": 6, "lon": 6, "alt_ft": 0, "gs_kt": 1, "track_deg": 1, "vrate_fpm": 0}\n'
        'ATTRIBUTION = "Contains data',
    ),
]
for old, new in edits:
    if new in text:
        print(f"  already done: {new.splitlines()[0].strip()[:60]}")
    elif text.count(old) == 1:
        text = text.replace(old, new)
        print(f"  updated: {new.splitlines()[0].strip()[:60]}")
    else:
        raise SystemExit(f"STOP: could not find this text exactly once in {FETCH}:\n{old}")
FETCH.write_text(text)

# 2. re-encode the days already downloaded (no new download)
for path in sorted(DATA.glob("segments-*.parquet")):
    before = path.stat().st_size / 1e6
    table = pq.read_table(path)
    meta = table.schema.metadata
    for c, digits in ROUND.items():
        typ = pa.float64() if c in ("lat", "lon") else pa.float32()
        i = table.schema.get_field_index(c)
        table = table.set_column(i, pa.field(c, typ), pc.round(table[c], digits).cast(typ))
    tmp = path.with_suffix(".tmp")
    pq.write_table(table.replace_schema_metadata(meta), tmp, compression="zstd", compression_level=9)
    tmp.replace(path)
    print(f"  {path.name}: {before:.0f} MB -> {path.stat().st_size / 1e6:.0f} MB")
print("Done.")
PYEOF
```

   You'll see 4 `updated:` lines, then one line per day (for example `segments-2026-11-10.parquet: 108 MB -> 38 MB`), then **`Done.`** It takes a few minutes. If you see `STOP: ...`, send the message to Joshua.

2. **Check.** Command:

```bash
uv run pytest -q tests/test_realdata.py
uv run ruff format --check && uv run ruff check
ls -lh data/adsblol && du -sh data/adsblol
```

   Expect **`11 passed`**, **`All checks passed!`**, and **no file over 50 MB**.

3. **Rewrite the E5.1 history so the 108 MB file never reaches GitHub.** The old big files are still inside commit `087ebd0`, and GitHub checks every commit you push, not just the latest. This turns E5.1 and the merge from `main` into one clean commit on top of `main`. Paste the whole box at once:

```bash
cd ..
git fetch origin
git branch e5.1-backup                     # safety copy of the old history; never push it
git add backend/data/adsblol backend/src/aerolink/realdata/fetch.py
git reset --soft origin/main               # keeps every file exactly as it is now
git commit -F - <<'EOF'
feat: E5.1 real-data pipeline (adsb.lol)

* feat: stream adsb.lol daily releases from GitHub without saving raw data
* feat: parse readsb trace files; filter to Central Florida helicopters and light aircraft
* feat: split flights into 1 Hz segments; write daily Parquet (zstd, rounded) and filter reports
* feat: replay real flights as simulator background traffic
* data: cleaned flights in backend/data/adsblol (ODbL, attributed), 14 days
* docs: ARCHITECTURE and PLAN updated for E5.1
EOF
git status --short
cd backend
```

   *What it does:* `reset --soft` moves the branch back to `main` without touching any file, and the commit then saves everything as one snapshot. `pyproject.toml`, `uv.lock` (mapie), `src/aerolink/realdata/__init__.py` and this guide stay uncommitted: `git status --short` shows them as ` M`, and the plan files as `??`. That's expected. They belong to later commits.

4. **Make sure nothing over 50 MB is about to be pushed.** Command:

```bash
git rev-list --objects origin/main..HEAD | git cat-file --batch-check='%(objectsize) %(rest)' | sort -n | tail -3
```

   The biggest number in the first column should be under 50,000,000 (50 MB).

5. **Push for the first time.** Command:

```bash
git push -u origin HEAD
```

   From now on, the plain `git push` lines in this guide work. When the push has succeeded, you can delete the safety copy: `git branch -D e5.1-backup`.

**E5.1 done.**

---

## Part 6: E5.2 Real-Data Benchmark

**What you're building:** a test bench that hides the last ~30% of your downloaded days, then asks each forecaster "here are the last 60 seconds of this real flight; where will it be in 30, 60, 120 and 300 seconds?" and measures how far off the answers are.

**Why split by day:** two moments from the same flight look almost identical. If training and testing could share a day, a model could "cheat" by remembering. Splitting by day (latest days held out) keeps the test honest. The same split is reused in E5.3 and E5.5.

**What gets measured, per forecaster and horizon:**
- **FDE** (final displacement error): distance between the predicted and true position at that horizon, in metres (mean, median, worst 10%).
- **ADE** (average displacement error): the same, averaged over all horizons up to this one.
- **Coverage:** how often the truth falls inside the forecaster's p10–p90 range. A well-calibrated forecaster scores about 80%.

**One fix included:** the existing Chronos-2 wrapper (`forecast/chronos.py`) used the older Chronos *v1* loading code, which can't run the Chronos-2 weights, and it reported positions relative to the wrong point. You replace it with a version that uses the Chronos-2 API, works in batches (much faster on a CPU) and reports offsets from the aircraft's current position. While checking the library, it turned out that fine-tuning *is* supported (`Chronos2Pipeline.fit`); the docs step in Part 11 records that.

### 6a. Replace the Chronos-2 forecaster

#### Replace the whole file: `backend/src/aerolink/forecast/chronos.py`

*What changes:* Loads Chronos-2 with the right API, forecasts in batches, and reads out p10/p50/p90 for each horizon.

1. Open the file (command):

```bash
code src/aerolink/forecast/chronos.py
```

2. Click inside the file, press **Ctrl + A** (select everything), then **Delete**.
3. Paste the box below with **Ctrl + V**, then **Ctrl + S**.

File contents:

```python
"""Chronos-2 zero-shot forecaster (PLAN E4.2, used on real data in E5.2).

Loads a pretrained Chronos-2 model once (``chronos-forecasting`` >= 2.3) and exposes the
``Forecaster`` protocol:

* position head: the last 60 s of east/north/up positions as one multivariate series;
  the model forecasts the next ``max(horizons)`` seconds and we read out each horizon.
* ETA head: the last 60 s of ground speed; the p10/p50/p90 speed profiles are integrated
  along the path (fast profile -> early arrival, slow profile -> late arrival).

Fine-tuning: supported by the installed library (``Chronos2Pipeline.fit``, full or LoRA);
not used here, see ARCHITECTURE §7.1.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from aerolink.contracts import VehicleType
from aerolink.forecast.cv import _ETA_TIME_SPREAD
from aerolink.forecast.features import to_local
from aerolink.forecast.interface import EtaPrediction, Forecaster, NodeEta, PositionPrediction
from aerolink.network import Network
from aerolink.vehicle import vehicle_cfg

_POSITION_HORIZONS = [30, 60, 120, 300]
_HISTORY_S = 60
_ETA_PREDICTION_S = 64
_QUANTILES = [0.1, 0.5, 0.9]


def _enu_series(track: np.ndarray) -> np.ndarray:
    """(3, T) east/north/up in metres relative to the *last* fix of a (T, 3) lat/lon/alt track."""
    lat0, lon0 = float(track[-1, 0]), float(track[-1, 1])
    east, north = to_local(track[:, 0], track[:, 1], lat0, lon0)
    up = track[:, 2] - track[-1, 2]
    return np.stack([east, north, up]).astype(np.float32)


def _speed_series(track: np.ndarray) -> np.ndarray:
    """(T-1,) ground speed m/s from a 1 Hz lat/lon/alt track."""
    lat0, lon0 = float(track[-1, 0]), float(track[-1, 1])
    east, north = to_local(track[:, 0], track[:, 1], lat0, lon0)
    return np.hypot(np.diff(east), np.diff(north)).astype(np.float32)


class ChronosForecaster(Forecaster):
    """Zero-shot Chronos-2 forecaster (default weights ``autogluon/chronos-2-small``)."""

    def __init__(self, network: Network | None, model_name: str = "autogluon/chronos-2-small"):
        self.network = network
        self.model_name = model_name
        self._pipeline: Any = None

    @property
    def name(self) -> str:
        return "chronos"

    def _load(self) -> Any:
        if self._pipeline is None:
            from chronos import BaseChronosPipeline

            self._pipeline = BaseChronosPipeline.from_pretrained(self.model_name, device_map="cpu")
        return self._pipeline

    def _quantiles(self, context: np.ndarray, prediction_length: int) -> np.ndarray:
        """(B, V, H, 3) p10/p50/p90 for a (B, V, T) context batch."""
        import torch

        quantiles, _ = self._load().predict_quantiles(
            torch.tensor(context, dtype=torch.float32),
            prediction_length=prediction_length,
            quantile_levels=_QUANTILES,
        )
        return np.stack([q.detach().cpu().numpy() for q in quantiles])

    # --- position head -------------------------------------------------------------------

    def predict_position_batch(
        self, tracks: list[np.ndarray], horizons_s: list[int] | None = None, batch_size: int = 64
    ) -> list[list[PositionPrediction]]:
        """Position predictions for many (T, 3) lat/lon/alt tracks at once (much faster on CPU)."""
        horizons = horizons_s or _POSITION_HORIZONS
        out: list[list[PositionPrediction]] = []
        for start in range(0, len(tracks), batch_size):
            chunk = [np.asarray(t, dtype=float)[-_HISTORY_S:] for t in tracks[start : start + batch_size]]
            context = np.stack([_enu_series(t) for t in chunk])  # (B, 3, T)
            q = self._quantiles(context, max(horizons))  # (B, 3, H, 3)
            for b in range(len(chunk)):
                preds = []
                for h in horizons:
                    e, n, u = q[b, 0, h - 1], q[b, 1, h - 1], q[b, 2, h - 1]
                    preds.append(
                        PositionPrediction(
                            horizon_s=h,
                            east_p10=float(e[0]),
                            east_p50=float(e[1]),
                            east_p90=float(e[2]),
                            north_p10=float(n[0]),
                            north_p50=float(n[1]),
                            north_p90=float(n[2]),
                            up_p10=float(u[0]),
                            up_p50=float(u[1]),
                            up_p90=float(u[2]),
                        )
                    )
                out.append(preds)
        return out

    def predict_position(
        self,
        track: list[tuple[float, float, float]],
        t_s: float,
        vehicle_type: VehicleType,
        horizons_s: list[int] | None = None,
    ) -> list[PositionPrediction]:
        horizons = horizons_s or _POSITION_HORIZONS
        if len(track) < 2:
            return [PositionPrediction(h, -10.0, 0.0, 10.0, -10.0, 0.0, 10.0, -5.0, 0.0, 5.0) for h in horizons]
        return self.predict_position_batch([np.asarray(track, dtype=float)], horizons)[0]

    # --- ETA head ------------------------------------------------------------------------

    def predict_eta(
        self,
        path: list[str],
        origin_t_s: float,
        vehicle_type: VehicleType,
        track: list[tuple[float, float, float]] | None = None,
        current_speed_mps: float | None = None,
    ) -> EtaPrediction:
        """Per-node ETAs from forecast speed profiles; constant cruise speed after the forecast ends."""
        assert self.network is not None, "the ETA head needs the corridor network"
        cruise = vehicle_cfg(vehicle_type).cruise_speed_mps
        speed = cruise
        if current_speed_mps is not None and current_speed_mps > 1.0:
            speed = 0.5 * (cruise + current_speed_mps)
        if track is None or len(track) < 3:
            return self._cv_eta(path, origin_t_s, speed)

        speeds = _speed_series(np.asarray(track, dtype=float)[-(_HISTORY_S + 1) :])
        q = self._quantiles(speeds[None, None, :], _ETA_PREDICTION_S)[0, 0]  # (H, 3)
        etas: list[NodeEta] = []
        cumul_m = 0.0
        for i, node in enumerate(path):
            if i > 0:
                cumul_m += self.network.edges[(path[i - 1], node)].length_m
            fast = self._time_to_cover(q[:, 2], cumul_m, speed)  # p90 speed -> early arrival
            mid = self._time_to_cover(q[:, 1], cumul_m, speed)
            slow = self._time_to_cover(q[:, 0], cumul_m, speed)
            etas.append(
                NodeEta(node=node, eta_p10=origin_t_s + fast, eta_p50=origin_t_s + mid, eta_p90=origin_t_s + slow)
            )
        return EtaPrediction(path=path, origin_t_s=origin_t_s, etas=etas)

    @staticmethod
    def _time_to_cover(profile: np.ndarray, distance_m: float, cruise_speed: float) -> float:
        """Seconds to fly ``distance_m`` at a 1 Hz speed profile, then at ``cruise_speed``."""
        if distance_m <= 0:
            return 0.0
        covered = np.cumsum(np.maximum(1.0, profile))
        idx = int(np.searchsorted(covered, distance_m))
        if idx < len(covered):
            before = covered[idx - 1] if idx > 0 else 0.0
            return idx + (distance_m - before) / max(1.0, float(profile[idx]))
        return len(profile) + (distance_m - float(covered[-1])) / max(1.0, cruise_speed)

    def _cv_eta(self, path: list[str], origin_t_s: float, speed: float) -> EtaPrediction:
        assert self.network is not None
        etas: list[NodeEta] = []
        cumul_m = 0.0
        total_m = self.network.route_length_m(path)
        total_s = total_m / max(1.0, speed)
        spread_s = max(30.0, total_s * _ETA_TIME_SPREAD)
        for i, node in enumerate(path):
            if i > 0:
                cumul_m += self.network.edges[(path[i - 1], node)].length_m
            eta = origin_t_s + (cumul_m / max(1.0, total_m)) * total_s
            etas.append(NodeEta(node=node, eta_p10=eta - spread_s, eta_p50=eta, eta_p90=eta + spread_s))
        return EtaPrediction(path=path, origin_t_s=origin_t_s, etas=etas)


def is_available(model_name: str = "autogluon/chronos-2-small") -> bool:
    """True if the weights can be loaded (downloaded once from Hugging Face, then cached)."""
    try:
        ChronosForecaster(None, model_name)._load()
    except Exception:  # noqa: BLE001 - no network, no cache, wrong library version...
        return False
    return True
```

### 6b. Create the new files

#### New file: `backend/src/aerolink/forecast/features.py`

*What this file does:* Shared geometry (lat/lon to metres) and the feature recipes the trained model uses (E5.3). The benchmark and the Chronos wrapper use its helpers too.

Open the file (command):

```bash
code src/aerolink/forecast/features.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Shared geometry and feature builders for the trained forecaster (PLAN E5.3).

Positions are turned into a local east/north/up frame in metres (flat-earth around the
current fix; error < 0.1% over the 300 s horizons used here). The same helpers are used
for real flights (adsb.lol) and simulated ones, so one model trains on both.

Position-head targets are residuals over constant velocity in the aircraft's own frame:
``along`` (ahead of / behind the CV point), ``cross`` (right / left of the CV line) and
``up``. Turning and speed changes show up there directly, which is easier to learn than
east/north residuals that depend on the heading.
"""

from __future__ import annotations

import math

import numpy as np

EARTH_R_M = 6_371_008.8
POSITION_HORIZONS_S = [30, 60, 120, 300]
HISTORY_S = 60  # seconds of 1 Hz history a position sample carries

# Vehicle classes as a number for LightGBM. Real data: A1 light, A7 rotorcraft.
VEHICLE_CODE = {"light": 0.0, "rotor": 1.0, "passenger": 2.0, "medical": 3.0}
CATEGORY_TO_VEHICLE = {"A1": "light", "A7": "rotor"}

POSITION_FEATURES = [
    "horizon_s",
    "vehicle",
    "hour",
    "speed_mps",
    "speed_mean_30",
    "speed_std_30",
    "accel_10",
    "accel_30",
    "turn_rate_10",
    "turn_rate_30",
    "vrate_mps",
    "vrate_mean_30",
    "alt_m",
    "speed_bias",
    # Graph context: NaN for real flights (they don't fly AeroLink corridors).
    "dist_next_node_m",
    "turn_at_next_deg",
    "remaining_route_m",
]


def to_local(lat: np.ndarray, lon: np.ndarray, lat0: float, lon0: float) -> tuple[np.ndarray, np.ndarray]:
    """(east_m, north_m) of points relative to (lat0, lon0)."""
    k = math.cos(math.radians(lat0))
    east = np.radians(np.asarray(lon, dtype=float) - lon0) * EARTH_R_M * k
    north = np.radians(np.asarray(lat, dtype=float) - lat0) * EARTH_R_M
    return east, north


def from_local(east: float, north: float, lat0: float, lon0: float) -> tuple[float, float]:
    k = math.cos(math.radians(lat0))
    return lat0 + math.degrees(north / EARTH_R_M), lon0 + math.degrees(east / (EARTH_R_M * k))


def _course(de: np.ndarray, dn: np.ndarray) -> np.ndarray:
    return np.degrees(np.arctan2(de, dn)) % 360.0


def _wrap180(a: np.ndarray | float) -> np.ndarray | float:
    return (np.asarray(a) + 180.0) % 360.0 - 180.0


def cv_state(history: np.ndarray) -> tuple[float, float]:
    """(speed m/s, course deg) from the last two 1 Hz fixes, exactly like ``CVForecaster``."""
    lat0, lon0 = float(history[-1, 0]), float(history[-1, 1])
    e, n = to_local(history[-2:, 0], history[-2:, 1], lat0, lon0)
    de, dn = float(e[1] - e[0]), float(n[1] - n[0])
    return math.hypot(de, dn), float(_course(np.array(de), np.array(dn)))


def kinematics(history: np.ndarray) -> dict[str, float]:
    """Kinematic features from a (T, 3) array of (lat, lon, alt_m), 1 Hz, oldest first."""
    lat0, lon0 = float(history[-1, 0]), float(history[-1, 1])
    e, n = to_local(history[:, 0], history[:, 1], lat0, lon0)
    de, dn = np.diff(e), np.diff(n)
    speed = np.hypot(de, dn)
    course = _course(de, dn)
    up = history[:, 2]
    vrate = np.diff(up)

    def turn_rate(window: int) -> float:
        w = min(window, len(course) - 1)
        if w < 1:
            return 0.0
        return float(np.sum(_wrap180(np.diff(course[-w - 1 :]))) / w)

    def accel(window: int) -> float:
        w = min(window, len(speed) - 1)
        return float((speed[-1] - speed[-1 - w]) / w) if w >= 1 else 0.0

    last30 = speed[-30:]
    return {
        "speed_mps": float(speed[-1]),
        "speed_mean_30": float(last30.mean()),
        "speed_std_30": float(last30.std()),
        "accel_10": accel(10),
        "accel_30": accel(30),
        "turn_rate_10": turn_rate(10),
        "turn_rate_30": turn_rate(30),
        "vrate_mps": float(vrate[-1]),
        "vrate_mean_30": float(vrate[-30:].mean()),
        "alt_m": float(up[-1]),
        "speed_bias": float(speed[-1] / max(1.0, speed.mean())),
    }


def cv_point(history: np.ndarray, horizon_s: float) -> tuple[float, float]:
    """CV-predicted (east, north) displacement from the last fix."""
    speed, course = cv_state(history)
    c = math.radians(course)
    return speed * horizon_s * math.sin(c), speed * horizon_s * math.cos(c)


def to_track_frame(de: float, dn: float, course_deg: float) -> tuple[float, float]:
    """East/north offset -> (along, cross) in a frame aligned with the course (cross > 0 = right)."""
    c = math.radians(course_deg)
    return de * math.sin(c) + dn * math.cos(c), de * math.cos(c) - dn * math.sin(c)


def from_track_frame(along: float, cross: float, course_deg: float) -> tuple[float, float]:
    c = math.radians(course_deg)
    return along * math.sin(c) + cross * math.cos(c), along * math.cos(c) - cross * math.sin(c)


def position_row(
    history: np.ndarray,
    horizon_s: int,
    vehicle: str | None,
    hour: float | None,
    graph: dict[str, float] | None = None,
) -> list[float]:
    """One feature row (``POSITION_FEATURES`` order) for one horizon."""
    k = kinematics(history)
    g = graph or {}
    values = {
        "horizon_s": float(horizon_s),
        "vehicle": VEHICLE_CODE.get(vehicle or "", math.nan),
        "hour": math.nan if hour is None else float(hour),
        **k,
        "dist_next_node_m": g.get("dist_next_node_m", math.nan),
        "turn_at_next_deg": g.get("turn_at_next_deg", math.nan),
        "remaining_route_m": g.get("remaining_route_m", math.nan),
    }
    return [values[f] for f in POSITION_FEATURES]


def position_targets(history: np.ndarray, future: np.ndarray, horizon_s: int) -> tuple[float, float, float]:
    """Residual (along, cross, up) of the true position at ``horizon_s`` over the CV point.

    ``future`` is the true (lat, lon, alt_m) at the horizon.
    """
    lat0, lon0 = float(history[-1, 0]), float(history[-1, 1])
    te, tn = to_local(np.array([future[0]]), np.array([future[1]]), lat0, lon0)
    ce, cn = cv_point(history, horizon_s)
    _, course = cv_state(history)
    along, cross = to_track_frame(float(te[0]) - ce, float(tn[0]) - cn, course)
    return along, cross, float(future[2] - history[-1, 2])


# --- ETA head -----------------------------------------------------------------------------

ETA_FEATURES = [
    "vehicle",
    "hour",
    "remaining_m",
    "cv_remaining_s",
    "speed_mps",
    "speed_ratio",
    "nodes_left",
    "merges_left",
    "traffic_ahead",
]


def eta_row(
    vehicle: str,
    hour: float | None,
    remaining_m: float,
    cv_remaining_s: float,
    speed_mps: float,
    cruise_mps: float,
    nodes_left: int,
    merges_left: int,
    traffic_ahead: float,
) -> list[float]:
    values = {
        "vehicle": VEHICLE_CODE.get(vehicle, math.nan),
        "hour": math.nan if hour is None else float(hour),
        "remaining_m": remaining_m,
        "cv_remaining_s": cv_remaining_s,
        "speed_mps": speed_mps,
        "speed_ratio": speed_mps / max(1.0, cruise_mps),
        "nodes_left": float(nodes_left),
        "merges_left": float(merges_left),
        "traffic_ahead": traffic_ahead,
    }
    return [values[f] for f in ETA_FEATURES]


def cv_speed(cruise_mps: float, current_speed_mps: float | None) -> float:
    """The speed ``CVForecaster.predict_eta`` uses: cruise, blended with the observed speed."""
    if current_speed_mps is not None and current_speed_mps > 1.0:
        return 0.5 * (cruise_mps + current_speed_mps)
    return cruise_mps
```

#### New file: `backend/src/aerolink/realdata/samples.py`

*What this file does:* Turns the cleaned flights into test samples (60 s of history + the true position at each horizon) and does the chronological day split.

Open the file (command):

```bash
code src/aerolink/realdata/samples.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Turn the cleaned adsb.lol segments into forecasting samples (PLAN E5.2, E5.3).

A *sample* is one moment in one real flight: the previous 60 s of positions (what a
forecaster sees) and the true position 30, 60, 120 and 300 s later (what it should
predict). Samples are taken every ``stride_s`` seconds along each segment.

Days, not samples, are split into training and held-out sets: two samples from the same
flight are almost identical, so a random split would leak the answer into training.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from aerolink.forecast.features import CATEGORY_TO_VEHICLE, HISTORY_S, POSITION_HORIZONS_S

FT_TO_M = 0.3048
DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "adsblol"
FLORIDA_UTC_OFFSET = timezone(timedelta(hours=-5))  # EST; good enough for an hour-of-day feature


@dataclass
class PositionSamples:
    """Arrays with one row per sample (N) and one column per horizon (H)."""

    day: np.ndarray  # (N,) "YYYY-MM-DD"
    segment_id: np.ndarray  # (N,)
    t: np.ndarray  # (N,) unix seconds of the last history fix
    vehicle: np.ndarray  # (N,) "rotor" / "light" / "" (unknown)
    hour: np.ndarray  # (N,) local hour of day
    history: np.ndarray  # (N, HISTORY_S, 3) lat, lon, alt_m; oldest first
    future: np.ndarray  # (N, H, 3) true lat, lon, alt_m at each horizon; NaN if past segment end
    horizons_s: list[int]

    def __len__(self) -> int:
        return len(self.t)

    def subset(self, mask: np.ndarray) -> PositionSamples:
        return PositionSamples(
            day=self.day[mask],
            segment_id=self.segment_id[mask],
            t=self.t[mask],
            vehicle=self.vehicle[mask],
            hour=self.hour[mask],
            history=self.history[mask],
            future=self.future[mask],
            horizons_s=self.horizons_s,
        )


def list_days(data_dir: Path = DATA_DIR) -> list[str]:
    return sorted(p.stem.removeprefix("segments-") for p in data_dir.glob("segments-*.parquet"))


def split_days(days: list[str], held_out_fraction: float = 0.3) -> tuple[list[str], list[str]]:
    """Chronological split: the latest days are held out (at least one, if there are two or more)."""
    days = sorted(days)
    if len(days) < 2:
        raise ValueError(f"need at least 2 days to split, have {len(days)}")
    n_held = min(len(days) - 1, max(1, round(len(days) * held_out_fraction)))
    return days[:-n_held], days[-n_held:]


def _segments(path: Path):
    table = pq.read_table(path, columns=["segment_id", "category", "t", "lat", "lon", "alt_ft"])
    seg = np.array(table["segment_id"].to_pylist())
    cat = np.array([c or "" for c in table["category"].to_pylist()])
    cols = {c: table[c].to_numpy().astype(float) for c in ["t", "lat", "lon", "alt_ft"]}
    order = np.lexsort((cols["t"], seg))
    seg, cat = seg[order], cat[order]
    cols = {c: v[order] for c, v in cols.items()}
    starts = np.flatnonzero(np.r_[True, seg[1:] != seg[:-1]])
    ends = np.r_[starts[1:], len(seg)]
    for a, b in zip(starts, ends, strict=True):
        xyz = np.column_stack([cols["lat"][a:b], cols["lon"][a:b], cols["alt_ft"][a:b] * FT_TO_M])
        yield str(seg[a]), str(cat[a]), cols["t"][a:b], xyz


def build_position_samples(
    days: list[str],
    data_dir: Path = DATA_DIR,
    stride_s: int = 30,
    horizons_s: list[int] | None = None,
) -> PositionSamples:
    """Samples from every segment of the given days (1 Hz data, so index = seconds)."""
    horizons = horizons_s or POSITION_HORIZONS_S
    rows: dict[str, list] = {k: [] for k in ["day", "seg", "t", "veh", "hour", "hist", "fut"]}
    for day in days:
        for seg_id, category, t, xyz in _segments(data_dir / f"segments-{day}.parquet"):
            if np.isnan(xyz[:, 2]).any():  # altitude gaps: fill from neighbours
                ok = ~np.isnan(xyz[:, 2])
                if ok.sum() < 2:
                    continue
                xyz[:, 2] = np.interp(np.arange(len(xyz)), np.flatnonzero(ok), xyz[ok, 2])
            n = len(t)
            for i in range(HISTORY_S - 1, n - min(horizons), stride_s):
                fut = np.full((len(horizons), 3), np.nan)
                for j, h in enumerate(horizons):
                    if i + h < n:
                        fut[j] = xyz[i + h]
                local = datetime.fromtimestamp(float(t[i]), UTC).astimezone(FLORIDA_UTC_OFFSET)
                rows["day"].append(day)
                rows["seg"].append(seg_id)
                rows["t"].append(float(t[i]))
                rows["veh"].append(CATEGORY_TO_VEHICLE.get(category, ""))
                rows["hour"].append(local.hour + local.minute / 60.0)
                rows["hist"].append(xyz[i - HISTORY_S + 1 : i + 1])
                rows["fut"].append(fut)
    if not rows["t"]:
        empty = np.zeros((0,))
        return PositionSamples(
            empty.astype(str),
            empty.astype(str),
            empty,
            empty.astype(str),
            empty,
            np.zeros((0, HISTORY_S, 3)),
            np.zeros((0, len(horizons), 3)),
            horizons,
        )
    return PositionSamples(
        day=np.array(rows["day"]),
        segment_id=np.array(rows["seg"]),
        t=np.array(rows["t"]),
        vehicle=np.array(rows["veh"]),
        hour=np.array(rows["hour"]),
        history=np.stack(rows["hist"]),
        future=np.stack(rows["fut"]),
        horizons_s=horizons,
    )
```

#### New file: `backend/src/aerolink/realdata/benchmark.py`

*What this file does:* The command you run. Scores each forecaster on the held-out days and writes `eval-results/realdata-e5.2.json`.

Open the file (command):

```bash
code src/aerolink/realdata/benchmark.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Forecasters on real flights: position-head error per horizon (PLAN E5.2).

    uv run python -m aerolink.realdata.benchmark                          # cv + chronos
    uv run python -m aerolink.realdata.benchmark --forecasters cv chronos aerolink-gbm

Days are split chronologically (``samples.split_days``): the latest ~30% are held out and
never used for training. Only held-out samples are scored here. For every forecaster and
horizon (30/60/120/300 s) it reports:

* ``fde_*``: horizontal distance between the p50 prediction and the true position at that
  horizon (mean, median, p90), in metres
* ``ade_mean_m``: the same error averaged over all horizons up to this one
* ``coverage_*``: how often the true offset falls inside the p10-p90 interval
  (east, north, up separately, and east+north together); the target for a calibrated
  model is 80% per axis

Results: ``backend/eval-results/realdata-e5.2.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, cast

import numpy as np

from aerolink.forecast.features import to_local
from aerolink.forecast.interface import PositionPrediction
from aerolink.realdata.fetch import ATTRIBUTION
from aerolink.realdata.samples import DATA_DIR, PositionSamples, build_position_samples, list_days, split_days

BACKEND = Path(__file__).resolve().parents[3]
DEFAULT_OUT = BACKEND / "eval-results" / "realdata-e5.2.json"


def make_forecaster(name: str, chronos_model: str):
    if name == "cv":
        from aerolink.forecast.cv import CVForecaster

        return CVForecaster(cast(Any, None))  # the position head needs no network
    if name == "chronos":
        from aerolink.forecast.chronos import ChronosForecaster

        return ChronosForecaster(None, chronos_model)
    if name == "aerolink-gbm":
        from aerolink.forecast.gbm import GBMForecaster

        return GBMForecaster.load(None)
    raise ValueError(f"unknown forecaster {name!r}")


def predict_all(forecaster, samples: PositionSamples) -> list[list[PositionPrediction]]:
    """Predictions for every sample, batched when the forecaster supports it."""
    tracks = [samples.history[i] for i in range(len(samples))]
    if hasattr(forecaster, "predict_position_batch"):
        kwargs = {}
        if getattr(forecaster, "name", "") == "aerolink-gbm":
            kwargs = {"vehicles": list(samples.vehicle), "hours": list(samples.hour)}
        return forecaster.predict_position_batch(tracks, samples.horizons_s, **kwargs)
    return [forecaster.predict_position([tuple(p) for p in tr], 0.0, "passenger", samples.horizons_s) for tr in tracks]


def score(samples: PositionSamples, preds: list[list[PositionPrediction]]) -> list[dict]:
    """Per-horizon error and interval coverage."""
    n, h = len(samples), len(samples.horizons_s)
    err = np.full((n, h), np.nan)
    inside = np.full((n, h, 3), np.nan)
    for i in range(n):
        lat0, lon0 = samples.history[i, -1, 0], samples.history[i, -1, 1]
        alt0 = samples.history[i, -1, 2]
        for j in range(h):
            fut = samples.future[i, j]
            if np.isnan(fut[0]):
                continue
            te, tn = to_local(np.array([fut[0]]), np.array([fut[1]]), lat0, lon0)
            truth = (float(te[0]), float(tn[0]), float(fut[2] - alt0))
            p = preds[i][j]
            err[i, j] = float(np.hypot(p.east_p50 - truth[0], p.north_p50 - truth[1]))
            inside[i, j, 0] = p.east_p10 <= truth[0] <= p.east_p90
            inside[i, j, 1] = p.north_p10 <= truth[1] <= p.north_p90
            inside[i, j, 2] = p.up_p10 <= truth[2] <= p.up_p90
    rows = []
    for j, horizon in enumerate(samples.horizons_s):
        ok = ~np.isnan(err[:, j])
        if not ok.any():
            rows.append({"horizon_s": horizon, "n": 0})
            continue
        e = err[ok, j]
        upto = err[:, : j + 1]
        full = ~np.isnan(upto).any(axis=1)
        cov = inside[ok, j]
        rows.append(
            {
                "horizon_s": horizon,
                "n": int(ok.sum()),
                "fde_mean_m": round(float(e.mean()), 1),
                "fde_median_m": round(float(np.median(e)), 1),
                "fde_p90_m": round(float(np.quantile(e, 0.9)), 1),
                "ade_mean_m": round(float(upto[full].mean()), 1) if full.any() else None,
                "coverage_east": round(float(cov[:, 0].mean()), 3),
                "coverage_north": round(float(cov[:, 1].mean()), 3),
                "coverage_up": round(float(cov[:, 2].mean()), 3),
                "coverage_horizontal": round(float((cov[:, 0] * cov[:, 1]).mean()), 3),
            }
        )
    return rows


def print_table(results: dict) -> None:
    print(f"\n{'forecaster':<14}{'horizon':>8}{'n':>8}{'FDE mean':>10}{'median':>9}{'p90':>9}{'ADE':>9}{'cov E/N':>12}")
    for name, rows in results.items():
        for r in rows:
            if not r["n"]:
                continue
            cov = f"{r['coverage_east']:.0%}/{r['coverage_north']:.0%}"
            print(
                f"{name:<14}{r['horizon_s']:>7}s{r['n']:>8}{r['fde_mean_m']:>9.0f}m{r['fde_median_m']:>8.0f}m"
                f"{r['fde_p90_m']:>8.0f}m{r['ade_mean_m'] or 0:>8.0f}m{cov:>12}"
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--forecasters", nargs="+", default=["cv", "chronos"])
    ap.add_argument("--chronos-model", default="autogluon/chronos-2-small")
    ap.add_argument("--data", type=Path, default=DATA_DIR)
    ap.add_argument("--held-out-fraction", type=float, default=0.3)
    ap.add_argument("--stride-s", type=int, default=30, help="seconds between samples along a flight")
    ap.add_argument("--max-samples", type=int, default=5000, help="random subset of held-out samples (0 = all)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args(argv)

    days = list_days(a.data)
    train_days, test_days = split_days(days, a.held_out_fraction)
    print(f"{len(days)} days: training {train_days[0]}..{train_days[-1]}, held out {test_days[0]}..{test_days[-1]}")
    samples = build_position_samples(test_days, a.data, stride_s=a.stride_s)
    total = len(samples)
    if a.max_samples and total > a.max_samples:
        keep = np.zeros(total, dtype=bool)
        keep[np.random.default_rng(a.seed).choice(total, a.max_samples, replace=False)] = True
        samples = samples.subset(keep)
    print(f"held-out samples: {total:,} (scoring {len(samples):,})")
    if not len(samples):
        print("No samples: collect more days first (E5.1).")
        return 1

    results: dict[str, list[dict]] = {}
    timing: dict[str, float] = {}
    for name in a.forecasters:
        print(f"running {name} ...", flush=True)
        started = time.monotonic()
        preds = predict_all(make_forecaster(name, a.chronos_model), samples)
        timing[name] = round((time.monotonic() - started) / len(samples) * 1000, 2)
        results[name] = score(samples, preds)
    print_table(results)
    report = {
        "attribution": ATTRIBUTION,
        "split": {"train_days": train_days, "held_out_days": test_days},
        "samples": {"held_out_total": total, "scored": len(samples), "stride_s": a.stride_s, "seed": a.seed},
        "chronos_model": a.chronos_model if "chronos" in a.forecasters else None,
        "ms_per_sample": timing,
        "results": results,
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

#### New file: `backend/src/aerolink/realdata/synthetic_days.py`

*What this file does:* Makes made-up flight days in exactly the E5.1 format, for the tests and for trying commands before the real download is finished. Never real data.

Open the file (command):

```bash
code src/aerolink/realdata/synthetic_days.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Made-up flight days in the E5.1 output format, for tests and dry runs. NOT REAL DATA.

    uv run python -m aerolink.realdata.synthetic_days /tmp/fake-adsblol

Writes ``segments-*.parquet`` and ``report-*.json`` files that look exactly like the real
ones (random helicopter and light-aircraft flights over Central Florida), so every E5.2-E5.5
command can be tried with ``--data /tmp/fake-adsblol`` before the real download finishes.
Never put them in ``backend/data/adsblol``.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from aerolink.realdata.fetch import write_day
from aerolink.realdata.segments import Segment


def make_days(out: Path, days: int = 5, flights_per_day: int = 25, seed: int = 0) -> list[str]:
    rng = np.random.default_rng(seed)
    first = date(2026, 11, 10)
    written = []
    for d in range(days):
        day = first + timedelta(days=d)
        midnight = 1762750800 + d * 86400  # 2026-11-10 00:00 EST
        segments = []
        for k in range(flights_per_day):
            n = int(rng.integers(400, 1200))
            t0 = midnight + int(rng.integers(7 * 3600, 19 * 3600))
            lat, lon = rng.uniform(27.5, 28.6), rng.uniform(-82.6, -81.3)
            heading, speed, alt = rng.uniform(0, 360), rng.uniform(25, 70), rng.uniform(500, 2500)
            turn = 0.0
            rows = []
            for i in range(n):
                if rng.random() < 0.01:
                    turn = rng.normal(0, 1.5)
                if rng.random() < 0.02:
                    turn = 0.0
                heading = (heading + turn) % 360
                speed = float(np.clip(speed + rng.normal(0, 0.3), 22, 80))
                alt += rng.normal(0, 1)
                lat += speed * np.cos(np.radians(heading)) / 111_000
                lon += speed * np.sin(np.radians(heading)) / (111_000 * np.cos(np.radians(lat)))
                rows.append((t0 + i, lat, lon, alt, speed / 0.514444, heading, 0.0))
            a = np.array(rows)
            data = {c: a[:, j] for j, c in enumerate(["t", "lat", "lon", "alt_ft", "gs_kt", "track_deg", "vrate_fpm"])}
            rotor = k % 2 == 1
            segments.append(
                Segment(f"f{k:05x}", 0, None, "R44" if rotor else "C172", "A7" if rotor else "A1", None, data)
            )
        write_day(segments, Counter(segments_kept=len(segments)), day, "synthetic-test-data", out)
        written.append(day.isoformat())
    return written


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/fake-adsblol")
    print(f"wrote {len(make_days(target))} made-up days to {target}")
```

#### New file: `backend/tests/test_e52_benchmark.py`

*What this file does:* Checks the day split, the samples, the benchmark command, and the Chronos wrapper (using a stand-in for the model, so no download is needed).

Open the file (command):

```bash
code tests/test_e52_benchmark.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""E5.2: day split, real-data samples, benchmark CLI, Chronos-2 wrapper (no downloads)."""

import json
from pathlib import Path

import numpy as np
import pytest
import torch

from aerolink.forecast.chronos import ChronosForecaster
from aerolink.realdata.benchmark import main
from aerolink.realdata.samples import build_position_samples, list_days, split_days
from aerolink.realdata.synthetic_days import make_days


@pytest.fixture(scope="module")
def fake_days(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("adsblol")
    make_days(out, days=4, flights_per_day=6)
    return out


def test_split_is_by_day_and_chronological():
    train, test = split_days(["2026-01-03", "2026-01-01", "2026-01-02", "2026-01-04"])
    assert train == ["2026-01-01", "2026-01-02", "2026-01-03"] and test == ["2026-01-04"]
    with pytest.raises(ValueError):
        split_days(["2026-01-01"])


def test_samples_have_history_and_future(fake_days: Path):
    days = list_days(fake_days)
    s = build_position_samples(days[:1], fake_days)
    assert len(s) > 0
    assert s.history.shape[1:] == (60, 3)
    assert s.future.shape[1:] == (4, 3)
    assert set(s.vehicle) <= {"rotor", "light"}
    assert np.isfinite(s.future[:, 0, 0]).all()  # 30 s ahead always exists


def test_benchmark_cv_writes_results(fake_days: Path, tmp_path: Path):
    out = tmp_path / "e52.json"
    assert main(["--data", str(fake_days), "--forecasters", "cv", "--out", str(out)]) == 0
    report = json.loads(out.read_text())
    rows = report["results"]["cv"]
    assert [r["horizon_s"] for r in rows] == [30, 60, 120, 300]
    assert rows[0]["fde_mean_m"] < rows[-1]["fde_mean_m"]  # error grows with horizon
    assert "ODbL" in report["attribution"]


class FakeChronos2:
    """Stands in for Chronos2Pipeline.predict_quantiles: straight-line forecast, +-10% spread."""

    def predict_quantiles(self, context, prediction_length, quantile_levels):
        out = []
        for series in context:  # (V, T)
            step = series[:, -1] - series[:, -2]
            h = torch.arange(1, prediction_length + 1, dtype=torch.float32)
            mid = series[:, -1:] + step[:, None] * h
            q = torch.stack([mid - 0.1 * mid.abs(), mid, mid + 0.1 * mid.abs()], dim=-1)
            out.append(q)
        return out, [o[..., 1] for o in out]


def test_chronos_position_batch_uses_quantiles():
    f = ChronosForecaster(None)
    f._pipeline = FakeChronos2()
    track = np.array([[28.0 + i * 1e-4, -82.0, 300.0] for i in range(60)])  # flying north ~11 m/s
    preds = f.predict_position_batch([track, track], [30, 300])
    assert len(preds) == 2 and [p.horizon_s for p in preds[0]] == [30, 300]
    p = preds[0][0]
    assert p.north_p10 <= p.north_p50 <= p.north_p90
    assert 300 < p.north_p50 < 360 and abs(p.east_p50) < 1  # ~11 m/s x 30 s, relative to the LAST fix


def test_time_to_cover_uses_profile_then_cruise():
    profile = np.full(10, 20.0)
    assert ChronosForecaster._time_to_cover(profile, 100.0, 50.0) == pytest.approx(5.0)
    assert ChronosForecaster._time_to_cover(profile, 300.0, 50.0) == pytest.approx(12.0)
```

### 6c. Run the checks

Command:

```bash
uv run ruff format
uv run ruff check --fix
uv run pytest -q tests/test_e52_benchmark.py
```

Expect **`5 passed`** at the end.

### 6d. Try it with made-up data

Command:

```bash
uv run python -m aerolink.realdata.synthetic_days /tmp/fake-adsblol
uv run python -m aerolink.realdata.benchmark --data /tmp/fake-adsblol --forecasters cv --out /tmp/e52-test.json
```

*What it does:* writes 5 made-up days to `/tmp/fake-adsblol` (outside the project, so they can never be committed) and runs the benchmark with the simple `cv` forecaster. You'll see a table like:

```text
forecaster     horizon       n  FDE mean   median      p90      ADE     cov E/N
cv                 30s    1234      160m      41m     514m     160m     47%/44%
cv                 60s    1184      561m     159m    1645m     361m     42%/39%
...
```

Errors should grow with the horizon. That's all this step checks.

### 6e. Run the real benchmark

**Wait until the 14-day download (5f) has finished.** Then command:

```bash
uv run python -m aerolink.realdata.benchmark --forecasters cv chronos
```

*What it does:* splits your real days, builds the held-out samples (up to 5,000, picked at random with a fixed seed so anyone gets the same set), and runs both forecasters. The first `chronos` run downloads the Chronos-2 weights from Hugging Face (about 100 MB, once). On a 4-core Codespace Chronos takes roughly **5–20 minutes**; `cv` takes seconds.

What to look at in the table:
- **FDE grows with horizon** for both: predicting 5 minutes ahead is harder than 30 seconds.
- **Coverage (cov E/N):** `cv` uses fixed-width ranges, so its coverage is usually far from 80%. Chronos-2's ranges come from the model.
- Write down which forecaster is better at each horizon. Whatever the result, it's the real result: don't change code to make one win.

If Chronos fails with a download or network error, run the same command again (downloads sometimes hiccup). If it keeps failing, run with `--forecasters cv` only, commit that, and tell Joshua.

### 6f. Commit

Command:

```bash
git add src/aerolink/forecast/chronos.py src/aerolink/forecast/features.py src/aerolink/realdata/samples.py src/aerolink/realdata/benchmark.py src/aerolink/realdata/synthetic_days.py tests/test_e52_benchmark.py eval-results/realdata-e5.2.json
git commit -F - <<'EOF'
feat: E5.2 real-data benchmark (cv vs chronos, position head)

* feat: chronological day split and position samples from adsb.lol segments
* feat: benchmark CLI: FDE/ADE per horizon and p10-p90 coverage
* fix: Chronos-2 wrapper uses the Chronos-2 API, batches, offsets from the current fix
* test: synthetic flight days for tests and dry runs
EOF
git push
```

**E5.2 done.**

---

## Part 7: E5.3 Trained Forecaster (`aerolink-gbm`)

**What you're building:** AeroLink's own forecaster, `aerolink-gbm`. It learns from thousands of simulated flights *and* the real training days.

**How it works, in plain words:**
- It doesn't predict positions from scratch. It starts from the simple constant-velocity guess (`cv`) and learns the **correction**: how much further ahead or behind, left or right, up or down the aircraft really ends up. That's easier to learn and never worse than starting from nothing.
- It uses **LightGBM**, a fast tree-based model that's very good with table-like inputs (speed, turn rate, vehicle type, distance to the next junction, time of day, traffic ahead...). Real flights don't fly AeroLink corridors, so their corridor inputs are left empty; LightGBM handles empty values natively, so one model trains on both.
- For every horizon it trains three models: a low guess (p10), a middle guess (p50) and a high guess (p90).
- **MAPIE** then checks those ranges on flights the model never trained on and stretches or shrinks them until they really contain the truth about 80% of the time. That's what lets AeroLink say "arrive 14:20, likely 14:17–14:26" and mean it.
- A second part, the **ETA head**, predicts how long the rest of a simulated flight will really take, compared to the simple estimate. The route engine uses it for arrival windows.

**Data split:** simulated flights from seeds 1–6 train the model; seeds 101–102 are held out. Real flights use the E5.2 day split. Within the training data, 20% of flights are kept back for MAPIE's calibration.

### 7a. Create the new files

#### New file: `backend/src/aerolink/forecast/simdata.py`

*What this file does:* Runs the scenarios with several seeds and turns the simulator's second-by-second flights into training samples for both heads (position and ETA).

Open the file (command):

```bash
code src/aerolink/forecast/simdata.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Training samples from simulated flights (PLAN E5.3).

Runs the scenarios with several seeds and turns the noise-free per-second state
(``SimulationResult.truth``) into:

* position samples, the same shape as the real ones (``realdata.samples.PositionSamples``),
  plus graph context (distance to the next node, turn there, route left);
* ETA samples: features and the true time to reach the destination, for every 30 s of
  every completed flight. "Reach" means arriving at the site (holding or landing starts),
  so pad waits are left to the capacity model and not counted twice.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

import numpy as np

from aerolink.contracts import VehicleType
from aerolink.forecast.features import (
    ETA_FEATURES,
    HISTORY_S,
    POSITION_HORIZONS_S,
    cv_speed,
    eta_row,
    to_local,
)
from aerolink.network import Network
from aerolink.realdata.samples import PositionSamples
from aerolink.sim import load_scenario, run_simulation
from aerolink.sim.runner import SimulationResult
from aerolink.vehicle import vehicle_cfg

AIRBORNE = {"climbing", "cruise", "descending", "holding", "landing"}
AT_SITE = {"holding", "landing"}


@dataclass
class EtaSamples:
    X: np.ndarray  # (N, len(ETA_FEATURES))
    y_log_ratio: np.ndarray  # (N,) log(actual remaining time / CV remaining time)
    cv_remaining_s: np.ndarray  # (N,)
    actual_remaining_s: np.ndarray  # (N,)
    group: np.ndarray  # (N,) flight id, for splitting

    def subset(self, mask: np.ndarray) -> EtaSamples:
        return EtaSamples(
            self.X[mask],
            self.y_log_ratio[mask],
            self.cv_remaining_s[mask],
            self.actual_remaining_s[mask],
            self.group[mask],
        )

    @staticmethod
    def concat(parts: list[EtaSamples]) -> EtaSamples:
        if not parts:
            return EtaSamples(np.zeros((0, len(ETA_FEATURES))), np.zeros(0), np.zeros(0), np.zeros(0), np.zeros(0, str))
        return EtaSamples(*(np.concatenate([getattr(p, f) for p in parts]) for f in EtaSamples.__dataclass_fields__))


def run_scenarios(network: Network, scenario_files: list[Path], seeds: list[int]) -> list[SimulationResult]:
    return [run_simulation(network, load_scenario(f), seed=s) for f in scenario_files for s in seeds]


def is_junction(network: Network, node: str) -> bool:
    return network.graph.degree(node) >= 3


def _bearing(network: Network, a: str, b: str) -> float:
    na, nb = network.nodes[a], network.nodes[b]
    e, n = to_local(np.array([nb["lat"]]), np.array([nb["lon"]]), na["lat"], na["lon"])
    return math.degrees(math.atan2(float(e[0]), float(n[0]))) % 360.0


def graph_context(network: Network, path: list[str], lat: float, lon: float) -> tuple[dict[str, float], int]:
    """Graph features for an aircraft at (lat, lon) flying ``path``; also the index of the next node."""
    xs = np.array([network.nodes[n]["lon"] for n in path])
    ys = np.array([network.nodes[n]["lat"] for n in path])
    e, n = to_local(ys, xs, lat, lon)  # nodes relative to the aircraft
    best_i, best_d = 1, math.inf
    for i in range(1, len(path)):  # nearest leg (straight line between nodes)
        ax, ay, bx, by = e[i - 1], n[i - 1], e[i], n[i]
        dx, dy = bx - ax, by - ay
        u = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
        d = math.hypot(ax + u * dx, ay + u * dy)
        if d < best_d:
            best_i, best_d = i, d
    dist_next = float(math.hypot(e[best_i], n[best_i]))
    turn = 0.0
    if best_i + 1 < len(path):
        a = _bearing(network, path[best_i - 1], path[best_i])
        b = _bearing(network, path[best_i], path[best_i + 1])
        turn = abs((b - a + 180.0) % 360.0 - 180.0)
    remaining = dist_next + network.route_length_m(path[best_i:])
    return {"dist_next_node_m": dist_next, "turn_at_next_deg": turn, "remaining_route_m": remaining}, best_i


def _path_history(result: SimulationResult) -> dict[str, list[tuple[float, list[str] | None]]]:
    """Per aircraft: (time, current path or None) whenever the route changes."""
    hist: dict[str, list[tuple[float, list[str] | None]]] = {}
    for e in result.events:
        aid = e.get("aircraft_id")
        if aid is None:
            continue
        if e["kind"] == "departure":
            hist.setdefault(aid, []).append((e["t"], list(e["path"])))
        elif e["kind"] == "suggestion" and e.get("accepted"):
            hist.setdefault(aid, []).append((e["t"], list(e["path"])))
        elif e["kind"] == "arrival":
            hist.setdefault(aid, []).append((e["t"], None))
    return hist


def _path_at(changes: list[tuple[float, list[str] | None]], t: float) -> list[str] | None:
    current = None
    for tc, p in changes:
        if tc > t:
            break
        current = p
    return current


def _tracks(result: SimulationResult) -> dict[str, dict[str, np.ndarray]]:
    by: dict[str, list[dict]] = {}
    for r in result.truth:
        by.setdefault(r["aircraft_id"], []).append(r)
    out = {}
    for aid, rows in by.items():
        rows.sort(key=lambda r: r["t"])
        out[aid] = {
            "t": np.array([r["t"] for r in rows]),
            "xyz": np.array([[r["lat"], r["lon"], r["alt_msl_m"]] for r in rows]),
            "speed": np.array([r["velocity_mps"] for r in rows]),
            "status": np.array([r["status"] for r in rows]),
        }
    return out


def _vehicle(result: SimulationResult, aid: str) -> str | None:
    ac = result.final_world.aircraft.get(aid)
    return ac.vehicle_type if ac is not None else None


def _start_hour(result: SimulationResult) -> float:
    start = datetime.fromisoformat(result.scenario.start_time)
    return start.hour + start.minute / 60.0


def add_noise(xyz: np.ndarray, rng: np.random.Generator, sigma_m: float) -> np.ndarray:
    """Horizontal GPS-like noise (metres), like the simulator's telemetry noise."""
    out = xyz.copy()
    out[:, 0] += np.degrees(rng.normal(0.0, sigma_m, len(xyz)) / 6_371_008.8)
    out[:, 1] += np.degrees(rng.normal(0.0, sigma_m, len(xyz)) / (6_371_008.8 * math.cos(math.radians(xyz[0, 0]))))
    return out


def position_samples(
    results: list[SimulationResult], network: Network, stride_s: int = 30, noise_m: float = 5.0, seed: int = 0
) -> tuple[PositionSamples, list[dict[str, float]]]:
    """Position samples from every airborne stretch of every simulated aircraft (1 s steps).

    The truth is noise-free, so ``noise_m`` of position noise is added (5 m, as in the
    scenarios' telemetry), like a real receiver would see it.
    """
    horizons = POSITION_HORIZONS_S
    rng = np.random.default_rng(seed)
    rows: dict[str, list] = {k: [] for k in ["seg", "t", "veh", "hour", "hist", "fut", "graph"]}
    for res in results:
        assert res.scenario.dt_s == 1.0, "position samples assume 1 s steps"
        paths = _path_history(res)
        h0 = _start_hour(res)
        for aid, tr in _tracks(res).items():
            if noise_m > 0:
                tr["xyz"] = add_noise(tr["xyz"], rng, noise_m)
            air = np.isin(tr["status"], list(AIRBORNE | {"background"}))
            starts = np.flatnonzero(air & ~np.r_[False, air[:-1]])
            ends = np.flatnonzero(air & ~np.r_[air[1:], False]) + 1
            for a, b in zip(starts, ends, strict=True):
                for i in range(a + HISTORY_S - 1, b - min(horizons), stride_s):
                    fut = np.full((len(horizons), 3), np.nan)
                    for j, h in enumerate(horizons):
                        if i + h < b:
                            fut[j] = tr["xyz"][i + h]
                    path = _path_at(paths.get(aid, []), float(tr["t"][i]))
                    graph: dict[str, float] = {}
                    if path and len(path) >= 2:
                        graph, _ = graph_context(network, path, float(tr["xyz"][i, 0]), float(tr["xyz"][i, 1]))
                    rows["seg"].append(f"{res.scenario.name}-{res.seed}-{aid}-{a}")
                    rows["t"].append(float(tr["t"][i]))
                    rows["veh"].append(_vehicle(res, aid) or "")
                    rows["hour"].append((h0 + float(tr["t"][i]) / 3600.0) % 24.0)
                    rows["hist"].append(tr["xyz"][i - HISTORY_S + 1 : i + 1])
                    rows["fut"].append(fut)
                    rows["graph"].append(graph)
    n = len(rows["t"])
    samples = PositionSamples(
        day=np.array(["sim"] * n),
        segment_id=np.array(rows["seg"]),
        t=np.array(rows["t"]),
        vehicle=np.array(rows["veh"]),
        hour=np.array(rows["hour"]),
        history=np.stack(rows["hist"]) if n else np.zeros((0, HISTORY_S, 3)),
        future=np.stack(rows["fut"]) if n else np.zeros((0, len(horizons), 3)),
        horizons_s=horizons,
    )
    return samples, rows["graph"]


def eta_samples(results: list[SimulationResult], network: Network, stride_s: int = 30) -> EtaSamples:
    """ETA samples from every completed, undiverted simulated flight."""
    X, y, cv_s, act_s, grp = [], [], [], [], []
    for res in results:
        tracks = _tracks(res)
        paths = _path_history(res)
        h0 = _start_hour(res)
        departures = {e["flight_id"]: e for e in res.events if e["kind"] == "departure"}
        active: list[tuple[float, float, str]] = []  # (dep t, arr t, aircraft) for traffic-ahead
        arrivals = [e for e in res.events if e["kind"] == "arrival" and e.get("flight_id") in departures]
        for arr in arrivals:
            active.append((departures[arr["flight_id"]]["t"], arr["t"], arr["aircraft_id"]))
        for arr in arrivals:
            if arr.get("diverted") or arr.get("destination_changed"):
                continue
            dep = departures[arr["flight_id"]]
            aid, vehicle = arr["aircraft_id"], _vehicle(res, arr["aircraft_id"])
            if vehicle is None:
                continue
            cruise = vehicle_cfg(cast(VehicleType, vehicle)).cruise_speed_mps
            tr = tracks[aid]
            in_flight = (tr["t"] > dep["t"]) & (tr["t"] <= arr["t"])
            at_site = np.flatnonzero(in_flight & np.isin(tr["status"], list(AT_SITE)))
            reach_t = float(tr["t"][at_site[0]]) if len(at_site) else float(arr["t"])
            idx = np.flatnonzero(in_flight & (tr["t"] <= reach_t - stride_s))
            for i in idx[::stride_s]:
                t = float(tr["t"][i])
                path = _path_at(paths.get(aid, []), t)
                if not path or path[-1] != arr["node"]:
                    continue
                graph, nxt = graph_context(network, path, float(tr["xyz"][i, 0]), float(tr["xyz"][i, 1]))
                remaining_nodes = path[nxt:]
                speed = float(tr["speed"][i])
                cv_rem = graph["remaining_route_m"] / cv_speed(cruise, speed)
                traffic = 0
                for d_t, a_t, other in active:
                    if other == aid or not (d_t <= t < a_t):
                        continue
                    op = _path_at(paths.get(other, []), t)
                    if op and set(op) & set(remaining_nodes):
                        traffic += 1
                X.append(
                    eta_row(
                        vehicle,
                        (h0 + t / 3600.0) % 24.0,
                        graph["remaining_route_m"],
                        cv_rem,
                        speed,
                        cruise,
                        len(remaining_nodes),
                        sum(is_junction(network, n) for n in remaining_nodes[:-1]),
                        float(traffic),
                    )
                )
                actual = max(1.0, reach_t - t)
                y.append(math.log(actual / max(1.0, cv_rem)))
                cv_s.append(cv_rem)
                act_s.append(actual)
                grp.append(f"{res.scenario.name}-{res.seed}-{arr['flight_id']}")
    return EtaSamples(
        np.array(X, dtype=float).reshape(-1, len(ETA_FEATURES)),
        np.array(y),
        np.array(cv_s),
        np.array(act_s),
        np.array(grp, dtype=str),
    )
```

#### New file: `backend/src/aerolink/forecast/gbm.py`

*What this file does:* The `aerolink-gbm` forecaster: training helpers (LightGBM quantile models + MAPIE calibration) and the class the rest of AeroLink uses to get predictions.

Open the file (command):

```bash
code src/aerolink/forecast/gbm.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""``aerolink-gbm``: LightGBM quantile forecaster calibrated with MAPIE (PLAN E5.3, ARCHITECTURE §7.1).

Two heads, both residuals over constant velocity (``cv``):

* position head: for each horizon (30/60/120/300 s) and axis (along-track, cross-track, up),
  a LightGBM model per quantile (p10, p50, p90) predicts where the aircraft will be
  relative to the CV point. MAPIE's ``ConformalizedQuantileRegressor`` widens or narrows
  the p10-p90 interval on held-back calibration data so it really covers ~80%.
* ETA head: one calibrated model predicts log(actual time / CV time) to reach the
  destination; each node's ETA is the CV time to that node scaled by the predicted ratio.

The trained models live in ``backend/models/aerolink-gbm/`` (``model.joblib`` +
``manifest.json`` with seeds, data and library versions). Train with
``uv run python -m aerolink.forecast.train_gbm``.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any, cast

import joblib
import lightgbm as lgb
import numpy as np
from mapie.regression import ConformalizedQuantileRegressor

from aerolink.contracts import VehicleType
from aerolink.forecast.features import (
    POSITION_HORIZONS_S,
    cv_point,
    cv_speed,
    cv_state,
    eta_row,
    from_track_frame,
    position_row,
)
from aerolink.forecast.interface import EtaPrediction, Forecaster, NodeEta, PositionPrediction
from aerolink.network import Network
from aerolink.vehicle import vehicle_cfg

MODEL_DIR = Path(__file__).resolve().parents[3] / "models" / "aerolink-gbm"
AXES = ("along", "cross", "up")


class _QuietMapie(logging.Filter):
    """MAPIE logs an INFO line on every call when p10 > p90 for some rows; we sort them ourselves."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "ill-sorted" not in record.getMessage()


logging.getLogger().addFilter(_QuietMapie())
COVERAGE_TARGET = 0.80  # p10-p90


def make_quantile_model(alpha: float, seed: int) -> lgb.LGBMRegressor:
    return lgb.LGBMRegressor(
        objective="quantile",
        alpha=alpha,
        n_estimators=300,
        learning_rate=0.05,
        num_leaves=31,
        min_child_samples=20,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.9,
        random_state=seed,
        verbose=-1,
    )


def fit_calibrated(
    X_train: np.ndarray, y_train: np.ndarray, X_cal: np.ndarray, y_cal: np.ndarray, seed: int
) -> ConformalizedQuantileRegressor:
    """Three quantile models (p10, p90, p50, in MAPIE's order) + conformal calibration."""
    models = [make_quantile_model(a, seed).fit(X_train, y_train) for a in (0.1, 0.9, 0.5)]
    cqr = ConformalizedQuantileRegressor(cast(Any, models), confidence_level=COVERAGE_TARGET, prefit=True)
    cqr.conformalize(X_cal, y_cal)
    return cqr


def predict_q(model: ConformalizedQuantileRegressor, X: np.ndarray) -> np.ndarray:
    """(N, 3) calibrated p10, p50, p90."""
    p50, pis = model.predict_interval(X)
    lo, hi = pis[:, 0, 0], pis[:, 1, 0]
    return np.column_stack([np.minimum(lo, p50), p50, np.maximum(hi, p50)])


class GBMForecaster(Forecaster):
    def __init__(self, network: Network | None, bundle: dict[str, Any]):
        self.network = network
        self.position: dict[tuple[int, str], ConformalizedQuantileRegressor] = bundle["position"]
        self.eta: ConformalizedQuantileRegressor | None = bundle.get("eta")
        self.manifest: dict = bundle.get("manifest", {})
        # Optional context the route engine sets before asking for ETAs (A2+): number of
        # planned flights sharing a node with the path being scored.
        self.traffic_ahead: float = math.nan
        self.local_hour: float | None = None  # local hour of day, if the caller knows it

    @property
    def name(self) -> str:
        return "aerolink-gbm"

    @classmethod
    def load(cls, network: Network | None, model_dir: Path = MODEL_DIR) -> GBMForecaster:
        path = model_dir / "model.joblib"
        if not path.exists():
            raise FileNotFoundError(f"{path} not found: train it with `uv run python -m aerolink.forecast.train_gbm`")
        return cls(network, joblib.load(path))

    # --- position head -------------------------------------------------------------------

    def predict_position_batch(
        self,
        tracks: list[np.ndarray],
        horizons_s: list[int] | None = None,
        vehicles: list[str] | None = None,
        hours: list[float] | None = None,
        graphs: list[dict[str, float]] | None = None,
    ) -> list[list[PositionPrediction]]:
        horizons = horizons_s or POSITION_HORIZONS_S
        tracks = [np.asarray(t, dtype=float) for t in tracks]
        n = len(tracks)
        out: list[list[PositionPrediction | None]] = [[None] * len(horizons) for _ in range(n)]
        for j, h in enumerate(horizons):
            X = np.array(
                [
                    position_row(
                        tracks[i],
                        h,
                        vehicles[i] if vehicles else None,
                        hours[i] if hours else None,
                        graphs[i] if graphs else None,
                    )
                    for i in range(n)
                ]
            )
            q = {axis: predict_q(self.position[(h, axis)], X) for axis in AXES}
            for i in range(n):
                ce, cn = cv_point(tracks[i], h)
                _, course = cv_state(tracks[i])
                # p50, then the bounding box of the rotated along/cross p10-p90 rectangle
                e50, n50 = from_track_frame(q["along"][i, 1], q["cross"][i, 1], course)
                corners = [
                    from_track_frame(a, c, course)
                    for a in (q["along"][i, 0], q["along"][i, 2])
                    for c in (q["cross"][i, 0], q["cross"][i, 2])
                ]
                es = [c[0] for c in corners]
                ns = [c[1] for c in corners]
                out[i][j] = PositionPrediction(
                    horizon_s=h,
                    east_p10=ce + min(es),
                    east_p50=ce + e50,
                    east_p90=ce + max(es),
                    north_p10=cn + min(ns),
                    north_p50=cn + n50,
                    north_p90=cn + max(ns),
                    up_p10=float(q["up"][i, 0]),
                    up_p50=float(q["up"][i, 1]),
                    up_p90=float(q["up"][i, 2]),
                )
        return [[p for p in row if p is not None] for row in out]

    def predict_position(
        self,
        track: list[tuple[float, float, float]],
        t_s: float,
        vehicle_type: VehicleType,
        horizons_s: list[int] | None = None,
    ) -> list[PositionPrediction]:
        if len(track) < 2:
            return [PositionPrediction(h, -10.0, 0.0, 10.0, -10.0, 0.0, 10.0, -5.0, 0.0, 5.0) for h in horizons_s or []]
        return self.predict_position_batch([np.asarray(track)], horizons_s, vehicles=[vehicle_type])[0]

    # --- ETA head ------------------------------------------------------------------------

    def predict_eta(
        self,
        path: list[str],
        origin_t_s: float,
        vehicle_type: VehicleType,
        track: list[tuple[float, float, float]] | None = None,
        current_speed_mps: float | None = None,
    ) -> EtaPrediction:
        assert self.network is not None and self.eta is not None, "ETA head needs the network and an ETA model"
        cruise = vehicle_cfg(vehicle_type).cruise_speed_mps
        speed = cv_speed(cruise, current_speed_mps)
        total_m = self.network.route_length_m(path)
        cv_total = total_m / max(1.0, speed)
        merges = sum(self.network.graph.degree(n) >= 3 for n in path[1:-1])
        row = eta_row(
            vehicle_type,
            self.local_hour,
            total_m,
            cv_total,
            # not airborne yet (no speed): assume cruise, as the training flights were in the air
            current_speed_mps if current_speed_mps is not None and current_speed_mps > 1.0 else cruise,
            cruise,
            len(path),
            merges,
            self.traffic_ahead,
        )
        r10, r50, r90 = np.exp(predict_q(self.eta, np.array([row]))[0])
        etas: list[NodeEta] = []
        cumul_m = 0.0
        for i, node in enumerate(path):
            if i > 0:
                cumul_m += self.network.edges[(path[i - 1], node)].length_m
            cv_t = cumul_m / max(1.0, speed)
            etas.append(
                NodeEta(
                    node=node,
                    eta_p10=origin_t_s + cv_t * float(r10),
                    eta_p50=origin_t_s + cv_t * float(r50),
                    eta_p90=origin_t_s + cv_t * float(r90),
                )
            )
        return EtaPrediction(path=path, origin_t_s=origin_t_s, etas=etas)
```

#### New file: `backend/src/aerolink/forecast/train_gbm.py`

*What this file does:* The command you run. Builds the data, trains and calibrates both heads, saves the model with a manifest of its seeds and data, and scores `cv`, `aerolink-gbm` and (optionally) `chronos` on held-out simulated and real data.

Open the file (command):

```bash
code src/aerolink/forecast/train_gbm.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Train, calibrate and evaluate ``aerolink-gbm`` (PLAN E5.3).

    uv run python -m aerolink.forecast.train_gbm                 # simulated + real data
    uv run python -m aerolink.forecast.train_gbm --with-chronos  # also score Chronos-2
    uv run python -m aerolink.forecast.train_gbm --no-real       # simulated data only

Data:
* simulated: every scenario in ``backend/scenarios`` with training seeds (default 1-6)
  and held-out seeds (101, 102) that are never trained on;
* real: the E5.1 adsb.lol days, split by day exactly as in E5.2 (latest ~30% held out).

Training samples are split again by flight: 80% fit the quantile models, 20% calibrate
them (MAPIE). Results go to ``backend/eval-results/aerolink-gbm-e5.3.json`` and the model to
``backend/models/aerolink-gbm/`` with a manifest of its seeds and data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from aerolink.forecast.cv import CVForecaster
from aerolink.forecast.features import POSITION_FEATURES, position_row, position_targets
from aerolink.forecast.gbm import AXES, COVERAGE_TARGET, MODEL_DIR, GBMForecaster, fit_calibrated, predict_q
from aerolink.forecast.simdata import EtaSamples, eta_samples, position_samples, run_scenarios
from aerolink.network import Network
from aerolink.realdata.benchmark import predict_all, score
from aerolink.realdata.samples import DATA_DIR, PositionSamples, build_position_samples, list_days, split_days

BACKEND = Path(__file__).resolve().parents[3]
DEFAULT_OUT = BACKEND / "eval-results" / "aerolink-gbm-e5.3.json"
ETA_BUCKETS_MIN = [5, 10, 20, 30, 45]


def _group_split(groups: np.ndarray, frac: float, seed: int) -> np.ndarray:
    """Boolean mask: True for the ``frac`` share of groups (flights) picked at random."""
    uniq = np.unique(groups)
    rng = np.random.default_rng(seed)
    picked = set(rng.choice(uniq, max(1, round(len(uniq) * frac)), replace=False)) if len(uniq) > 1 else set()
    return np.array([g in picked for g in groups], dtype=bool)


def position_xy(
    samples: PositionSamples, graphs: list[dict[str, float]] | None, rng: np.random.Generator, mask_hour: float = 0.0
) -> dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Per horizon: (X, Y[along, cross, up], group) for samples whose future is known."""
    out = {}
    for j, h in enumerate(samples.horizons_s):
        X, Y, G = [], [], []
        for i in range(len(samples)):
            if np.isnan(samples.future[i, j, 0]):
                continue
            hour = None if rng.random() < mask_hour else float(samples.hour[i])
            X.append(
                position_row(samples.history[i], h, samples.vehicle[i] or None, hour, graphs[i] if graphs else None)
            )
            Y.append(position_targets(samples.history[i], samples.future[i, j], h))
            G.append(samples.segment_id[i])
        out[h] = (
            np.array(X, dtype=float).reshape(-1, len(POSITION_FEATURES)),
            np.array(Y, dtype=float).reshape(-1, 3),
            np.array(G, dtype=str),
        )
    return out


def _merge(parts: list[dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]]]):
    keys = parts[0].keys()
    return {h: tuple(np.concatenate([p[h][k] for p in parts]) for k in range(3)) for h in keys}


def frame_coverage(models, data) -> list[dict]:
    """p10-p90 coverage in the calibrated frame (along / cross / up), per horizon."""
    rows = []
    for h, (X, Y, _) in data.items():
        if not len(X):
            continue
        row: dict = {"horizon_s": h, "n": len(X)}
        for k, axis in enumerate(AXES):
            q = predict_q(models[(h, axis)], X)
            row[f"coverage_{axis}"] = round(float(((Y[:, k] >= q[:, 0]) & (Y[:, k] <= q[:, 2])).mean()), 3)
        rows.append(row)
    return rows


def eta_report(model, data: EtaSamples) -> list[dict]:
    """ETA error and coverage per horizon bucket (CV remaining time), CV vs aerolink-gbm."""
    q = np.exp(predict_q(model, data.X)) * data.cv_remaining_s[:, None]
    cv_half = np.maximum(30.0, 0.08 * data.cv_remaining_s)
    rows = []
    lo = 0.0
    for b in ETA_BUCKETS_MIN:
        m = (data.cv_remaining_s > lo * 60) & (data.cv_remaining_s <= b * 60)
        lo = b
        if not m.any():
            rows.append({"horizon_max_min": b, "n": 0})
            continue
        act = data.actual_remaining_s[m]
        rows.append(
            {
                "horizon_max_min": b,
                "n": int(m.sum()),
                "cv_mae_s": round(float(np.abs(act - data.cv_remaining_s[m]).mean()), 1),
                "gbm_mae_s": round(float(np.abs(act - q[m, 1]).mean()), 1),
                "cv_coverage": round(float((np.abs(act - data.cv_remaining_s[m]) <= cv_half[m]).mean()), 3),
                "gbm_coverage": round(float(((act >= q[m, 0]) & (act <= q[m, 2])).mean()), 3),
            }
        )
    return rows


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--network", type=Path, default=BACKEND.parent / "airspace" / "central-fl-v1.geojson")
    ap.add_argument("--scenarios", type=Path, default=BACKEND / "scenarios")
    ap.add_argument("--train-seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5, 6])
    ap.add_argument("--test-seeds", type=int, nargs="+", default=[101, 102])
    ap.add_argument("--data", type=Path, default=DATA_DIR)
    ap.add_argument("--no-real", action="store_true", help="train on simulated data only")
    ap.add_argument("--with-chronos", action="store_true", help="also score Chronos-2 (downloads weights once)")
    ap.add_argument("--seed", type=int, default=42, help="LightGBM and split seed")
    ap.add_argument("--max-eval-samples", type=int, default=3000)
    ap.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args(argv)
    rng = np.random.default_rng(a.seed)
    started = time.monotonic()

    network = Network.from_geojson(a.network)
    scenario_files = sorted(a.scenarios.glob("*.yaml"))
    print(
        f"simulating {len(scenario_files)} scenarios x {len(a.train_seeds) + len(a.test_seeds)} seeds ...", flush=True
    )
    sim_train = run_scenarios(network, scenario_files, a.train_seeds)
    sim_test = run_scenarios(network, scenario_files, a.test_seeds)
    sim_pos_train, sim_graph_train = position_samples(sim_train, network)
    sim_pos_test, sim_graph_test = position_samples(sim_test, network)
    eta_train = eta_samples(sim_train, network)
    eta_test = eta_samples(sim_test, network)
    print(f"simulated: {len(sim_pos_train):,} position / {len(eta_train.y_log_ratio):,} ETA training samples")

    real_train = real_test = None
    days: dict[str, list[str]] = {"train": [], "held_out": []}
    if not a.no_real:
        all_days = list_days(a.data)
        if len(all_days) < 2:
            print("fewer than 2 real days found: run E5.1 first, or pass --no-real")
            return 1
        days["train"], days["held_out"] = split_days(all_days)
        real_train = build_position_samples(days["train"], a.data)
        real_test = build_position_samples(days["held_out"], a.data)
        print(f"real: {len(real_train):,} training samples ({len(days['train'])} days)")

    # --- position head ---
    parts = [position_xy(sim_pos_train, sim_graph_train, rng, mask_hour=0.1)]
    if real_train is not None:
        parts.append(position_xy(real_train, None, rng))
    pos = _merge(parts)
    pos_models = {}
    pos_cal = {}
    for h, (X, Y, G) in pos.items():
        cal = _group_split(G, 0.2, a.seed)
        pos_cal[h] = (X[cal], Y[cal], G[cal])
        for k, axis in enumerate(AXES):
            pos_models[(h, axis)] = fit_calibrated(X[~cal], Y[~cal, k], X[cal], Y[cal, k], a.seed)
        print(f"  position head {h:>3}s: {int((~cal).sum()):,} fit / {int(cal.sum()):,} calibration", flush=True)

    # --- ETA head ---
    hour_col = 1  # ETA_FEATURES index of "hour"; hidden on 10% of rows so missing hours are learnt
    Xe = eta_train.X.copy()
    Xe[rng.random(len(Xe)) < 0.1, hour_col] = np.nan
    Xe[rng.random(len(Xe)) < 0.1, -1] = np.nan  # traffic_ahead is not always known either
    cal = _group_split(eta_train.group, 0.2, a.seed)
    eta_model = fit_calibrated(Xe[~cal], eta_train.y_log_ratio[~cal], Xe[cal], eta_train.y_log_ratio[cal], a.seed)

    a.model_dir.mkdir(parents=True, exist_ok=True)
    model_path = a.model_dir / "model.joblib"
    manifest = {
        "model": "aerolink-gbm",
        "seed": a.seed,
        "sim_train_seeds": a.train_seeds,
        "sim_test_seeds": a.test_seeds,
        "scenarios": [f.stem for f in scenario_files],
        "real_days": days,
        "coverage_target": COVERAGE_TARGET,
        "samples": {
            "position_fit_and_calibration": {str(h): int(len(v[0])) for h, v in pos.items()},
            "eta": int(len(eta_train.y_log_ratio)),
        },
        "libraries": {p: version(p) for p in ["lightgbm", "mapie", "scikit-learn", "numpy"]},
    }
    joblib.dump({"position": pos_models, "eta": eta_model, "manifest": manifest}, model_path, compress=3)
    manifest["sha256"] = _sha256(model_path)
    (a.model_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {model_path} ({model_path.stat().st_size / 1e6:.1f} MB)")

    # --- evaluation on held-out data ---
    gbm = GBMForecaster.load(network, a.model_dir)
    forecasters = {"cv": CVForecaster(network), "aerolink-gbm": gbm}
    if a.with_chronos:
        from aerolink.forecast.chronos import ChronosForecaster

        forecasters["chronos"] = ChronosForecaster(network)

    def compare(samples: PositionSamples, graphs) -> dict:
        if a.max_eval_samples and len(samples) > a.max_eval_samples:
            keep = np.zeros(len(samples), dtype=bool)
            keep[np.random.default_rng(a.seed).choice(len(samples), a.max_eval_samples, replace=False)] = True
            samples = samples.subset(keep)
            graphs = [g for g, k in zip(graphs, keep, strict=True) if k] if graphs else None
        res = {}
        for name, f in forecasters.items():
            if isinstance(f, GBMForecaster):
                preds = f.predict_position_batch(
                    list(samples.history), samples.horizons_s, list(samples.vehicle), list(samples.hour), graphs
                )
            else:
                preds = predict_all(f, samples)
            res[name] = score(samples, preds)
        return res

    report: dict[str, Any] = {
        "manifest": manifest,
        "position_sim_held_out": compare(sim_pos_test, sim_graph_test),
        "position_real_held_out": compare(real_test, None) if real_test is not None and len(real_test) else None,
        "position_calibration_coverage_sim_held_out": frame_coverage(
            pos_models, position_xy(sim_pos_test, sim_graph_test, rng)
        ),
        "position_calibration_coverage_real_held_out": (
            frame_coverage(pos_models, position_xy(real_test, None, rng)) if real_test is not None else None
        ),
        "eta_sim_held_out": eta_report(eta_model, eta_test),
        "minutes": round((time.monotonic() - started) / 60, 1),
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=2) + "\n")

    print("\nPosition head, horizontal error at each horizon (mean, m) and p10-p90 coverage in the model's frame:")
    for label, key, cov_key in [
        ("simulated", "position_sim_held_out", "position_calibration_coverage_sim_held_out"),
        ("real", "position_real_held_out", "position_calibration_coverage_real_held_out"),
    ]:
        if not report[key]:
            continue
        cov = {r["horizon_s"]: r for r in report[cov_key]}
        for h_i, h in enumerate(sim_pos_test.horizons_s):
            errs = "  ".join(
                f"{n}={rows[h_i]['fde_mean_m']:.0f}" for n, rows in report[key].items() if rows[h_i].get("n")
            )
            c = cov.get(h, {})
            cv_txt = "/".join(f"{c.get('coverage_' + ax, 0):.0%}" for ax in AXES)
            print(f"  {label:<9} {h:>3}s  {errs}   coverage along/cross/up {cv_txt}")
    print("\nETA head (simulated held-out): mean absolute error and p10-p90 coverage")
    for r in report["eta_sim_held_out"]:
        if r["n"]:
            print(
                f"  <= {r['horizon_max_min']:>2} min  n={r['n']:<5} cv {r['cv_mae_s']:>6.0f}s ({r['cv_coverage']:.0%})"
                f"   gbm {r['gbm_mae_s']:>6.0f}s ({r['gbm_coverage']:.0%})"
            )
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

#### New file: `backend/tests/test_e53_gbm.py`

*What this file does:* Trains a tiny model end to end on made-up data and checks the files, the manifest and that predictions come out in order (p10 ≤ p50 ≤ p90).

Open the file (command):

```bash
code tests/test_e53_gbm.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""E5.3: train a small aerolink-gbm end to end and check its outputs."""

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from aerolink.forecast.features import from_track_frame, position_targets, to_track_frame
from aerolink.forecast.gbm import GBMForecaster
from aerolink.forecast.train_gbm import main
from aerolink.network import Network
from aerolink.realdata.synthetic_days import make_days

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def trained(tmp_path_factory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("gbm")
    data, scenarios, models = root / "adsblol", root / "scenarios", root / "model"
    make_days(data, days=3, flights_per_day=8)
    scenarios.mkdir()
    shutil.copy(BACKEND / "scenarios" / "i4_peak.yaml", scenarios)
    out = root / "e53.json"
    args = ["--data", str(data), "--scenarios", str(scenarios), "--train-seeds", "1", "--test-seeds", "2"]
    assert main([*args, "--model-dir", str(models), "--out", str(out)]) == 0
    return models, out


def test_track_frame_round_trip():
    for course in [0.0, 45.0, 200.0]:
        a, c = to_track_frame(30.0, -40.0, course)
        e, n = from_track_frame(a, c, course)
        assert e == pytest.approx(30.0) and n == pytest.approx(-40.0)


def test_straight_flight_has_zero_residual():
    hist = np.array([[28.0 + i * 1e-4, -82.0, 300.0] for i in range(60)])
    fut = np.array([28.0 + 89 * 1e-4, -82.0, 300.0])  # 30 s later, same speed
    along, cross, up = position_targets(hist, fut, 30)
    assert abs(along) < 1 and abs(cross) < 1 and up == 0


def test_model_files_and_manifest(trained):
    models, out = trained
    manifest = json.loads((models / "manifest.json").read_text())
    assert manifest["seed"] == 42 and manifest["sim_train_seeds"] == [1]
    assert len(manifest["sha256"]) == 64
    report = json.loads(out.read_text())
    assert report["position_real_held_out"]["aerolink-gbm"][0]["n"] > 0
    assert report["eta_sim_held_out"]


def test_gbm_forecaster_predicts_ordered_quantiles(trained, fl_network: Network):
    models, _ = trained
    f = GBMForecaster.load(fl_network, models)
    path = fl_network.shortest_paths("ev-lal", "ev-water-st", "passenger", k=1)[0]
    eta = f.predict_eta(path, 100.0, "passenger")
    assert [e.node for e in eta.etas] == path
    last = eta.etas[-1]
    assert 100.0 < last.eta_p10 <= last.eta_p50 <= last.eta_p90
    track = [(28.0 + i * 1e-4, -82.0, 300.0) for i in range(60)]
    for p in f.predict_position(track, 0.0, "passenger", [30, 120]):
        assert p.east_p10 <= p.east_p50 <= p.east_p90 and p.north_p10 <= p.north_p50 <= p.north_p90
```

### 7b. Run the checks

Command:

```bash
uv run ruff format
uv run ruff check --fix
uv run pytest -q tests/test_e53_gbm.py
```

Expect **`4 passed`** (this test trains a small model, so it takes a minute or two).

### 7c. Try it with made-up data

Command:

```bash
uv run python -m aerolink.forecast.train_gbm --data /tmp/fake-adsblol --train-seeds 1 2 --test-seeds 101 --model-dir /tmp/gbm-test --out /tmp/e53-test.json
```

*What it does:* a quick, small training run (about 2 minutes) into `/tmp`, so nothing in the project changes. It ends with two small tables (position head and ETA head). If it finishes with `wrote /tmp/e53-test.json`, everything is wired up.

### 7d. Train the real model

Command:

```bash
uv run python -m aerolink.forecast.train_gbm --with-chronos
```

*What it does:* simulates all 7 scenarios with 8 seeds, builds samples from them and from your real training days, trains 39 small models, calibrates them, saves everything to `backend/models/aerolink-gbm/`, then scores all three forecasters on the held-out data. Expect **15–40 minutes** on a 4-core Codespace (most of it is Chronos-2 scoring; leave out `--with-chronos` for a quick rehearsal).

> **Revised:** your real data is much bigger than the guide assumed (10 training days of about 1.6 million flight-seconds each, roughly 500,000 real training samples). Expect this step to take **longer, maybe 1 hour or more**, and to use several GB of memory. If it stops with `Killed` or `MemoryError`, push your work and switch to an **8-core** Codespace (see Troubleshooting). If it's still too slow there, stop and ask Joshua about capping the real training samples (decision D4 in `docs/Carla/plans/SIGNOFF.md`). Don't change the code on your own.

What it prints at the end (example):

```text
Position head, horizontal error at each horizon (mean, m) and p10-p90 coverage in the model's frame:
  simulated  30s  cv=159  aerolink-gbm=124  chronos=...   coverage along/cross/up 79%/83%/93%
  ...
  real       30s  cv=160  aerolink-gbm=113  chronos=...   coverage along/cross/up 80%/81%/71%
  ...
ETA head (simulated held-out): mean absolute error and p10-p90 coverage
  <=  5 min  n=484   cv     32s (58%)   gbm      8s (80%)
  ...
```

How to read it:
- **error columns:** smaller is better, compared at the same horizon.
- **coverage along/cross/up:** the share of held-out cases inside the calibrated p10–p90 range. The target is **80%**. Values in the 70s–80s mean the calibration is doing its job; much lower on real data means real flights behave differently from the training mix (worth noting for Joshua).
- **ETA head:** the error of the predicted remaining flight time, and how often the truth fell in the range. `cv` uses a fixed ±8% window; `gbm`'s is calibrated.

Check the model files are a sensible size. Command:

```bash
ls -lh models/aerolink-gbm
```

You should see `model.joblib` (roughly 10–30 MB) and `manifest.json`. Open the manifest (`code models/aerolink-gbm/manifest.json`) to see what it records: the seeds, the scenarios, the real days, the library versions and a fingerprint (`sha256`) of the model file. That's the "versioned with its training seeds" story.

### 7e. Add `aerolink-gbm` to the real-data benchmark

Now that the model exists, score it on the same held-out samples as E5.2. Command:

```bash
uv run python -m aerolink.realdata.benchmark --forecasters cv chronos aerolink-gbm
```

*What it does:* rewrites `eval-results/realdata-e5.2.json` with all three forecasters side by side. (Same samples and seed as before, so `cv` and `chronos` give the same numbers again.)

### 7f. Commit

Command:

```bash
git add src/aerolink/forecast/simdata.py src/aerolink/forecast/gbm.py src/aerolink/forecast/train_gbm.py tests/test_e53_gbm.py models/aerolink-gbm eval-results/aerolink-gbm-e5.3.json eval-results/realdata-e5.2.json pyproject.toml uv.lock
git commit -F - <<'EOF'
feat: E5.3 aerolink-gbm forecaster (LightGBM + MAPIE)

* feat: simulated training samples for position and ETA heads
* feat: residual-over-cv quantile models, conformalized to 80% p10-p90 coverage
* feat: train_gbm CLI with held-out sim and real evaluation vs cv and chronos
* data: model files with manifest (seeds, data, library versions, sha256)
* chore: add mapie
EOF
git push
```

**E5.3 done.**

---

## Part 8: E5.4 Demand Forecaster

**What you're building:** a forecaster for flights that **haven't been planned yet**. If a passenger usually books an Orlando-to-Tampa hop at 8:15 every weekday, AeroLink should expect it in the traffic picture before it's booked.

**The data is synthetic (made up), on purpose.** No public air-taxi demand data exists for the region. So the program generates 28 days of plausible demand and labels it `SYNTHETIC` everywhere (in the files, the results and the docs):
- **How much:** the FDOT AAM Business Plan (Nov 2025) forecasts 220,000 to 1.4 million AAM passenger trips in Florida in 2027. We take the low end and give Central Florida a 27% share (roughly its share of the state's population; an assumption, written down in the code): about **163 departures a day**, spread over the network's eVTOL sites by number of pads.
- **When:** the same morning and evening peaks the simulator uses, 30% quieter at weekends, drawn randomly (Poisson) per site per 15 minutes.
- **Medical:** one dispatch every 30 minutes across the region (the simulator's default), shared unevenly between helipads.

**What gets predicted and compared:**
- Departures per eVTOL site and dispatches per helipad, per 15 minutes, one day ahead: **Chronos-2** (with time of day as a hint) against the simple baselines from the plan: **mean rate**, **last schedule** (same time yesterday), **constant rate**, plus a stronger **time-of-day mean**.
- Where each passenger flight goes: **LightGBM** against a uniform guess.
- The route engine turns these into **expected flights** (probability × arrival time) and adds them to the traffic forecast (used in E5.7, config A3).

### 8a. Create the folder and files

Command:

```bash
mkdir -p src/aerolink/demand
```

#### New file: `backend/src/aerolink/demand/__init__.py`

*What this file does:* Marks the folder as a package and says, up front, that the data is synthetic.

Open the file (command):

```bash
code src/aerolink/demand/__init__.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Demand forecasting: flights that are not planned yet (PLAN E5.4, ARCHITECTURE §7.2).

All demand data here is SYNTHETIC: no public UAM demand data exists for the region.
"""
```

#### New file: `backend/src/aerolink/demand/synthetic.py`

*What this file does:* Generates the labelled synthetic history, scaled from FDOT's 2027 figure, and saves it as two Parquet files.

Open the file (command):

```bash
code src/aerolink/demand/synthetic.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Synthetic demand history (PLAN E5.4). LABELLED SYNTHETIC EVERYWHERE.

Passenger trips are scaled from the FDOT AAM Business Plan (Nov 2025), which forecasts
220 thousand to 1.4 million AAM passenger trips in Florida in 2027. We take the low end,
give Central Florida a 27% share (its rough share of the state's population, an
assumption), and spread the result over the network's eVTOL sites by pad count:

    220,000 x 0.27 / 365  ~= 163 passenger departures per day

Within a day, departures follow the simulator's own diurnal shape (morning and evening
peaks, ``aerolink.sim.demand.diurnal_rate_per_hour``), 30% lower at weekends, drawn as a
Poisson process per site per 15-minute bin. Each trip gets a destination from an
origin-, period- and distance-dependent preference (a gravity model with a fixed random
twist), so a model has something real to learn.

Medical dispatches use the simulator's default rate (one every 30 min across the region),
shared unevenly between helipads (busier trauma centres), flat over the day with a small
evening rise.

    uv run python -m aerolink.demand.synthetic          # writes backend/data/synthetic-demand/
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from aerolink.network import Network
from aerolink.sim.demand import DemandConfig, diurnal_rate_per_hour

BACKEND = Path(__file__).resolve().parents[3]
DEFAULT_DIR = BACKEND / "data" / "synthetic-demand"
NETWORK = BACKEND.parent / "airspace" / "central-fl-v1.geojson"
BIN_S = 900  # 15 minutes
BINS_PER_DAY = 96
LABEL = "SYNTHETIC: generated by aerolink.demand.synthetic, not observed data"


@dataclass(frozen=True)
class SyntheticDemandConfig:
    annual_trips_florida: float = 220_000.0  # FDOT AAM Business Plan, 2027, low end
    central_florida_share: float = 0.27  # assumption: share of state population
    weekend_factor: float = 0.7
    medical_per_day: float = 48.0  # simulator default: one dispatch every 1,800 s
    days: int = 28
    first_day: date = date(2027, 1, 4)  # a Monday
    seed: int = 2027

    @property
    def passenger_per_day(self) -> float:
        return self.annual_trips_florida * self.central_florida_share / 365.0


def _distance_km(network: Network, a: str, b: str) -> float:
    na, nb = network.nodes[a], network.nodes[b]
    k = math.cos(math.radians(0.5 * (na["lat"] + nb["lat"])))
    return 111.2 * math.hypot(na["lat"] - nb["lat"], (na["lon"] - nb["lon"]) * k)


def period_of(hour: float) -> int:
    """0 night, 1 morning, 2 midday, 3 evening."""
    if 6 <= hour < 10:
        return 1
    if 10 <= hour < 16:
        return 2
    if 16 <= hour < 21:
        return 3
    return 0


def destination_table(network: Network, sites: list[str], rng: np.random.Generator) -> np.ndarray:
    """(period, origin, destination) probabilities; zero on the diagonal."""
    pads = np.array([network.nodes[s].get("pads", 1) for s in sites], dtype=float)
    n = len(sites)
    table = np.zeros((4, n, n))
    for p in range(4):
        twist = rng.lognormal(0.0, 0.6, size=(n, n))
        for i in range(n):
            for j in range(n):
                if i != j:
                    table[p, i, j] = pads[j] * math.exp(-_distance_km(network, sites[i], sites[j]) / 90.0) * twist[i, j]
            table[p, i] /= table[p, i].sum()
    return table


def generate(network: Network, cfg: SyntheticDemandConfig) -> tuple[pa.Table, pa.Table]:
    """(counts per site per 15-min bin, individual passenger trips)."""
    rng = np.random.default_rng(cfg.seed)
    evtol = sorted(network.nodes_for("passenger"))
    helipads = sorted(n for n in network.nodes_for("medical") if "helipad" in network.nodes[n].get("types", []))
    pads = np.array([network.nodes[s].get("pads", 1) for s in evtol], dtype=float)
    site_share = pads / pads.sum()
    med_share = rng.dirichlet(np.full(len(helipads), 1.5))
    dest = destination_table(network, evtol, rng)

    shape_cfg = DemandConfig()
    hours = (np.arange(BINS_PER_DAY) + 0.5) * BIN_S / 3600.0
    shape = np.array([diurnal_rate_per_hour(h, shape_cfg) for h in hours])
    shape /= shape.sum()
    med_shape = 1.0 + 0.3 * np.exp(-0.5 * ((hours - 19.0) / 3.0) ** 2)
    med_shape /= med_shape.sum()

    counts = {k: [] for k in ["synthetic", "day", "weekday", "bin", "site", "kind", "count"]}
    trips = {k: [] for k in ["synthetic", "day", "weekday", "t_of_day_s", "origin", "destination"]}
    for d in range(cfg.days):
        day = cfg.first_day + timedelta(days=d)
        factor = cfg.weekend_factor if day.weekday() >= 5 else 1.0
        for si, site in enumerate(evtol):
            lam = cfg.passenger_per_day * factor * site_share[si] * shape
            n = rng.poisson(lam)
            for b in range(BINS_PER_DAY):
                counts["synthetic"].append(True)
                counts["day"].append(day.isoformat())
                counts["weekday"].append(day.weekday())
                counts["bin"].append(b)
                counts["site"].append(site)
                counts["kind"].append("passenger")
                counts["count"].append(int(n[b]))
                for _ in range(int(n[b])):
                    t = b * BIN_S + float(rng.uniform(0, BIN_S))
                    j = int(rng.choice(len(evtol), p=dest[period_of(t / 3600.0), si]))
                    trips["synthetic"].append(True)
                    trips["day"].append(day.isoformat())
                    trips["weekday"].append(day.weekday())
                    trips["t_of_day_s"].append(t)
                    trips["origin"].append(site)
                    trips["destination"].append(evtol[j])
        for hi, site in enumerate(helipads):
            n = rng.poisson(cfg.medical_per_day * med_share[hi] * med_shape)
            for b in range(BINS_PER_DAY):
                counts["synthetic"].append(True)
                counts["day"].append(day.isoformat())
                counts["weekday"].append(day.weekday())
                counts["bin"].append(b)
                counts["site"].append(site)
                counts["kind"].append("medical")
                counts["count"].append(int(n[b]))
    meta = {"label": LABEL, "config": repr(cfg)}
    return pa.table(counts).replace_schema_metadata(meta), pa.table(trips).replace_schema_metadata(meta)


def write(out: Path, counts: pa.Table, trips: pa.Table) -> None:
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(counts, out / "counts.parquet")
    pq.write_table(trips, out / "trips.parquet")


def load(out: Path = DEFAULT_DIR) -> tuple[pa.Table, pa.Table]:
    return pq.read_table(out / "counts.parquet"), pq.read_table(out / "trips.parquet")


def count_matrix(counts: pa.Table, kind: str) -> tuple[list[str], list[str], np.ndarray]:
    """(days, sites, array[site, day*96 + bin]) for one kind ("passenger" or "medical")."""
    df = counts.filter(pa.array([k == kind for k in counts["kind"].to_pylist()])).to_pydict()
    days = sorted(set(df["day"]))
    sites = sorted(set(df["site"]))
    di = {d: i for i, d in enumerate(days)}
    si = {s: i for i, s in enumerate(sites)}
    m = np.zeros((len(sites), len(days) * BINS_PER_DAY))
    for d, s, b, c in zip(df["day"], df["site"], df["bin"], df["count"], strict=True):
        m[si[s], di[d] * BINS_PER_DAY + b] = c
    return days, sites, m


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the synthetic demand history (labelled synthetic).")
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--seed", type=int, default=2027)
    ap.add_argument("--network", type=Path, default=NETWORK)
    ap.add_argument("--out", type=Path, default=DEFAULT_DIR)
    a = ap.parse_args(argv)
    cfg = SyntheticDemandConfig(days=a.days, seed=a.seed)
    counts, trips = generate(Network.from_geojson(a.network), cfg)
    write(a.out, counts, trips)
    kinds = counts.to_pydict()
    pax = sum(c for c, k in zip(kinds["count"], kinds["kind"], strict=True) if k == "passenger")
    med = sum(c for c, k in zip(kinds["count"], kinds["kind"], strict=True) if k == "medical")
    print(
        f"SYNTHETIC demand, {cfg.days} days: {pax:,} passenger departures ({pax / cfg.days:.0f}/day, "
        f"target {cfg.passenger_per_day:.0f}), {med:,} medical dispatches"
    )
    print(f"wrote {a.out}/counts.parquet and trips.parquet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

#### New file: `backend/src/aerolink/demand/forecaster.py`

*What this file does:* The count forecasters (Chronos-2 and the baselines), the destination model, and `DemandModel`, which turns forecasts into expected flights for the route engine.

Open the file (command):

```bash
code src/aerolink/demand/forecaster.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Demand forecasters and their baselines (PLAN E5.4, ARCHITECTURE §7.2).

Counts (departures per eVTOL site, dispatches per helipad, per 15 min):

* ``chronos``: Chronos-2, zero-shot, with time of day (sin/cos) as a known covariate
* ``mean_rate``: each site's mean count over the whole history (flat)
* ``last_schedule``: the same 15-min bin one day earlier
* ``constant_rate``: one rate for every site and bin (total history mean)
* ``tod_mean``: each site's mean for that time of day (not in the plan; a stronger,
  still simple reference, so the Chronos result has an honest yardstick)

Destinations of passenger trips: LightGBM multiclass on origin, hour and weekday,
against a uniform choice among the other sites.

``DemandModel`` turns both into *expected flights* for the traffic forecast: for every
site and upcoming bin, probability-weighted arrivals at each destination (ARCHITECTURE §7.3).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import lightgbm as lgb
import numpy as np

from aerolink.demand.synthetic import BIN_S, BINS_PER_DAY

QUANTILES = [0.1, 0.5, 0.9]


def _tod_covariates(start_bin: int, length: int) -> dict[str, np.ndarray]:
    phase = 2 * math.pi * ((np.arange(length) + start_bin) % BINS_PER_DAY) / BINS_PER_DAY
    return {"tod_sin": np.sin(phase), "tod_cos": np.cos(phase)}


def _poisson_band(rate: np.ndarray) -> np.ndarray:
    """(S, H, 3) p10/p50/p90 of a Poisson count with the given mean (normal approximation)."""
    sd = np.sqrt(np.maximum(rate, 1e-9))
    return np.stack([np.maximum(0.0, rate - 1.2816 * sd), rate, rate + 1.2816 * sd], axis=-1)


# Every count forecaster: (history[S, T], horizon H) -> (S, H, 3) p10/p50/p90.
# The history always ends at a day boundary or anywhere: its length T sets the time of day.


def mean_rate(history: np.ndarray, horizon: int) -> np.ndarray:
    return _poisson_band(np.repeat(history.mean(axis=1, keepdims=True), horizon, axis=1))


def constant_rate(history: np.ndarray, horizon: int) -> np.ndarray:
    return _poisson_band(np.full((history.shape[0], horizon), history.mean()))


def last_schedule(history: np.ndarray, horizon: int) -> np.ndarray:
    T = history.shape[1]
    idx = [T - BINS_PER_DAY + (h % BINS_PER_DAY) for h in range(horizon)]
    return _poisson_band(history[:, idx])


def tod_mean(history: np.ndarray, horizon: int) -> np.ndarray:
    S, T = history.shape
    days = T // BINS_PER_DAY
    tail = history[:, T - days * BINS_PER_DAY :].reshape(S, days, BINS_PER_DAY).mean(axis=1)
    idx = [(T + h) % BINS_PER_DAY for h in range(horizon)]
    return _poisson_band(tail[:, idx])


class ChronosCounts:
    """Chronos-2 on count series with time-of-day covariates."""

    def __init__(self, model_name: str = "autogluon/chronos-2-small", context_days: int = 14):
        self.model_name = model_name
        self.context_days = context_days
        self._pipeline: Any = None

    def _load(self) -> Any:
        if self._pipeline is None:
            from chronos import BaseChronosPipeline

            self._pipeline = BaseChronosPipeline.from_pretrained(self.model_name, device_map="cpu")
        return self._pipeline

    def __call__(self, history: np.ndarray, horizon: int) -> np.ndarray:
        T = history.shape[1]
        ctx = min(T, self.context_days * BINS_PER_DAY)
        start = T - ctx
        past = _tod_covariates(start, ctx)
        future = _tod_covariates(T, horizon)
        inputs = [
            {"target": history[s, start:].astype(np.float32), "past_covariates": past, "future_covariates": future}
            for s in range(history.shape[0])
        ]
        quantiles, _ = self._load().predict_quantiles(inputs, prediction_length=horizon, quantile_levels=QUANTILES)
        out = np.stack([q.detach().cpu().numpy()[0] for q in quantiles])  # (S, H, 3)
        return np.maximum(0.0, np.sort(out, axis=-1))


COUNT_FORECASTERS: dict[str, Callable[[np.ndarray, int], np.ndarray]] = {
    "mean_rate": mean_rate,
    "last_schedule": last_schedule,
    "constant_rate": constant_rate,
    "tod_mean": tod_mean,
}


# --- destinations ---------------------------------------------------------------------------


class DestinationModel:
    """P(destination | origin, hour, weekday) for passenger trips (LightGBM multiclass)."""

    def __init__(self, sites: list[str], seed: int = 42):
        self.sites = sorted(sites)
        self.index = {s: i for i, s in enumerate(self.sites)}
        self.seed = seed
        self.model: lgb.LGBMClassifier | None = None

    def _x(self, origins: list[str], hours: np.ndarray, weekdays: np.ndarray) -> np.ndarray:
        return np.column_stack([[self.index[o] for o in origins], hours, weekdays]).astype(float)

    def fit(self, origins: list[str], hours: np.ndarray, weekdays: np.ndarray, dests: list[str]) -> DestinationModel:
        self.model = lgb.LGBMClassifier(
            n_estimators=200, learning_rate=0.05, num_leaves=15, random_state=self.seed, verbose=-1
        )
        self.model.fit(
            self._x(origins, hours, weekdays), np.array([self.index[d] for d in dests]), categorical_feature=[0]
        )
        return self

    def proba(self, origins: list[str], hours: np.ndarray, weekdays: np.ndarray) -> np.ndarray:
        """(N, n_sites) probabilities; never the origin itself."""
        assert self.model is not None
        raw = self.model.predict_proba(self._x(origins, hours, weekdays))
        p = np.zeros((len(origins), len(self.sites)))
        p[:, np.asarray(self.model.classes_)] = np.asarray(raw)
        for i, o in enumerate(origins):
            p[i, self.index[o]] = 0.0
        return p / np.maximum(1e-12, p.sum(axis=1, keepdims=True))

    def uniform(self, origins: list[str]) -> np.ndarray:
        p = np.ones((len(origins), len(self.sites)))
        for i, o in enumerate(origins):
            p[i, self.index[o]] = 0.0
        return p / p.sum(axis=1, keepdims=True)


# --- expected flights for the route engine -------------------------------------------------------


class DemandModel:
    """Next-day demand forecast, made once and cached, read by the route engine (A3+).

    ``forecast`` maps kind -> (sites, array[site, bin of day, 3]) of p10/p50/p90 counts per
    15 min for the forecast day.
    """

    def __init__(
        self,
        forecast: dict[str, tuple[list[str], np.ndarray]],
        destinations: DestinationModel | None,
        weekday: int,
    ):
        self.forecast = forecast
        self.destinations = destinations
        self.weekday = weekday

    @classmethod
    def from_history(
        cls,
        histories: dict[str, tuple[list[str], np.ndarray]],
        destinations: DestinationModel | None,
        weekday: int,
        count_forecaster: Callable[[np.ndarray, int], np.ndarray] = tod_mean,
    ) -> DemandModel:
        return cls(
            {k: (s, count_forecaster(h, BINS_PER_DAY)) for k, (s, h) in histories.items()}, destinations, weekday
        )

    def expected_flights(
        self,
        local_hour: float,
        t_s: float,
        travel_time_s: Callable[[str, str], float | None],
        horizon_s: float = 1800.0,
        medical_bases: dict[str, str] | None = None,
        min_prob: float = 0.02,
    ) -> list[tuple[float, str, float, float, float]]:
        """(probability, node, eta p10, p50, p90) of flights expected to depart in the next ``horizon_s``.

        ``t_s`` is the caller's clock at ``local_hour``. Passenger flights arrive at their
        likely destinations; medical dispatches arrive at the helipad (from ``medical_bases``,
        the nearest base, when given). ETAs are spread over the departure bin.
        """
        out: list[tuple[float, str, float, float, float]] = []
        first = int((local_hour * 3600.0) // BIN_S)
        n_bins = max(1, math.ceil(horizon_s / BIN_S))
        for kind, (sites, q) in self.forecast.items():
            for k in range(n_bins):
                b = (first + k) % BINS_PER_DAY
                dep_mid = t_s + (b * BIN_S + BIN_S / 2.0 - local_hour * 3600.0) % 86400.0
                for si, site in enumerate(sites):
                    rate = float(q[si, b, 1])
                    if rate < min_prob:
                        continue
                    if kind == "passenger":
                        if self.destinations is not None and site in self.destinations.index:
                            p = self.destinations.proba(
                                [site], np.array([(b + 0.5) * BIN_S / 3600.0]), np.array([self.weekday])
                            )[0]
                            dests = [(self.destinations.sites[j], float(p[j])) for j in np.flatnonzero(p > 0)]
                        else:
                            dests = []
                        for dest, pd in dests:
                            prob = min(1.0, rate * pd)
                            tt = travel_time_s(site, dest) if prob >= min_prob else None
                            if tt is not None:
                                out.append(
                                    (prob, dest, dep_mid - BIN_S / 2 + tt, dep_mid + tt, dep_mid + BIN_S / 2 + tt)
                                )
                    else:
                        base = (medical_bases or {}).get(site)
                        tt = travel_time_s(base, site) if base else 0.0
                        if tt is not None:
                            prob = min(1.0, rate)
                            out.append((prob, site, dep_mid - BIN_S / 2 + tt, dep_mid + tt, dep_mid + BIN_S / 2 + tt))
        return out
```

#### New file: `backend/src/aerolink/demand/evaluate.py`

*What this file does:* The command you run. Holds out the last 7 days, forecasts each one day ahead, and writes `eval-results/demand-e5.4.json`.

Open the file (command):

```bash
code src/aerolink/demand/evaluate.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Demand forecaster accuracy on SYNTHETIC data (PLAN E5.4).

    uv run python -m aerolink.demand.evaluate               # Chronos-2 + baselines
    uv run python -m aerolink.demand.evaluate --no-chronos  # baselines only (no download)

The last ``--test-days`` days of the synthetic history are held out. Each held-out day is
forecast one day ahead from everything before it (96 bins of 15 min per site). Reported per
forecaster and kind: mean absolute error per site-bin, daily total bias, and p10-p90
coverage; for medical also the rate calibration (predicted / observed dispatches).
Destinations: LightGBM vs a uniform choice, on held-out trips (top-1 accuracy, log loss).

Results: ``backend/eval-results/demand-e5.4.json`` (labelled synthetic).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from aerolink.demand.forecaster import COUNT_FORECASTERS, ChronosCounts, DestinationModel
from aerolink.demand.synthetic import (
    BINS_PER_DAY,
    DEFAULT_DIR,
    LABEL,
    NETWORK,
    SyntheticDemandConfig,
    count_matrix,
    generate,
    load,
    write,
)
from aerolink.network import Network

BACKEND = Path(__file__).resolve().parents[3]
DEFAULT_OUT = BACKEND / "eval-results" / "demand-e5.4.json"


def evaluate_counts(m: np.ndarray, test_days: int, forecasters: dict) -> dict:
    n_days = m.shape[1] // BINS_PER_DAY
    res = {}
    for name, f in forecasters.items():
        err, bias, cov, pred_tot, obs_tot = [], [], [], 0.0, 0.0
        for d in range(n_days - test_days, n_days):
            hist = m[:, : d * BINS_PER_DAY]
            truth = m[:, d * BINS_PER_DAY : (d + 1) * BINS_PER_DAY]
            q = f(hist, BINS_PER_DAY)
            err.append(np.abs(q[..., 1] - truth).mean())
            bias.append(float(q[..., 1].sum() - truth.sum()))
            cov.append(((truth >= np.floor(q[..., 0])) & (truth <= np.ceil(q[..., 2]))).mean())
            pred_tot += float(q[..., 1].sum())
            obs_tot += float(truth.sum())
        res[name] = {
            "mae_per_site_bin": round(float(np.mean(err)), 4),
            "daily_total_bias": round(float(np.mean(bias)), 2),
            "coverage_p10_p90": round(float(np.mean(cov)), 3),
            "rate_calibration": round(pred_tot / max(1e-9, obs_tot), 3),
        }
    return res


def evaluate_destinations(trips: dict, sites: list[str], test_days: list[str], seed: int) -> dict:
    test = np.array([d in set(test_days) for d in trips["day"]])
    origins = np.array(trips["origin"])
    dests = np.array(trips["destination"])
    hours = np.array(trips["t_of_day_s"]) / 3600.0
    weekdays = np.array(trips["weekday"], dtype=float)
    model = DestinationModel(sites, seed).fit(list(origins[~test]), hours[~test], weekdays[~test], list(dests[~test]))
    truth = np.array([model.index[d] for d in dests[test]])
    out = {"held_out_trips": int(test.sum())}
    for name, p in [
        ("lightgbm", model.proba(list(origins[test]), hours[test], weekdays[test])),
        ("uniform", model.uniform(list(origins[test]))),
    ]:
        pt = p[np.arange(len(truth)), truth]
        out[name] = {
            "top1_accuracy": round(float((p.argmax(axis=1) == truth).mean()), 3),
            "log_loss": round(float(-np.log(np.maximum(pt, 1e-12)).mean()), 3),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--test-days", type=int, default=7)
    ap.add_argument("--no-chronos", action="store_true")
    ap.add_argument("--chronos-model", default="autogluon/chronos-2-small")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args(argv)

    if not (a.data / "counts.parquet").exists():
        print(f"no synthetic history in {a.data}: generating it")
        write(a.data, *generate(Network.from_geojson(NETWORK), SyntheticDemandConfig()))
    counts, trips_table = load(a.data)

    forecasters = dict(COUNT_FORECASTERS)
    if not a.no_chronos:
        forecasters["chronos"] = ChronosCounts(a.chronos_model)

    report: dict = {"label": LABEL, "test_days": a.test_days, "counts": {}}
    for kind in ["passenger", "medical"]:
        days, sites, m = count_matrix(counts, kind)
        print(f"{kind}: {len(sites)} sites, {len(days)} days, {int(m.sum()):,} flights", flush=True)
        report["counts"][kind] = evaluate_counts(m, a.test_days, forecasters)
        if kind == "passenger":
            report["destinations"] = evaluate_destinations(trips_table.to_pydict(), sites, days[-a.test_days :], a.seed)

    for kind, res in report["counts"].items():
        print(f"\n{kind} (one day ahead, {a.test_days} held-out days, SYNTHETIC)")
        print(f"  {'forecaster':<15}{'MAE/bin':>9}{'bias/day':>10}{'cover':>8}{'calib':>8}")
        for name, r in sorted(res.items(), key=lambda kv: kv[1]["mae_per_site_bin"]):
            print(
                f"  {name:<15}{r['mae_per_site_bin']:>9.3f}{r['daily_total_bias']:>10.1f}"
                f"{r['coverage_p10_p90']:>8.0%}{r['rate_calibration']:>8.2f}"
            )
    d = report["destinations"]
    print(f"\ndestinations ({d['held_out_trips']} held-out trips):")
    for name in ["lightgbm", "uniform"]:
        print(f"  {name:<10} top-1 {d[name]['top1_accuracy']:.0%}   log loss {d[name]['log_loss']:.3f}")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

#### New file: `backend/tests/test_e54_demand.py`

*What this file does:* Checks the synthetic data is labelled, scaled and repeatable, the baselines, the Chronos wrapper (with a stand-in model), the evaluation command and the expected flights.

Open the file (command):

```bash
code tests/test_e54_demand.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""E5.4: synthetic demand, baselines, Chronos-2 wrapper and expected flights (no downloads)."""

import json
from pathlib import Path

import numpy as np
import pytest
import torch

from aerolink.demand.evaluate import main
from aerolink.demand.forecaster import (
    BINS_PER_DAY,
    ChronosCounts,
    DemandModel,
    DestinationModel,
    constant_rate,
    last_schedule,
    mean_rate,
    tod_mean,
)
from aerolink.demand.synthetic import LABEL, SyntheticDemandConfig, count_matrix, generate, write
from aerolink.network import Network


@pytest.fixture(scope="module")
def synthetic(fl_network: Network, tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("demand")
    write(out, *generate(fl_network, SyntheticDemandConfig(days=10)))
    return out


def test_synthetic_is_labelled_scaled_and_deterministic(fl_network: Network):
    counts, trips = generate(fl_network, SyntheticDemandConfig(days=7))
    assert counts.schema.metadata[b"label"].decode() == LABEL
    assert all(counts["synthetic"].to_pylist())
    _, sites, m = count_matrix(counts, "passenger")
    assert len(sites) == len(fl_network.nodes_for("passenger"))
    assert 100 < m.sum() / 7 < 200  # ~163/day on weekdays, less at weekends
    again, _ = generate(fl_network, SyntheticDemandConfig(days=7))
    assert again.equals(counts)
    assert all(o != d for o, d in zip(trips["origin"].to_pylist(), trips["destination"].to_pylist(), strict=True))


def test_baselines_shapes_and_order():
    hist = np.random.default_rng(0).poisson(1.0, size=(3, 2 * BINS_PER_DAY)).astype(float)
    for f in (mean_rate, constant_rate, last_schedule, tod_mean):
        q = f(hist, 8)
        assert q.shape == (3, 8, 3)
        assert (q[..., 0] <= q[..., 1]).all() and (q[..., 1] <= q[..., 2]).all()
    assert np.allclose(last_schedule(hist, 4)[..., 1], hist[:, BINS_PER_DAY : BINS_PER_DAY + 4])


class FakeChronos2:
    def predict_quantiles(self, inputs, prediction_length, quantile_levels):
        assert "tod_sin" in inputs[0]["future_covariates"]
        out = [torch.full((1, prediction_length, 3), float(np.mean(x["target"]))) for x in inputs]
        return out, [o[..., 1] for o in out]


def test_chronos_counts_with_covariates():
    f = ChronosCounts()
    f._pipeline = FakeChronos2()
    q = f(np.ones((2, BINS_PER_DAY * 3)), 4)
    assert q.shape == (2, 4, 3) and np.allclose(q, 1.0)


def test_evaluate_reports_every_forecaster(synthetic: Path, tmp_path: Path):
    out = tmp_path / "e54.json"
    assert main(["--data", str(synthetic), "--no-chronos", "--test-days", "3", "--out", str(out)]) == 0
    r = json.loads(out.read_text())
    assert set(r["counts"]["passenger"]) == {"mean_rate", "last_schedule", "constant_rate", "tod_mean"}
    assert r["destinations"]["lightgbm"]["top1_accuracy"] > r["destinations"]["uniform"]["top1_accuracy"]
    assert r["label"] == LABEL


def test_expected_flights_weight_by_probability(fl_network: Network, synthetic: Path):
    import pyarrow.parquet as pq

    counts = pq.read_table(synthetic / "counts.parquet")
    trips = pq.read_table(synthetic / "trips.parquet").to_pydict()
    hist = {k: count_matrix(counts, k)[1:] for k in ["passenger", "medical"]}
    dest = DestinationModel(hist["passenger"][0]).fit(
        trips["origin"],
        np.array(trips["t_of_day_s"]) / 3600,
        np.array(trips["weekday"], dtype=float),
        trips["destination"],
    )
    model = DemandModel.from_history(hist, dest, weekday=2)
    exp = model.expected_flights(8.0, 0.0, lambda a, b: 600.0)
    assert exp and all(0 < p <= 1 for p, *_ in exp)
    assert all(lo <= mid <= hi for _, _, lo, mid, hi in exp)
```

### 8b. Run the checks

Command:

```bash
uv run ruff format
uv run ruff check --fix
uv run pytest -q tests/test_e54_demand.py
```

Expect **`5 passed`**.

### 8c. Generate the synthetic history

Command:

```bash
uv run python -m aerolink.demand.synthetic
```

Expected output (the numbers will match exactly, because the seed is fixed):

```text
SYNTHETIC demand, 28 days: 4,217 passenger departures (151/day, target 163), 1,380 medical dispatches
wrote .../backend/data/synthetic-demand/counts.parquet and trips.parquet
```

(151 a day on average because weekends are quieter than the 163 weekday target.)

### 8d. Evaluate

Command:

```bash
uv run python -m aerolink.demand.evaluate
```

*What it does:* forecasts each of the 7 held-out days with every forecaster, including Chronos-2 (uses the weights downloaded in Part 6; a couple of minutes). Example output:

```text
passenger (one day ahead, 7 held-out days, SYNTHETIC)
  forecaster       MAE/bin  bias/day   cover   calib
  tod_mean           0.368       7.7     99%    1.05
  chronos            ...
  ...
destinations (1015 held-out trips):
  lightgbm   top-1 46%   log loss 1.439
  uniform    top-1 21%   log loss 1.609
```

How to read it:
- **MAE/bin:** average miss per site per 15 minutes (counts are small, so these are small numbers). Lower is better. The table is sorted best first.
- **bias/day:** predicted minus actual flights per day across all sites (near 0 is good).
- **cover:** how often the actual count was inside the p10–p90 range.
- **calib** (rate calibration): predicted ÷ actual total flights. 1.00 is perfect; this is the "medical rate calibration" metric from the plan.
- **destinations:** LightGBM should beat uniform on both top-1 accuracy (higher) and log loss (lower).

If Chronos-2 can't load, use `--no-chronos`, commit, and tell Joshua.

### 8e. Commit

Command:

```bash
git add src/aerolink/demand tests/test_e54_demand.py data/synthetic-demand eval-results/demand-e5.4.json
git commit -F - <<'EOF'
feat: E5.4 demand forecaster (synthetic, labelled)

* feat: synthetic diurnal Poisson demand scaled from FDOT's 2027 trip forecast
* feat: Chronos-2 counts with time-of-day covariates vs mean/last/constant baselines
* feat: LightGBM destination model; expected flights for the traffic forecast
EOF
git push
```

**E5.4 done.**

---

## Part 9: E5.5 Off-Route Detection

**What you're building:** a detector that notices when an aircraft stops following its predicted path, and the reaction to it.

**The classical baseline** flags an aircraft only when it's more than a fixed **300 m** from its route. That misses an aircraft that stays on the line but crawls along at half speed, and it's either too loose or too tight depending on the corridor.

**AeroLink's detector** uses tolerances learned from data. Every check runs every 10 seconds and needs **3 hits in a row** (one wobble isn't enough):
- **Route tolerance (learned):** a LightGBM model learns, from clean simulated flights, how far aircraft normally stray from the route given their speed and the turn ahead (tight on straight legs, wider before turns). Outside that = flag.
- **Schedule window:** at departure the ETA head (E5.3) predicts an arrival window. If the aircraft can no longer make it inside that window even at full cruise speed, it's off its predicted path in time. This is what catches the slow-down.
- **Band check:** 30 s ago, `aerolink-gbm` predicted where the aircraft would be now, with a calibrated range. Being outside it means an unexpected manoeuvre.
- **Anomaly score:** an `IsolationForest` (a standard scikit-learn "this looks unusual" model) trained on how clean **real** flights deviate from their 30 s forecasts.

The band check and anomaly score are quick but jumpy on their own, so AeroLink counts them only when **both** agree. The final rule: *route tolerance* OR *schedule window* OR (*band* AND *anomaly*).

(While building this, checking the band every second at p05–p95, as the plan first said, gave about 23 false alarms per flight-hour, because consecutive seconds aren't independent. Checking every 10 s at p01–p99 gives about 1. Part 11's docs step writes that change into ARCHITECTURE §7.4.)

**When an aircraft is flagged**, the route engine marks its flight `off_route`, recalculates route options from its current position ("Off route — recalculating"), and spreads its possible positions over every node it could reach in the next 10 minutes so the traffic forecast accounts for it. That code lives in the new `routing/aerolink.py`, which E5.7 also uses.

**How it's measured:** the `rogue_aircraft` scenario is run with 20 held-out seeds. Half of the completed flights get a deviation injected at a random moment, and the moment is recorded (that's the labelled ground truth): a **turn** off the route (25–60°), a slow **drift** (5–10°), or a **slow**-down to half speed. A detection counts if the method flags the flight within 300 s of the deviation starting; every other new flag is a false alarm.

- **Precision:** of all the flags raised, how many were real deviations.
- **Recall:** of all the injected deviations, how many were caught.
- **Latency:** how many seconds after the deviation started the first flag came.

### 9a. Create the folder and files

Command:

```bash
mkdir -p src/aerolink/offroute
```

#### New file: `backend/src/aerolink/offroute/__init__.py`

*What this file does:* Marks the folder as a package.

Open the file (command):

```bash
code src/aerolink/offroute/__init__.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Off-route detection (PLAN E5.5, ARCHITECTURE §7.4)."""
```

#### New file: `backend/src/aerolink/offroute/detector.py`

*What this file does:* All the checks (fixed bound, learned tolerance, schedule window, band, anomaly), the models behind them, and `reachable_mass` for spreading a flagged aircraft over the network.

Open the file (command):

```bash
code src/aerolink/offroute/detector.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Off-route detection: learned tolerance instead of fixed bounds (PLAN E5.5, ARCHITECTURE §7.4).

The classical baseline (ARCHITECTURE §6) flags an aircraft more than a fixed 300 m from its
route. AeroLink replaces that fixed number with tolerances learned from data, and adds
checks the fixed bound cannot do. Every check runs on a 1 Hz track, is evaluated every
10 s (back-to-back seconds are not independent evidence) and needs 3 consecutive hits:

* **Route tolerance (learned):** a LightGBM quantile model (p99) of how far clean simulated
  flights stray from their route, given speed, distance to the next node and the turn
  there. The tolerance is tight on straight legs and wider before turns.
* **Schedule window:** at departure the ETA head gives a p10-p90 arrival window. An
  aircraft that can no longer reach its destination inside that window (widened to p99),
  even at cruise speed, is off its predicted path in time.
* **Band check:** 30 s ago the position head predicted where the aircraft would be now,
  with a calibrated p10-p90 box, widened here to p01-p99 (x1.81). Outside = unexpected
  manoeuvre.
* **Anomaly score:** an ``IsolationForest`` trained on residual features of clean real
  flights (cross-track and along-track error of that 30 s prediction, speed change,
  vertical-rate deviation). Below the 1st percentile of clean flights = anomalous.

AeroLink's rule: route tolerance OR schedule OR (band AND anomaly). The band and anomaly
checks catch sudden manoeuvres early but raise false alarms on their own, so they only
count when they agree.

When an aircraft is flagged, the route engine marks its flight ``off_route``, recalculates
options from its position, and spreads its possible positions over the nodes it can reach
(``reachable_mass``) so the traffic forecast accounts for it (see ``routing/aerolink.py``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
from sklearn.ensemble import IsolationForest

from aerolink.forecast.features import HISTORY_S, VEHICLE_CODE, cv_state, to_local, to_track_frame
from aerolink.network import Network

LEAD_S = 30  # the band is the 30 s position forecast made 30 s earlier
BAND_SCALE = 2.326 / 1.2816  # p10-p90 half-width -> p01-p99
CHECK_EVERY_S = 10
CONSECUTIVE = 3
FIXED_BOUND_M = 300.0
MODEL_DIR = Path(__file__).resolve().parents[3] / "models" / "offroute"
RESIDUAL_FEATURES = ["abs_cross_err_m", "along_err_m", "speed_change_mps", "vrate_dev_mps"]
TOLERANCE_FEATURES = ["vehicle", "speed_mps", "dist_next_node_m", "turn_at_next_deg", "alt_m"]


# --- geometry -------------------------------------------------------------------------------


def route_polyline(network: Network, path: list[str]) -> list[tuple[float, float]]:
    """The planned route as (lat, lon) points, following each corridor's real shape."""
    pts: list[tuple[float, float]] = []
    for a, b in zip(path, path[1:], strict=False):
        for lon, lat in network.edges[(a, b)].oriented_geometry(a):
            if not pts or pts[-1] != (lat, lon):
                pts.append((lat, lon))
    return pts


def cross_track_m(lat: float, lon: float, path_xy: list[tuple[float, float]]) -> float:
    """Distance from (lat, lon) to the nearest piece of a route given as (lat, lon) points."""
    ys = np.array([p[0] for p in path_xy])
    xs = np.array([p[1] for p in path_xy])
    e, n = to_local(ys, xs, lat, lon)
    ax, ay, bx, by = e[:-1], n[:-1], e[1:], n[1:]
    dx, dy = bx - ax, by - ay
    seg2 = np.maximum(dx * dx + dy * dy, 1e-9)
    u = np.clip(-(ax * dx + ay * dy) / seg2, 0.0, 1.0)
    return float(np.min(np.hypot(ax + u * dx, ay + u * dy))) if len(ax) else math.inf


def _speed(xyz: np.ndarray, i: int) -> float:
    if i < 1:
        return 0.0
    e, n = to_local(xyz[i - 1 : i + 1, 0], xyz[i - 1 : i + 1, 1], xyz[i, 0], xyz[i, 1])
    return float(math.hypot(e[1] - e[0], n[1] - n[0]))


def smoothed_speed(xyz: np.ndarray, i: int, window: int = 10) -> float:
    """Ground speed over the last ``window`` seconds (less noisy than one second)."""
    j = max(0, i - window)
    if i <= j:
        return 0.0
    e, n = to_local(xyz[[j, i], 0], xyz[[j, i], 1], xyz[i, 0], xyz[i, 1])
    return float(math.hypot(e[1] - e[0], n[1] - n[0]) / (i - j))


# --- checks over time -------------------------------------------------------------------------


def consecutive(flags: np.ndarray, n: int = CONSECUTIVE, clear: int = 2) -> np.ndarray:
    """Flag after ``n`` consecutive Trues; clear only after ``clear`` consecutive Falses.

    The hysteresis stops one wobbling aircraft from raising a new alarm every few checks.
    """
    run_on = run_off = 0
    state = False
    out = np.zeros(len(flags), dtype=bool)
    for k, f in enumerate(flags):
        run_on, run_off = (run_on + 1, 0) if f else (0, run_off + 1)
        if not state and run_on >= n:
            state = True
        elif state and run_off >= clear:
            state = False
        out[k] = state
    return out


def checked(n: int, idx: np.ndarray, hits: np.ndarray, t: np.ndarray) -> np.ndarray:
    """``consecutive`` on one check every ``CHECK_EVERY_S``; each result holds until the next check."""
    keep = ((t[idx] - t[0]) % CHECK_EVERY_S) == 0
    checks, state = idx[keep], consecutive(hits[keep])
    out = np.zeros(n, dtype=bool)
    for k, i in enumerate(checks):
        end = checks[k + 1] if k + 1 < len(checks) else n
        out[i:end] = state[k]
    return out


# --- band check and anomaly score (position head residuals) ------------------------------------


@dataclass
class TrackForecasts:
    """30 s-ahead predictions made at every second of a track that has 60 s of history."""

    t: np.ndarray  # (N,) time the prediction is FOR (made 30 s earlier)
    box: np.ndarray  # (N, 4) east p10, east p90, north p10, north p90 relative to the fix at t - 30
    p50: np.ndarray  # (N, 2) east, north p50 relative to the fix at t - 30
    course: np.ndarray  # (N,) course at t - 30


def forecast_track(forecaster, xyz: np.ndarray, t: np.ndarray, vehicle: str | None, graphs=None) -> TrackForecasts:
    """Batch-predict the 30 s position for every anchor along one track (1 Hz)."""
    anchors = list(range(HISTORY_S - 1, len(t) - LEAD_S))
    if not anchors:
        z = np.zeros((0,))
        return TrackForecasts(z, np.zeros((0, 4)), np.zeros((0, 2)), z)
    tracks = [xyz[i - HISTORY_S + 1 : i + 1] for i in anchors]
    if getattr(forecaster, "name", "") == "aerolink-gbm":
        preds = forecaster.predict_position_batch(
            tracks, [LEAD_S], vehicles=[vehicle] * len(tracks), graphs=[graphs[i] for i in anchors] if graphs else None
        )
    elif hasattr(forecaster, "predict_position_batch"):
        preds = forecaster.predict_position_batch(tracks, [LEAD_S])
    else:
        preds = [forecaster.predict_position([tuple(p) for p in tr], 0.0, "passenger", [LEAD_S]) for tr in tracks]
    p = [row[0] for row in preds]
    return TrackForecasts(
        t=t[np.array(anchors) + LEAD_S],
        box=np.array([[q.east_p10, q.east_p90, q.north_p10, q.north_p90] for q in p]),
        p50=np.array([[q.east_p50, q.north_p50] for q in p]),
        course=np.array([cv_state(tr)[1] for tr in tracks]),
    )


def outside_band(xyz: np.ndarray, t: np.ndarray, fc: TrackForecasts) -> tuple[np.ndarray, np.ndarray]:
    """(index into the track, True where the fix is outside the p01-p99 box)."""
    idx = np.searchsorted(t, fc.t)
    out = np.zeros(len(idx), dtype=bool)
    for k, i in enumerate(idx):
        j = i - LEAD_S
        e, n = to_local(xyz[i : i + 1, 0], xyz[i : i + 1, 1], xyz[j, 0], xyz[j, 1])
        e0, e1, n0, n1 = fc.box[k]
        ce, cn = fc.p50[k]
        e0, e1 = ce - (ce - e0) * BAND_SCALE, ce + (e1 - ce) * BAND_SCALE
        n0, n1 = cn - (cn - n0) * BAND_SCALE, cn + (n1 - cn) * BAND_SCALE
        out[k] = not (e0 <= float(e[0]) <= e1 and n0 <= float(n[0]) <= n1)
    return idx, out


def residual_features(xyz: np.ndarray, t: np.ndarray, fc: TrackForecasts) -> tuple[np.ndarray, np.ndarray]:
    """(index into the track, residual features) for every sample that has a forecast."""
    idx = np.searchsorted(t, fc.t)
    rows = []
    for k, i in enumerate(idx):
        j = i - LEAD_S
        e, n = to_local(xyz[i : i + 1, 0], xyz[i : i + 1, 1], xyz[j, 0], xyz[j, 1])
        along, cross = to_track_frame(float(e[0]) - fc.p50[k, 0], float(n[0]) - fc.p50[k, 1], float(fc.course[k]))
        vr = np.diff(xyz[j : i + 1, 2])
        rows.append(
            [
                abs(cross),
                along,
                _speed(xyz, int(i)) - _speed(xyz, int(j)),
                float(vr[-1] - vr.mean()) if len(vr) else 0.0,
            ]
        )
    return idx, np.array(rows, dtype=float).reshape(-1, len(RESIDUAL_FEATURES))


@dataclass
class AnomalyModel:
    forest: IsolationForest
    threshold: float  # decision_function below this = anomalous
    manifest: dict = field(default_factory=dict)

    def anomalous(self, X: np.ndarray) -> np.ndarray:
        if not len(X):
            return np.zeros(0, dtype=bool)
        return self.forest.decision_function(X) < self.threshold


def train_anomaly_model(X: np.ndarray, seed: int = 42, clean_quantile: float = 0.01) -> AnomalyModel:
    forest = IsolationForest(n_estimators=200, contamination="auto", random_state=seed).fit(X)
    threshold = float(np.quantile(forest.decision_function(X), clean_quantile))
    return AnomalyModel(forest, threshold, {"seed": seed, "clean_quantile": clean_quantile, "samples": len(X)})


# --- learned route tolerance ---------------------------------------------------------------------


@dataclass
class ToleranceModel:
    """p99 of the distance from the route that clean flights show, given their situation."""

    model: lgb.LGBMRegressor
    manifest: dict = field(default_factory=dict)

    def tolerance_m(self, X: np.ndarray) -> np.ndarray:
        return np.maximum(30.0, self.model.predict(X)) if len(X) else np.zeros(0)


def tolerance_row(vehicle: str | None, speed: float, graph: dict[str, float], alt_m: float) -> list[float]:
    return [
        VEHICLE_CODE.get(vehicle or "", math.nan),
        speed,
        graph.get("dist_next_node_m", math.nan),
        graph.get("turn_at_next_deg", math.nan),
        alt_m,
    ]


def train_tolerance_model(X: np.ndarray, cross_m: np.ndarray, seed: int = 42) -> ToleranceModel:
    model = lgb.LGBMRegressor(
        objective="quantile",
        alpha=0.99,
        n_estimators=200,
        learning_rate=0.05,
        num_leaves=15,
        min_child_samples=50,
        random_state=seed,
        verbose=-1,
    )
    model.fit(X, cross_m)
    return ToleranceModel(model, {"seed": seed, "quantile": 0.99, "samples": len(X)})


# --- everything together --------------------------------------------------------------------------


@dataclass
class OffRouteModels:
    tolerance: ToleranceModel
    anomaly: AnomalyModel

    def save(self, model_dir: Path = MODEL_DIR) -> Path:
        model_dir.mkdir(parents=True, exist_ok=True)
        path = model_dir / "offroute.joblib"
        joblib.dump(self, path, compress=3)
        return path

    @staticmethod
    def load(model_dir: Path = MODEL_DIR) -> OffRouteModels:
        return joblib.load(model_dir / "offroute.joblib")


@dataclass
class Detection:
    """Per track sample: True while each check has the aircraft flagged."""

    fixed: np.ndarray  # classical: > 300 m from the route
    tolerance: np.ndarray
    schedule: np.ndarray
    band: np.ndarray
    anomaly: np.ndarray

    @property
    def ai(self) -> np.ndarray:
        return self.tolerance | self.schedule | (self.band & self.anomaly)


def detect(
    xyz: np.ndarray,
    t: np.ndarray,
    route: list[tuple[float, float]],
    graphs: list[dict[str, float]],
    forecaster,
    models: OffRouteModels,
    vehicle: str | None,
    cruise_mps: float,
    arrival_window: tuple[float, float] | None = None,
) -> Detection:
    """Run every check over one flight's 1 Hz track.

    ``route`` is the planned route polyline, ``graphs`` the graph context at each sample
    (``forecast.simdata.graph_context``), ``arrival_window`` the ETA head's (p50, p90)
    arrival time at the destination, predicted at departure.
    """
    n = len(t)
    all_idx = np.arange(n)
    cross = np.array([cross_track_m(xyz[i, 0], xyz[i, 1], route) for i in range(n)])
    fixed = consecutive(cross > FIXED_BOUND_M, n=1, clear=1)

    speeds = np.array([smoothed_speed(xyz, i) for i in range(n)])
    X = np.array([tolerance_row(vehicle, speeds[i], graphs[i], xyz[i, 2]) for i in range(n)])
    tolerance = checked(n, all_idx, cross > models.tolerance.tolerance_m(X), t)

    schedule = np.zeros(n, dtype=bool)
    if arrival_window is not None:
        p50, p90 = arrival_window
        latest = p90 + (p90 - p50) * BAND_SCALE
        remaining = np.array([g.get("remaining_route_m", 0.0) for g in graphs])
        schedule = checked(n, all_idx, t + remaining / max(1.0, cruise_mps) > latest, t)

    band = np.zeros(n, dtype=bool)
    anom = np.zeros(n, dtype=bool)
    fc = forecast_track(forecaster, xyz, t, vehicle, graphs)
    if len(fc.t):
        idx, out = outside_band(xyz, t, fc)
        band = checked(n, idx, out, t)
        idx, R = residual_features(xyz, t, fc)
        anom = checked(n, idx, models.anomaly.anomalous(R), t)
    return Detection(fixed, tolerance, schedule, band, anom)


def reachable_mass(
    network: Network, lat: float, lon: float, speed_mps: float, t_s: float, horizon_s: float = 600.0
) -> list[tuple[float, str, float, float, float]]:
    """Spread an off-route aircraft over every node it can reach within ``horizon_s``.

    Returns traffic-forecast entries (probability, node, eta p10, p50, p90): equal
    probability per reachable node (straight-line flight at its current speed), so the
    total mass is one aircraft.
    """
    speed = max(10.0, speed_mps)
    reach = []
    for nid, props in network.nodes.items():
        e, n = to_local(np.array([props["lat"]]), np.array([props["lon"]]), lat, lon)
        eta = math.hypot(float(e[0]), float(n[0])) / speed
        if eta <= horizon_s:
            reach.append((nid, eta))
    if not reach:
        return []
    p = 1.0 / len(reach)
    return [(p, nid, t_s + 0.8 * eta, t_s + eta, t_s + 1.3 * eta + 30.0) for nid, eta in reach]
```

#### New file: `backend/src/aerolink/offroute/evaluate.py`

*What this file does:* The command you run. Trains the anomaly and tolerance models, injects labelled deviations into `rogue_aircraft` flights, scores every method and writes `eval-results/offroute-e5.5.json`.

Open the file (command):

```bash
code src/aerolink/offroute/evaluate.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Train the off-route models and measure detection on ``rogue_aircraft`` (PLAN E5.5).

    uv run python -m aerolink.offroute.evaluate

1. **Train** (saved to ``backend/models/offroute/offroute.joblib``):
   * the IsolationForest, on residual features of clean real flights (E5.1 training days,
     same day split as E5.2);
   * the route tolerance, on clean simulated flights (all scenarios, training seeds).
2. **Labelled ground truth:** run ``rogue_aircraft`` with held-out seeds and take every
   completed AeroLink flight. Half of them get a deviation injected at a random moment
   (the label): a ``turn`` away from the route (25-60 deg), a slow ``drift`` (5-10 deg),
   or a ``slow``-down to half speed. 5 m position noise is added to every track.
3. **Score** each method: a deviation is detected if the method raises a flag within
   300 s after it starts; every other new flag is a false alarm.

    precision = detections / (detections + false alarms)
    recall    = detections / injected deviations
    latency   = seconds from deviation start to the first flag (median, p90)

Results: ``backend/eval-results/offroute-e5.5.json``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from aerolink.forecast.features import to_local
from aerolink.forecast.simdata import (
    AIRBORNE,
    AT_SITE,
    _path_at,
    _path_history,
    _tracks,
    _vehicle,
    add_noise,
    graph_context,
    run_scenarios,
)
from aerolink.network import Network
from aerolink.offroute.detector import (
    MODEL_DIR,
    OffRouteModels,
    cross_track_m,
    detect,
    forecast_track,
    residual_features,
    route_polyline,
    smoothed_speed,
    tolerance_row,
    train_anomaly_model,
    train_tolerance_model,
)
from aerolink.realdata.samples import DATA_DIR, _segments, list_days, split_days
from aerolink.sim import load_scenario, run_simulation
from aerolink.sim.runner import SimulationResult
from aerolink.vehicle import vehicle_cfg

BACKEND = Path(__file__).resolve().parents[3]
DEFAULT_OUT = BACKEND / "eval-results" / "offroute-e5.5.json"
WINDOW_S = 300
METHODS = ["fixed_bounds", "tolerance", "schedule", "band", "anomaly", "ai"]


def load_forecaster(name: str, network: Network):
    if name == "aerolink-gbm":
        from aerolink.forecast.gbm import GBMForecaster

        return GBMForecaster.load(network)
    from aerolink.forecast.cv import CVForecaster

    return CVForecaster(network)


def flights(res: SimulationResult, network: Network, min_s: int = 240) -> Iterator[dict]:
    """Every completed AeroLink flight: its airborne 1 Hz track (until it reaches the site) and route."""
    tracks, paths = _tracks(res), _path_history(res)
    deps = {e["flight_id"]: e for e in res.events if e["kind"] == "departure"}
    for arr in (e for e in res.events if e["kind"] == "arrival" and e.get("flight_id") in deps):
        dep, aid = deps[arr["flight_id"]], arr["aircraft_id"]
        tr = tracks[aid]
        m = (tr["t"] > dep["t"]) & (tr["t"] <= arr["t"]) & np.isin(tr["status"], list(AIRBORNE - AT_SITE))
        idx = np.flatnonzero(m)
        if len(idx) < min_s:
            continue
        yield {
            "aircraft_id": aid,
            "vehicle": _vehicle(res, aid),
            "dep_t": float(dep["t"]),
            "t": tr["t"][idx],
            "xyz": tr["xyz"][idx],
            "path": _path_at(paths[aid], float(tr["t"][idx[0]])) or list(dep["path"]),
        }


def clean_real_features(days: list[str], data_dir: Path, forecaster, max_segments: int, seed: int) -> np.ndarray:
    segs = [s for d in days for s in _segments(data_dir / f"segments-{d}.parquet")]
    if max_segments and len(segs) > max_segments:
        pick = np.random.default_rng(seed).choice(len(segs), max_segments, replace=False)
        segs = [segs[i] for i in sorted(pick)]
    feats = []
    for _, category, t, xyz in segs:
        xyz = xyz.copy()
        if not np.isfinite(xyz[:, 2]).any():
            continue
        xyz[:, 2] = np.nan_to_num(xyz[:, 2], nan=float(np.nanmedian(xyz[:, 2])))
        fc = forecast_track(forecaster, xyz, t, {"A7": "rotor", "A1": "light"}.get(category))
        if len(fc.t):
            feats.append(residual_features(xyz, t, fc)[1])
    return np.concatenate(feats) if feats else np.zeros((0, 4))


def clean_sim_tolerance_data(network: Network, scenarios: list[Path], seeds: list[int], rng) -> tuple:
    X, y = [], []
    for res in run_scenarios(network, scenarios, seeds):
        for f in flights(res, network):
            xyz = add_noise(f["xyz"], rng, 5.0)
            route = route_polyline(network, f["path"])
            for i in range(10, len(xyz), 5):
                g, _ = graph_context(network, f["path"], float(xyz[i, 0]), float(xyz[i, 1]))
                X.append(tolerance_row(f["vehicle"], smoothed_speed(xyz, i), g, float(xyz[i, 2])))
                y.append(cross_track_m(float(xyz[i, 0]), float(xyz[i, 1]), route))
    return np.array(X, dtype=float), np.array(y, dtype=float)


def inject(xyz: np.ndarray, i0: int, kind: str, rng: np.random.Generator) -> np.ndarray:
    """Modify a 1 Hz track from index ``i0`` on (the labelled deviation)."""
    out = xyz.copy()
    if kind == "slow":
        for i in range(i0, len(xyz)):
            src = i0 + 0.5 * (i - i0)
            a = int(src)
            w = src - a
            out[i] = xyz[a] * (1 - w) + xyz[min(a + 1, len(xyz) - 1)] * w
        return out
    angle = math.radians(rng.uniform(25, 60) if kind == "turn" else rng.uniform(5, 10)) * rng.choice([-1, 1])
    lat0, lon0 = xyz[i0, 0], xyz[i0, 1]
    e, n = to_local(xyz[i0:, 0], xyz[i0:, 1], lat0, lon0)
    c, s = math.cos(angle), math.sin(angle)
    k = math.cos(math.radians(lat0))
    out[i0:, 0] = lat0 + np.degrees((e * s + n * c) / 6_371_008.8)
    out[i0:, 1] = lon0 + np.degrees((e * c - n * s) / (6_371_008.8 * k))
    return out


def _onsets(flags: np.ndarray) -> np.ndarray:
    return np.flatnonzero(flags & ~np.r_[False, flags[:-1]])


def score_flights(results: list[dict]) -> dict:
    out = {}
    n_dev = sum(1 for f in results if f["i0"] is not None)
    for m in METHODS:
        tp = fp = 0
        latency: list[float] = []
        by_kind: dict[str, list[int]] = {}
        for f in results:
            on = _onsets(f["flags"][m])
            if f["i0"] is None:
                fp += len(on)
                continue
            fp += int(np.sum(on < f["i0"]))
            hit = [i for i in on if f["i0"] <= i <= f["i0"] + WINDOW_S]
            if hit:
                tp += 1
                latency.append(float(hit[0] - f["i0"]))
            by_kind.setdefault(f["kind"], []).append(1 if hit else 0)
        out[m] = {
            "precision": round(tp / max(1, tp + fp), 3),
            "recall": round(tp / max(1, n_dev), 3),
            "detections": tp,
            "false_alarms": fp,
            "latency_median_s": float(np.median(latency)) if latency else None,
            "latency_p90_s": float(np.quantile(latency, 0.9)) if latency else None,
            "recall_by_kind": {k: round(float(np.mean(v)), 3) for k, v in sorted(by_kind.items())},
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--network", type=Path, default=BACKEND.parent / "airspace" / "central-fl-v1.geojson")
    ap.add_argument("--scenarios", type=Path, default=BACKEND / "scenarios")
    ap.add_argument("--scenario", type=Path, default=BACKEND / "scenarios" / "rogue_aircraft.yaml")
    ap.add_argument("--train-seeds", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--seeds", type=int, nargs="+", default=list(range(201, 221)), help="held-out rogue seeds")
    ap.add_argument("--forecaster", choices=["aerolink-gbm", "cv"], default="aerolink-gbm")
    ap.add_argument("--data", type=Path, default=DATA_DIR)
    ap.add_argument("--max-train-segments", type=int, default=400)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args(argv)
    rng = np.random.default_rng(a.seed)
    network = Network.from_geojson(a.network)
    forecaster = load_forecaster(a.forecaster, network)

    days = list_days(a.data)
    if len(days) < 2:
        print("fewer than 2 real days: run E5.1 first")
        return 1
    train_days, _ = split_days(days)
    X_real = clean_real_features(train_days, a.data, forecaster, a.max_train_segments, a.seed)
    anomaly = train_anomaly_model(X_real, a.seed)
    anomaly.manifest.update({"forecaster": a.forecaster, "train_days": train_days})
    print(f"anomaly model: {len(X_real):,} clean real samples", flush=True)
    X_tol, y_tol = clean_sim_tolerance_data(network, sorted(a.scenarios.glob("*.yaml")), a.train_seeds, rng)
    tolerance = train_tolerance_model(X_tol, y_tol, a.seed)
    tolerance.manifest.update({"sim_train_seeds": a.train_seeds})
    print(f"route tolerance: {len(y_tol):,} clean simulated samples (median {np.median(y_tol):.0f} m off route)")
    models = OffRouteModels(tolerance, anomaly)
    print(f"wrote {models.save(a.model_dir)}", flush=True)

    scenario = load_scenario(a.scenario)
    results: list[dict] = []
    for seed in a.seeds:
        for f in flights(run_simulation(network, scenario, seed=seed), network):
            t, xyz = f["t"], f["xyz"]
            kind, i0 = "none", None
            if rng.random() < 0.5:
                kind = str(rng.choice(["turn", "drift", "slow"]))
                i0 = int(rng.integers(90, len(t) - 120))
                xyz = inject(xyz, i0, kind, rng)
            xyz = add_noise(xyz, rng, 5.0)
            graphs = [graph_context(network, f["path"], float(p[0]), float(p[1]))[0] for p in xyz]
            window = None
            if hasattr(forecaster, "eta") and forecaster.eta is not None:
                eta = forecaster.predict_eta(f["path"], f["dep_t"], f["vehicle"]).etas[-1]
                window = (eta.eta_p50, eta.eta_p90)
            d = detect(
                xyz,
                t,
                route_polyline(network, f["path"]),
                graphs,
                forecaster,
                models,
                f["vehicle"],
                vehicle_cfg(f["vehicle"]).cruise_speed_mps,
                window,
            )
            flags = {
                "fixed_bounds": d.fixed,
                "tolerance": d.tolerance,
                "schedule": d.schedule,
                "band": d.band,
                "anomaly": d.anomaly,
                "ai": d.ai,
            }
            results.append({"kind": kind, "i0": i0, "flags": flags})
        print(f"  seed {seed}: {len(results)} flights so far", flush=True)

    n_dev = sum(1 for f in results if f["i0"] is not None)
    report = {
        "scenario": scenario.name,
        "held_out_seeds": a.seeds,
        "forecaster": a.forecaster,
        "flights": len(results),
        "injected_deviations": n_dev,
        "deviation_kinds": {k: sum(1 for f in results if f["kind"] == k) for k in ["turn", "drift", "slow"]},
        "detection_window_s": WINDOW_S,
        "models": {"anomaly": anomaly.manifest, "tolerance": tolerance.manifest},
        "results": score_flights(results),
    }
    print(f"\n{len(results)} flights, {n_dev} injected deviations")
    print(f"{'method':<14}{'precision':>10}{'recall':>8}{'latency':>9}{'p90':>7}   recall turn/drift/slow")
    for m, r in report["results"].items():
        k = r["recall_by_kind"]
        lat = "-" if r["latency_median_s"] is None else f"{r['latency_median_s']:.0f}s"
        p90 = "-" if r["latency_p90_s"] is None else f"{r['latency_p90_s']:.0f}s"
        print(
            f"{m:<14}{r['precision']:>10.0%}{r['recall']:>8.0%}{lat:>9}{p90:>7}   "
            f"{k.get('turn', 0):.0%}/{k.get('drift', 0):.0%}/{k.get('slow', 0):.0%}"
        )
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

#### New file: `backend/src/aerolink/routing/aerolink.py`

*What this file does:* The full AeroLink route engine with switches for each AI piece. For E5.5 it provides `recalculate` (flight → `off_route`, fresh options from the current position) and adds off-route aircraft to the traffic forecast. E5.7 uses its switches for the ablations.

Open the file (command):

```bash
code src/aerolink/routing/aerolink.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Full AeroLink route engine, switchable piece by piece for the ablations (PLAN E5.7, ARCHITECTURE §8, §9.3).

Builds on the A1 engine (``routing/a1.py``) and adds, one configuration at a time:

| Config | Adds |
|---|---|
| A1 | B1 + AI ETA windows (``cv`` ETAs in simulation, as in M4: see PLAN E4.2 note) |
| A2 | ETA windows from ``aerolink-gbm`` |
| A3 | + demand forecaster: expected (not yet planned) flights in the traffic forecast |
| A4 | + off-route detection: aircraft not following a plan are spread over the nodes they can reach |
| A5 | + risk ranking (mu = 600 s per unit of bunching risk) + en-route suggestions |

Unlike the M4 wrapper, this engine sees the live traffic picture: the simulator binds it
to the world (``bind``), so every request is scored against the flights already planned.
It still only predicts and ranks; it never approves, assigns or holds anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from aerolink.contracts import VehicleType
from aerolink.demand.forecaster import DemandModel
from aerolink.forecast.gbm import GBMForecaster
from aerolink.forecast.interface import Forecaster
from aerolink.network import Network
from aerolink.offroute.detector import reachable_mass
from aerolink.routing.a1 import A1Advisor, A1Config, A1RouteOption
from aerolink.traffic import forecast_traffic
from aerolink.vehicle import vehicle_cfg

if TYPE_CHECKING:
    from aerolink.sim.state import AircraftState, FlightPlan, WorldState


@dataclass(frozen=True)
class AeroLinkConfig:
    demand: bool = False  # A3
    off_route: bool = False  # A4
    risk_ranking: bool = False  # A5
    en_route: bool = False  # A5
    a1: A1Config = field(default_factory=A1Config)


ABLATIONS: dict[str, AeroLinkConfig] = {
    "A1": AeroLinkConfig(),
    "A2": AeroLinkConfig(),
    "A3": AeroLinkConfig(demand=True),
    "A4": AeroLinkConfig(demand=True, off_route=True),
    "A5": AeroLinkConfig(demand=True, off_route=True, risk_ranking=True, en_route=True),
}


class AeroLinkAdvisor(A1Advisor):
    def __init__(
        self,
        network: Network,
        forecaster: Forecaster,
        cfg: AeroLinkConfig,
        demand: DemandModel | None = None,
        k: int = 3,
    ):
        a1 = cfg.a1 if cfg.risk_ranking else replace(cfg.a1, mu_risk_s=0.0)
        super().__init__(network, forecaster, a1, k)
        self.config = cfg
        self.demand = demand
        self.world: WorldState | None = None
        self.controller: Any = None
        self.start_local_hour = 0.0
        self.flight_status: dict[str, str] = {}  # flight id -> "off_route" once flagged
        self._expected_cache: dict[int, list] = {}
        self._travel: dict[tuple[str, str], float | None] = {}

    # --- live traffic picture -------------------------------------------------------------

    def bind(self, world: WorldState, controller: Any) -> None:
        """Called by the simulator once the world exists (``sim/runner.py``)."""
        self.world = world
        self.controller = controller
        self.start_local_hour = float(controller.cfg.start_local_hour)

    def local_hour(self, t_s: float) -> float:
        return (self.start_local_hour + t_s / 3600.0) % 24.0

    def planned_flights(self) -> list[FlightPlan]:
        if self.world is None:
            return []
        return [ac.active_flight for ac in self.world.aircraft.values() if ac.active_flight is not None]

    def _travel_time(self, a: str | None, b: str) -> float | None:
        if a is None:
            return 0.0
        if (a, b) not in self._travel:
            d = self.network.route_distance_m(a, b, "passenger")
            self._travel[(a, b)] = None if d is None else d / vehicle_cfg("passenger").cruise_speed_mps
        return self._travel[(a, b)]

    def expected_flights(self, t_s: float) -> list[tuple[float, str, float, float, float]]:
        out: list[tuple[float, str, float, float, float]] = []
        if self.config.demand and self.demand is not None:
            key = int(t_s // 900)
            if key not in self._expected_cache:
                self._expected_cache[key] = self.demand.expected_flights(self.local_hour(t_s), t_s, self._travel_time)
            out += self._expected_cache[key]
        if self.config.off_route:
            out += self.off_route_mass(t_s)
        return out

    def off_route_mass(self, t_s: float) -> list[tuple[float, str, float, float, float]]:
        """Aircraft flying without an AeroLink plan (background / rogue traffic), spread over reachable nodes."""
        out = []
        for bg in getattr(self.controller, "background_aircraft", []) or []:
            times = bg.times_s
            if not times or not (times[0] <= t_s <= times[-1]):
                continue
            k = max(i for i in range(len(times)) if times[i] <= t_s)
            k = min(k, len(times) - 2)
            a, b = self.network.nodes[bg.path[k]], self.network.nodes[bg.path[k + 1]]
            w = (t_s - times[k]) / max(1e-6, times[k + 1] - times[k])
            lat, lon = a["lat"] + w * (b["lat"] - a["lat"]), a["lon"] + w * (b["lon"] - a["lon"])
            out += reachable_mass(self.network, lat, lon, bg.speed_mps, t_s)
        return out

    # --- route requests -------------------------------------------------------------------

    def request_routes(
        self,
        origin: str,
        destination: str,
        vehicle_type: VehicleType,
        t_s: float,
        planned: list[FlightPlan] | None = None,
    ) -> list[A1RouteOption]:
        paths = self._candidate_paths(origin, destination, vehicle_type)
        if not paths:
            return []
        planned = self.planned_flights() if planned is None else planned
        traffic = forecast_traffic(list(planned), self._capacities(planned), self.expected_flights(t_s))
        options = []
        for path in paths:
            if isinstance(self.forecaster, GBMForecaster):  # aerolink-gbm uses traffic and time of day
                nodes = set(path[1:])
                self.forecaster.traffic_ahead = float(sum(1 for p in planned if nodes & set(p.eta_by_node)))
                self.forecaster.local_hour = self.local_hour(t_s)
            options.append(
                self._score(
                    path, self.forecaster.predict_eta(path, t_s, vehicle_type), vehicle_type, t_s, traffic, planned
                )
            )
        options.sort(key=lambda o: o.total_predicted_s)
        return options

    def suggest(
        self, ac: AircraftState, from_node: str, t_s: float, planned: list[FlightPlan] | None = None
    ) -> A1RouteOption | None:
        if not self.config.en_route:
            return None
        return super().suggest(ac, from_node, t_s, planned)

    def recalculate(
        self, flight_id: str, lat: float, lon: float, destination: str, vehicle_type: VehicleType, t_s: float
    ) -> list[A1RouteOption]:
        """Off route: mark the flight ``off_route`` and return fresh options from the nearest node."""
        self.flight_status[flight_id] = "off_route"
        k = math.cos(math.radians(lat))
        nearest = min(
            (n for n in self.network.nodes if self.network.graph.degree(n) > 0),
            key=lambda n: (self.network.nodes[n]["lat"] - lat) ** 2 + ((self.network.nodes[n]["lon"] - lon) * k) ** 2,
        )
        return self.request_routes(nearest, destination, vehicle_type, t_s)


class AeroLinkRouteAdvisorWrapper:
    """``controller.RouteAdvisor`` adapter (paths in, paths out) that the simulator binds to its world."""

    def __init__(self, advisor: AeroLinkAdvisor):
        self.advisor = advisor

    def bind(self, world: WorldState, controller: Any) -> None:
        self.advisor.bind(world, controller)

    def request_routes(self, origin: str, destination: str, vehicle_type: VehicleType, t_s: float) -> list[list[str]]:
        return [o.path for o in self.advisor.request_routes(origin, destination, vehicle_type, t_s)]

    def suggest(self, ac: AircraftState, from_node: str, t_s: float) -> list[str] | None:
        opt = self.advisor.suggest(ac, from_node, t_s)
        return opt.path if opt else None

    def predict(self, origin: str, destination: str, vehicle_type: VehicleType, t_s: float) -> list[A1RouteOption]:
        return self.advisor.request_routes(origin, destination, vehicle_type, t_s)
```

#### New file: `backend/tests/test_e55_offroute.py`

*What this file does:* Checks the "3 in a row" logic, the distance-from-route maths, that the reachable spread adds up to exactly one aircraft, that recalculating marks the flight `off_route`, and the evaluation command end to end.

Open the file (command):

```bash
code tests/test_e55_offroute.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""E5.5: off-route checks, end-to-end evaluation, recalculation and reachable mass."""

import json
from pathlib import Path

import numpy as np
import pytest

from aerolink.forecast.cv import CVForecaster
from aerolink.network import Network
from aerolink.offroute.detector import consecutive, cross_track_m, reachable_mass, route_polyline
from aerolink.offroute.evaluate import main
from aerolink.realdata.synthetic_days import make_days
from aerolink.routing.aerolink import ABLATIONS, AeroLinkAdvisor


def test_consecutive_needs_three_and_clears_slowly():
    f = np.array([1, 1, 0, 1, 1, 1, 0, 1, 0, 0, 0], dtype=bool)
    assert consecutive(f).tolist() == [0, 0, 0, 0, 0, 1, 1, 1, 1, 0, 0]


def test_cross_track_follows_corridor_shape(fl_network: Network):
    path = fl_network.shortest_paths("ev-lal", "ev-water-st", "passenger", k=1)[0]
    route = route_polyline(fl_network, path)
    lat, lon = route[len(route) // 2]
    assert cross_track_m(lat, lon, route) < 1.0
    assert cross_track_m(lat + 0.02, lon, route) > 300  # ~2 km north of a point on the route


def test_reachable_mass_is_one_aircraft(fl_network: Network):
    n = fl_network.nodes["ev-lal"]
    mass = reachable_mass(fl_network, n["lat"], n["lon"], 50.0, 100.0, horizon_s=900)
    assert mass and sum(p for p, *_ in mass) == pytest.approx(1.0)
    assert any(node == "ev-lal" for _, node, *_ in mass)


def test_recalculate_marks_off_route(fl_network: Network):
    adv = AeroLinkAdvisor(fl_network, CVForecaster(fl_network), ABLATIONS["A4"])
    n = fl_network.nodes["ev-lal"]
    opts = adv.recalculate("flight-1", n["lat"] + 0.001, n["lon"], "ev-water-st", "passenger", 0.0)
    assert adv.flight_status["flight-1"] == "off_route"
    assert opts and opts[0].path[-1] == "ev-water-st"


def test_evaluate_end_to_end(tmp_path: Path):
    data = tmp_path / "adsblol"
    make_days(data, days=3, flights_per_day=4)
    out = tmp_path / "e55.json"
    args = ["--data", str(data), "--forecaster", "cv", "--train-seeds", "1", "--seeds", "201", "202"]
    assert main([*args, "--model-dir", str(tmp_path / "m"), "--out", str(out)]) == 0
    r = json.loads(out.read_text())
    assert set(r["results"]) == {"fixed_bounds", "tolerance", "schedule", "band", "anomaly", "ai"}
    assert (tmp_path / "m" / "offroute.joblib").exists()
```

### 9b. Run the checks

Command:

```bash
uv run ruff format
uv run ruff check --fix
uv run pytest -q tests/test_e55_offroute.py
```

Expect **`5 passed`**.

### 9c. Run the evaluation

Command:

```bash
uv run python -m aerolink.offroute.evaluate
```

*What it does:* trains the two models (saved to `backend/models/offroute/offroute.joblib`), then runs and scores the labelled flights. About **5–15 minutes**. Example output:

```text
40 flights, 22 injected deviations
method         precision  recall  latency    p90   recall turn/drift/slow
fixed_bounds         93%     64%      32s    48s   100%/100%/0%
tolerance            93%     64%      28s    32s   100%/100%/0%
schedule             75%     27%     147s   258s   40%/0%/50%
band                 46%     23%      78s   163s   60%/0%/25%
anomaly              22%     41%      77s   229s   80%/33%/25%
ai                   75%     82%      30s   103s   100%/100%/50%
```

How to read it: `fixed_bounds` is the classical baseline; `ai` is AeroLink's combined rule; the other rows show each check on its own. Look especially at the **slow** column: the fixed bound can't see a slow-down at all. Your numbers will differ (they depend on your real data and trained model). Report whatever comes out.

### 9d. Commit

Command:

```bash
git add src/aerolink/offroute src/aerolink/routing/aerolink.py tests/test_e55_offroute.py models/offroute eval-results/offroute-e5.5.json
git commit -F - <<'EOF'
feat: E5.5 off-route detection with learned tolerance

* feat: learned route tolerance, ETA-window schedule check, position band, IsolationForest
* feat: labelled deviations on rogue_aircraft; precision/recall/latency vs fixed 300 m bound
* feat: recalculate from current position (off_route) and reachable-set mass in traffic
EOF
git push
```

**E5.5 done.**

---

## Part 10: E5.6 Route Summary (LLM)

**What you're building:** the one-line summary above the route options, like a maps app: *"Fastest route: about 21 min to Water Street, low traffic. The next option takes 4 min longer."*

**How it stays safe:**
- An LLM (any model on [OpenRouter](https://openrouter.ai), which offers many AI models through one standard API) only ever sees a small table of facts from the route options: minutes, waits, traffic levels, arrival times, place names.
- **Every number in its answer is checked** against that table (differences between options are allowed too, like "4 min longer"). One wrong number, more than two sentences, or any API error, and the answer is thrown away and a template sentence is shown instead. So **no mismatched number ever reaches a user**, whatever the LLM does.
- The LLM never influences which route is recommended.

**What gets measured:** the **pass rate**: the share of LLM answers that pass the check and would be shown.

### 10a. Create the folder and files

Command:

```bash
mkdir -p src/aerolink/summary
```

#### New file: `backend/src/aerolink/summary/__init__.py`

*What this file does:* Marks the folder as a package.

Open the file (command):

```bash
code src/aerolink/summary/__init__.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Plain-language route summaries (PLAN E5.6, ARCHITECTURE §7.5)."""
```

#### New file: `backend/src/aerolink/summary/llm.py`

*What this file does:* Builds the facts table and the list of allowed numbers, calls OpenRouter, runs the number check, and falls back to the template.

Open the file (command):

```bash
code src/aerolink/summary/llm.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Two-sentence route summary from an LLM, with a number check (PLAN E5.6, ARCHITECTURE §7.5).

The LLM (any model on OpenRouter, an OpenAI-compatible API) only ever sees a small table of
facts taken from the route response: minutes, waits, traffic levels, arrival times and
place names. Every number in its answer must be one of the numbers in that table (or a
difference between two of them); otherwise the text is thrown away and a template
sentence is used. The LLM never influences routing.

Settings (environment, see ``.env.example``): ``LLM_BASE_URL``, ``LLM_MODEL``, ``LLM_API_KEY``.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta

SYSTEM_PROMPT = (
    "You write the one-line summary a navigation app shows above route options for a small "
    "aircraft. Write at most two short sentences. Use ONLY the facts given; never invent "
    "numbers, times or places. Prefer minutes. Mention why the first option is recommended."
)
NUMBER = re.compile(r"\d+(?::\d\d)?(?:\.\d+)?")


@dataclass(frozen=True)
class Facts:
    lines: list[str]  # what the LLM sees
    allowed: set[str]  # every number it may write


def _hhmm(iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%H:%M")


def facts_from_response(response: dict, names: dict[str, str] | None = None) -> Facts:
    """Facts table and allowed numbers from a ``route-response`` (contracts v4.0)."""
    names = names or {}
    lines: list[str] = []
    minutes: list[int] = []
    allowed: set[str] = set()
    for r in response["routes"]:
        mins = round(r["est_time_s"] / 60)
        wait = round(r["expected_wait_s"] / 60)
        risk = round(100 * r.get("bunching_risk", 0.0))
        dest = r["eta"][-1] if r.get("eta") else None
        place = names.get(dest["node"], dest["node"]) if dest else "destination"
        arrive = _hhmm(dest["eta_p50"]) if dest else None
        window = f"{_hhmm(dest['eta_p10'])}-{_hhmm(dest['eta_p90'])}" if dest else None
        lines.append(
            f"option {r['rank']} ({r['label']}): {mins} min flying, {wait} min expected wait, "
            f"traffic {r['traffic']}, bunching risk {risk}%, arrive {place} at {arrive} (likely {window}); "
            f"note: {r.get('reason') or 'none'}"
        )
        minutes.append(mins + wait)
        allowed |= {str(r["rank"]), str(mins), str(wait), str(mins + wait), str(risk)}
        allowed |= set(NUMBER.findall(place))  # e.g. "hp-61fl", "I-4"
        if dest:
            for k in ("eta_p10", "eta_p50", "eta_p90"):
                hhmm = _hhmm(dest[k])
                allowed |= {hhmm, hhmm.lstrip("0") or "0"}
        allowed |= set(NUMBER.findall(r.get("reason") or ""))
    for a in minutes:
        for b in minutes:
            if a > b:
                allowed.add(str(a - b))
    # numbers inside place names (e.g. "I-4", "SR-60") are allowed too
    for n in names.values():
        allowed |= set(NUMBER.findall(n))
    allowed.add(str(len(response["routes"])))
    return Facts(lines, allowed)


def check_numbers(text: str, facts: Facts) -> list[str]:
    """Numbers in ``text`` that are not in the facts (empty list = passes)."""
    return [n for n in NUMBER.findall(text) if n not in facts.allowed and n.lstrip("0") not in facts.allowed]


def template_summary(response: dict, names: dict[str, str] | None = None) -> str:
    """Deterministic fallback, always correct."""
    names = names or {}
    routes = response["routes"]
    if not routes:
        return "No route is available right now."
    best = routes[0]
    dest = best["eta"][-1] if best.get("eta") else None
    place = names.get(dest["node"], dest["node"]) if dest else "your destination"
    total = round(best["est_time_s"] / 60) + round(best["expected_wait_s"] / 60)  # same rounding as the facts
    text = f"Fastest route: about {total} min to {place}, {best['traffic']} traffic."
    if len(routes) > 1:
        other = round(routes[1]["est_time_s"] / 60) + round(routes[1]["expected_wait_s"] / 60)
        diff = other - total
        text += f" The next option takes {diff} min longer." if diff > 0 else " The next option takes about as long."
    return text


def call_llm(facts: Facts, timeout_s: float = 20.0) -> str:
    base = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL", "")
    key = os.environ.get("LLM_API_KEY", "")
    if not model or not key:
        raise RuntimeError("set LLM_MODEL and LLM_API_KEY (see .env.example)")
    body = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 120,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Facts:\n" + "\n".join(facts.lines)},
        ],
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "X-Title": "AeroLink"},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


@dataclass(frozen=True)
class Summary:
    text: str
    source: str  # "llm" or "template"
    rejected: str | None = None  # LLM text that failed the check
    bad_numbers: tuple[str, ...] = ()


def summarize(response: dict, names: dict[str, str] | None = None, llm=call_llm) -> Summary:
    """LLM summary if it passes the number check, else the template. Never raises."""
    facts = facts_from_response(response, names)
    try:
        text = llm(facts)
    except Exception as e:  # noqa: BLE001 - network, quota, bad key: the user still gets a summary
        return Summary(template_summary(response, names), "template", rejected=f"error: {e}")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    bad = check_numbers(text, facts)
    if bad or not text or len(sentences) > 2 or len(text) > 300:
        return Summary(template_summary(response, names), "template", rejected=text, bad_numbers=tuple(bad))
    return Summary(text, "llm")


def iso(base: datetime, seconds: float) -> str:
    return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
```

#### New file: `backend/src/aerolink/summary/evaluate.py`

*What this file does:* The command you run. Builds 50 realistic route responses, asks the LLM for each summary, and writes the pass rate to `eval-results/summary-e5.6.json`.

Open the file (command):

```bash
code src/aerolink/summary/evaluate.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Pass rate of LLM route summaries (PLAN E5.6).

    uv run python -m aerolink.summary.evaluate                 # needs LLM_MODEL + LLM_API_KEY
    uv run python -m aerolink.summary.evaluate --template-only # no API call (checks the plumbing)

Builds ``--cases`` route responses from the corridor network (random origin/destination
pairs, A1 route engine, ``cv`` ETAs), asks the LLM for each summary and runs the number
check. The pass rate is the share of LLM answers shown to the user; every failure falls
back to the template, so no mismatched number ever reaches a user.

Results: ``backend/eval-results/summary-e5.6.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from aerolink.forecast.cv import CVForecaster
from aerolink.network import Network
from aerolink.routing.a1 import A1Advisor, A1RouteOption
from aerolink.summary.llm import call_llm, check_numbers, facts_from_response, iso, summarize, template_summary

BACKEND = Path(__file__).resolve().parents[3]
DEFAULT_OUT = BACKEND / "eval-results" / "summary-e5.6.json"


def to_response(options: list[A1RouteOption], network: Network, t0: datetime) -> dict:
    """The fields of a contracts v4.0 ``route-response`` the summary uses."""
    routes = []
    for rank, o in enumerate(options, start=1):
        dest = o.path[-1]
        lo, hi = o.eta_window_s
        routes.append(
            {
                "rank": rank,
                "label": "fastest" if rank == 1 else "alternative",
                "distance_m": round(network.route_length_m(o.path)),
                "est_time_s": round(o.cost_terms["est_time_s"]),
                "expected_wait_s": round(o.cost_terms["expected_wait_s"]),
                "bunching_risk": round(o.bunching_risk, 2),
                "traffic": o.traffic,
                "cost_terms": o.cost_terms,
                "reason": None,
                "eta": [{"node": dest, "eta_p10": iso(t0, lo), "eta_p50": iso(t0, o.eta_s), "eta_p90": iso(t0, hi)}],
            }
        )
    return {"status": "ok" if routes else "no_route", "message_id": str(uuid.uuid4()), "routes": routes}


def _no_llm(facts) -> str:
    raise RuntimeError("template-only run")


def build_cases(network: Network, n: int, seed: int) -> list[dict]:
    rng = np.random.default_rng(seed)
    advisor = A1Advisor(network, CVForecaster(network))
    t0 = datetime(2026, 11, 20, 13, 0, tzinfo=UTC)
    cases = []
    tries = 0
    while len(cases) < n and tries < n * 50:
        tries += 1
        vt = "medical" if rng.random() < 0.3 else "passenger"
        sites = network.nodes_for(vt)
        o, d = (str(x) for x in rng.choice(sites, 2, replace=False))
        options = advisor.request_routes(o, d, vt, float(rng.uniform(0, 3600)))
        if options:
            cases.append(to_response(options, network, t0))
    return cases


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--network", type=Path, default=BACKEND.parent / "airspace" / "central-fl-v1.geojson")
    ap.add_argument("--cases", type=int, default=50)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--template-only", action="store_true", help="skip the LLM (the pass rate is then 0%)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args(argv)
    if not a.template_only and not (os.environ.get("LLM_MODEL") and os.environ.get("LLM_API_KEY")):
        print("LLM_MODEL and LLM_API_KEY are not set. Export them (see the guide) or use --template-only.")
        return 1

    network = Network.from_geojson(a.network)
    names = {nid: p.get("name", nid) for nid, p in network.nodes.items()}
    cases = build_cases(network, a.cases, a.seed)
    results = []
    for i, resp in enumerate(cases):
        s = summarize(resp, names, llm=_no_llm if a.template_only else call_llm)
        results.append(
            {
                "case": i,
                "source": s.source,
                "summary": s.text,
                "rejected": s.rejected,
                "bad_numbers": list(s.bad_numbers),
                "facts": facts_from_response(resp, names).lines,
            }
        )
        print(f"[{s.source:>8}] {s.text}", flush=True)
    passed = sum(r["source"] == "llm" for r in results)
    errors = sum(1 for r in results if str(r["rejected"] or "").startswith("error:"))
    mismatched = sum(1 for r in results if r["bad_numbers"])
    template_ok = all(not check_numbers(template_summary(c, names), facts_from_response(c, names)) for c in cases)
    report = {
        "model": None if a.template_only else os.environ.get("LLM_MODEL"),
        "cases": len(results),
        "llm_pass_rate": round(passed / max(1, len(results)), 3),
        "rejected_number_mismatch": mismatched,
        "rejected_other": len(results) - passed - mismatched - errors,
        "api_errors": errors,
        "templates_pass_number_check": template_ok,
        "mismatched_numbers_shown_to_users": 0,
        "results": results,
    }
    print(
        f"\nLLM pass rate: {passed}/{len(results)} ({report['llm_pass_rate']:.0%}); "
        f"{mismatched} rejected for wrong numbers, {errors} API errors; all shown text passed the check."
    )
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

#### New file: `backend/tests/test_e56_summary.py`

*What this file does:* Proves a correct LLM answer is used, an answer with an invented number is replaced by the template, an API error falls back to the template, and the template itself always passes. No API key needed.

Open the file (command):

```bash
code tests/test_e56_summary.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""E5.6: route summaries never show a number that isn't in the route data."""

import json
from pathlib import Path

from aerolink.summary.evaluate import main
from aerolink.summary.llm import check_numbers, facts_from_response, summarize, template_summary

REPO = Path(__file__).resolve().parents[2]
RESPONSE = json.loads((REPO / "contracts/examples/route-response/valid/ok-two-options.json").read_text())


def test_facts_allow_route_numbers_and_differences():
    facts = facts_from_response(RESPONSE)
    assert {"19", "15:19", "3"} <= facts.allowed  # 1140 s = 19 min; arrival 15:19; 22 - 19 = 3 min
    assert check_numbers("Arrive 15:19, about 19 min, 3 min faster.", facts) == []
    assert check_numbers("Arrive 15:45 after 25 min.", facts) == ["15:45", "25"]


def test_good_llm_text_is_used():
    s = summarize(RESPONSE, llm=lambda facts: "Fastest: 19 min, arriving 15:19. The other option is 3 min slower.")
    assert s.source == "llm"


def test_wrong_number_falls_back_to_template():
    s = summarize(RESPONSE, llm=lambda facts: "Fastest route takes 12 min.")
    assert s.source == "template" and s.bad_numbers == ("12",)
    assert check_numbers(s.text, facts_from_response(RESPONSE)) == []


def test_api_error_falls_back_to_template():
    def broken(facts):
        raise TimeoutError("no network")

    s = summarize(RESPONSE, llm=broken)
    assert s.source == "template" and s.text == template_summary(RESPONSE)


def test_template_only_run(tmp_path: Path):
    out = tmp_path / "e56.json"
    assert main(["--template-only", "--cases", "5", "--out", str(out)]) == 0
    r = json.loads(out.read_text())
    assert r["cases"] == 5 and r["templates_pass_number_check"] is True
```

### 10b. Run the checks

Command:

```bash
uv run ruff format
uv run ruff check --fix
uv run pytest -q tests/test_e56_summary.py
uv run python -m aerolink.summary.evaluate --template-only --cases 5 --out /tmp/e56-test.json
```

Expect **`5 passed`**, then 5 template summaries and `LLM pass rate: 0/5 (0%) ... 5 API errors` (correct: `--template-only` never calls the LLM; it only checks the plumbing).

### 10c. Set up the OpenRouter key

You need an OpenRouter API key with a little credit on it. **Ask Joshua for the project's key** (don't create a paid account yourself unless he asks you to).

> **Security:** an API key is like a password that spends money. Never paste it into a file you commit, a pull request, a chat, or a screenshot. The commands below keep it only in this terminal's memory; it disappears when you close the terminal.

Command (replace the two values: the model ID is copied from [openrouter.ai/models](https://openrouter.ai/models), for example `openai/gpt-4o-mini`; use the one Joshua tells you if he names one):

```bash
export LLM_MODEL="paste-the-model-id-here"
export LLM_API_KEY="paste-the-key-here"
```

*What it does:* `export` sets a value for this terminal session only. `LLM_BASE_URL` already defaults to OpenRouter.

(Optional, so you don't have to re-type it after a restart: store both as **Codespaces secrets** at [github.com/settings/codespaces](https://github.com/settings/codespaces) → **New secret**, give them access to the AeroLink repository, then restart the Codespace.)

### 10d. Measure the pass rate

Command:

```bash
uv run python -m aerolink.summary.evaluate
```

*What it does:* 50 summaries, one API call each (about a minute, and a few cents). Each line shows `[     llm]` (passed and shown) or `[template]` (rejected or failed). It ends with:

```text
LLM pass rate: 46/50 (92%); 3 rejected for wrong numbers, 1 API errors; all shown text passed the check.
```

(Example numbers.) If you see many API errors, check the key and model ID, and that the key has credit.

> **Revised: before committing, make sure the key didn't end up in the results file** (an error message can sometimes contain it). Command:
>
> ```bash
> grep -c "$LLM_API_KEY" eval-results/summary-e5.6.json
> ```
>
> It must print **`0`**. If it prints anything else, don't commit. Tell Joshua, because the key needs replacing.

### 10e. Commit

Command:

```bash
git add src/aerolink/summary tests/test_e56_summary.py eval-results/summary-e5.6.json
git commit -F - <<'EOF'
feat: E5.6 LLM route summary with number check

* feat: OpenRouter summary from structured route facts only
* feat: reject any summary with a number not in the data; template fallback
* feat: pass-rate evaluation over 50 route responses
EOF
git push
```

**E5.6 done.**

---

## Part 11: E5.7 Full Ablations and Docs

**What you're building:** the final scoreboard. The closed-loop simulator flies every scenario once per configuration, each adding one AI piece:

| Config | What it is |
|---|---|
| B0 | shortest path, no traffic information |
| B1 | classical navigation, no AI (the baseline to beat) |
| A1 | B1 + AI arrival windows (`cv` ETAs in simulation, as in M4) |
| A2 | arrival windows from `aerolink-gbm` |
| A3 | A2 + demand forecaster (expected flights in the traffic picture) |
| A4 | A3 + off-route detection (aircraft not following a plan are spread over where they could go) |
| A5 | **full AeroLink**: A4 + risk-aware ranking + en-route "faster route" suggestions |
| A5@50% | A5 when only half of the suggestions are accepted (sensitivity check) |

One more simulator change makes this fair: the M4 route engine was never shown the flights already planned, so it couldn't react to traffic. The new engine is **bound to the simulated world** when a run starts, so every request sees the live traffic picture. That's the second (and last) change to `sim/runner.py`.

### 11a. Replace the simulator runner (final version)

#### Replace the whole file: `backend/src/aerolink/sim/runner.py`

*What changes:* Same as the E5.1 version plus three lines: if the route engine has a `bind` method, the simulator hands it the world and controller at the start of the run.

1. Open the file (command):

```bash
code src/aerolink/sim/runner.py
```

2. Click inside the file, press **Ctrl + A** (select everything), then **Delete**.
3. Paste the box below with **Ctrl + V**, then **Ctrl + S**.

File contents:

```python
"""Fixed-step simulation loop (PLAN E3.2).

``run_simulation(network, scenario, seed)`` is a pure function: the same inputs
always give byte-identical telemetry and events (see ``SimulationResult.digest``).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import numpy as np

from aerolink.contracts import SCHEMA_VERSION
from aerolink.network import Network
from aerolink.sim.background import (
    BackgroundAircraft,
    BackgroundState,
    generate_tracks,
    step_background,
)
from aerolink.sim.clock import SimClock
from aerolink.sim.controller import Controller, ControllerConfig, RouteAdvisor, ShortestPathAdvisor
from aerolink.sim.fleet import create_fleet
from aerolink.sim.invariants import InvariantChecker, Violation
from aerolink.sim.pads import init_pads
from aerolink.sim.scenarios import Scenario
from aerolink.sim.state import WindField, WorldState
from aerolink.sim.telemetry import telemetry_record


@dataclass
class SimulationResult:
    scenario: Scenario
    seed: int
    telemetry: list[dict]  # noisy, contract-shaped Telemetry records
    events: list[dict]  # structured event log
    truth: list[dict]  # noise-free per-step state of every aircraft
    final_world: WorldState
    steps: int = 0
    stats: dict = field(default_factory=dict)
    violations: list[Violation] = field(default_factory=list)  # only with check_invariants=True

    def digest(self, precision: int = 6) -> str:
        """SHA-256 over telemetry + events + truth; equal iff runs are identical.

        Floats are rounded to ``precision`` decimal places so tiny platform
        differences in numpy/libm trig/exp don't break cross-platform tests.
        """
        h = hashlib.sha256()
        for part in (self.telemetry, self.events, self.truth):
            h.update(json.dumps(_round_floats(part, precision), sort_keys=True, separators=(",", ":")).encode())
        return h.hexdigest()


def _round_floats(obj, precision: int):
    """Recursively round floats in dicts/lists for stable digests."""
    if isinstance(obj, float):
        return round(obj, precision)
    if isinstance(obj, dict):
        return {k: _round_floats(v, precision) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, precision) for v in obj]
    return obj


def build_world(network: Network, scenario: Scenario, seed: int) -> WorldState:
    rng = np.random.default_rng(seed)
    pads = init_pads(network, scenario.pad_capacity, scenario.stand_capacity, default_stand_count=True)
    fleet = create_fleet(
        network,
        pads,
        rng,
        passenger=scenario.fleet.get("passenger", 0),
        medical=scenario.fleet.get("medical", 0),
    )
    wind = WindField(
        speed_mps=scenario.wind.speed_mps,
        direction_deg=scenario.wind.direction_deg,
        gust_fraction=scenario.wind.gust_fraction,
    )
    return WorldState(t=0.0, aircraft=fleet, pads=pads, rng=rng, wind=wind)


def run_simulation(
    network: Network,
    scenario: Scenario,
    seed: int | None = None,
    advisor: RouteAdvisor | None = None,
    record_truth: bool = True,
    check_invariants: bool = False,
) -> SimulationResult:
    """Run ``scenario`` for ``duration_s``. ``seed`` overrides ``scenario.seed``.

    ``check_invariants`` runs the read-only ``InvariantChecker`` after every
    step and stores what it finds in ``result.violations``; output is unchanged.
    """
    seed = scenario.seed if seed is None else seed
    clock = SimClock.from_iso(scenario.start_time, dt_s=scenario.dt_s)
    world = build_world(network, scenario, seed)
    background_aircraft: list[BackgroundAircraft] = []
    background_states: list[BackgroundState] = []
    if scenario.world.background_traffic:
        if scenario.background_traffic.replay_file:
            from aerolink.realdata.replay import load_replay  # real flights (PLAN E5.1)

            background_aircraft = load_replay(
                Path(scenario.background_traffic.replay_file),
                network,
                scenario.start_time,
                scenario.duration_s,
                limit=scenario.background_traffic.count,
            )
        else:
            background_aircraft = generate_tracks(network, scenario.background_traffic, world.rng)
    cfg = ControllerConfig(
        dt_s=scenario.dt_s,
        start_local_hour=clock.local_hour(),
        turnaround_s=scenario.pad_turnaround_time_s,
        max_hold_s=scenario.max_hold_s,
        suggestion_acceptance_rate=scenario.suggestion_acceptance_rate,
        destination_change_probability=scenario.destination_change_probability,
        cruise_speed_sigma=scenario.cruise_speed_sigma,
        demand=scenario.demand,
        world=scenario.world,
        separation=scenario.separation,
        energy=scenario.energy,
        pad_booking=scenario.pad_booking,
        schedule=scenario.schedule,
        background_traffic=scenario.background_traffic,
    )
    controller = Controller(
        network,
        world,
        clock,
        advisor or ShortestPathAdvisor(network, k=scenario.route_options),
        cfg,
        background_aircraft=background_aircraft,
    )
    bind = getattr(controller.advisor, "bind", None)  # route engines that read live traffic (PLAN E5.7)
    if callable(bind):
        bind(world, controller)
    telemetry: list[dict] = []
    truth: list[dict] = []
    steps = math.ceil(scenario.duration_s / scenario.dt_s - 1e-9)
    checker = (
        InvariantChecker(network, scenario.dt_s, separation=cfg.separation if cfg.world.separation else None)
        if check_invariants
        else None
    )
    if checker is not None:
        checker.check_step(world)
    for _ in range(steps):
        if scenario.world.background_traffic:
            background_states = step_background(background_aircraft, world.t, network)
        controller.step()
        if scenario.world.background_traffic:
            ts = controller.clock.now()
            for st in background_states:
                if st.finished:
                    continue
                telemetry.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "message_id": str(UUID(bytes=world.rng.bytes(16), version=4)),
                        "aircraft_id": st.aircraft_id,
                        "vehicle_type": "background",
                        "timestamp": ts.isoformat().replace("+00:00", "Z"),
                        "position": {
                            "lat": round(st.position.lat, 8),
                            "lon": round(st.position.lon, 8),
                            "alt_msl_m": round(st.position.alt_msl_m, 2),
                        },
                        "motion": {
                            "ground_speed_mps": round(st.velocity_mps, 2),
                            "course_deg": round(st.heading_deg, 2),
                            "vertical_speed_mps": round(st.vertical_speed_mps, 2),
                        },
                        "fix": {"type": "3d", "hdop": 0.9, "satellites": 11, "replayed": True},
                        "active_flight_id": None,
                    }
                )
                if record_truth:
                    truth.append(
                        {
                            "t": round(world.t, 6),
                            "aircraft_id": st.aircraft_id,
                            "status": "background",
                            "node": None,
                            "lat": st.position.lat,
                            "lon": st.position.lon,
                            "alt_msl_m": st.position.alt_msl_m,
                            "velocity_mps": st.velocity_mps,
                            "vertical_speed_mps": st.vertical_speed_mps,
                            "heading_deg": st.heading_deg,
                            "battery_soc": 1.0,
                        }
                    )
        if checker is not None:
            checker.check_step(world)
        ts = controller.clock.now()
        for aid in sorted(world.aircraft):
            ac = world.aircraft[aid]
            rec = telemetry_record(ac, ts, scenario.telemetry_noise)
            if rec is not None:
                telemetry.append(rec)
            if record_truth:
                truth.append(
                    {
                        "t": round(world.t, 6),
                        "aircraft_id": aid,
                        "status": ac.status,
                        "node": ac.current_node,
                        "lat": ac.position.lat,
                        "lon": ac.position.lon,
                        "alt_msl_m": ac.position.alt_msl_m,
                        "velocity_mps": ac.velocity_mps,
                        "vertical_speed_mps": ac.vertical_speed_mps,
                        "heading_deg": ac.heading_deg,
                        "battery_soc": ac.battery_soc,
                    }
                )
    if checker is not None:
        checker.check_events(world.events, world)
    kinds: dict[str, int] = {}
    for e in world.events:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    return SimulationResult(
        scenario=scenario,
        seed=seed,
        telemetry=telemetry,
        events=world.events,
        truth=truth,
        final_world=world,
        steps=steps,
        stats={"event_counts": dict(sorted(kinds.items())), "telemetry_records": len(telemetry)},
        violations=checker.violations if checker is not None else [],
    )
```

Check the change. Command:

```bash
git diff --stat src/aerolink/sim/runner.py
```

Expect **`1 file changed, 3 insertions(+)`**.

### 11b. Create the new files

#### New file: `backend/src/aerolink/eval/ablations.py`

*What this file does:* The command you run. Runs every configuration on every scenario, writes all results, builds the summary table, and (with `--update-docs`) writes it into ARCHITECTURE §9.3.

Open the file (command):

```bash
code src/aerolink/eval/ablations.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""Full ablations: what each AI component adds (PLAN E5.7, ARCHITECTURE §9.3).

    uv run python -m aerolink.eval.ablations                       # all configs, all scenarios
    uv run python -m aerolink.eval.ablations --update-docs         # also refresh ARCHITECTURE §9.3

Runs B0, B1, A1-A5 and "A5@50%" (A5 with only half of the en-route suggestions accepted)
across every scenario and seed, in the closed-loop simulator. Needs the trained
``aerolink-gbm`` (E5.3) and the synthetic demand history (E5.4).

Results: ``backend/eval-results/m5-ablations.json`` (every run) and
``backend/eval-results/m5-ablations.md`` (the summary table, also written into
ARCHITECTURE §9.3 with ``--update-docs``).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np

from aerolink.demand.forecaster import ChronosCounts, DemandModel, DestinationModel, tod_mean
from aerolink.demand.synthetic import DEFAULT_DIR as DEMAND_DIR
from aerolink.demand.synthetic import count_matrix, load
from aerolink.eval.harness import _count_events, make_advisor
from aerolink.forecast.cv import CVForecaster
from aerolink.forecast.gbm import GBMForecaster
from aerolink.network import Network
from aerolink.routing.aerolink import ABLATIONS, AeroLinkAdvisor, AeroLinkRouteAdvisorWrapper
from aerolink.sim import load_scenario, run_simulation

BACKEND = Path(__file__).resolve().parents[3]
DOCS = BACKEND.parent / "docs" / "ARCHITECTURE.md"
CONFIGS = ["B0", "B1", "A1", "A2", "A3", "A4", "A5", "A5@50%"]
DESCRIPTIONS = {
    "B0": "shortest path, no traffic information",
    "B1": "**classical navigation** (§6), no AI",
    "A1": "B1 + AI ETA windows (`cv` ETAs in simulation, see PLAN E4.2)",
    "A2": "B1 + AI ETA windows (`aerolink-gbm`)",
    "A3": "A2 + demand forecaster",
    "A4": "A3 + AI off-route detection",
    "A5": "**full AeroLink** (A4 + risk ranking + en-route suggestions)",
    "A5@50%": "A5, half of the suggestions accepted (sensitivity)",
}
START, END = "<!-- ablation-table:start -->", "<!-- ablation-table:end -->"


def build_demand(network: Network, scenario_start: str, use_chronos: bool) -> DemandModel:
    counts, trips = load(DEMAND_DIR)
    histories = {}
    for kind in ["passenger", "medical"]:
        _, sites, m = count_matrix(counts, kind)
        histories[kind] = (sites, m)
    t = trips.to_pydict()
    dest = DestinationModel(histories["passenger"][0]).fit(
        t["origin"], np.array(t["t_of_day_s"]) / 3600.0, np.array(t["weekday"], dtype=float), t["destination"]
    )
    forecaster = tod_mean
    if use_chronos:
        try:
            chronos = ChronosCounts()
            chronos._load()
            forecaster = chronos
        except Exception as e:  # noqa: BLE001
            print(f"Chronos-2 unavailable ({e.__class__.__name__}); demand uses the time-of-day mean")
    weekday = datetime.fromisoformat(scenario_start).weekday()
    return DemandModel.from_history(histories, dest, weekday, forecaster)


def advisor_for(config: str, network: Network, gbm: GBMForecaster, demand: DemandModel):
    if config in ("B0", "B1"):
        return make_advisor(config, network)
    name = config.split("@")[0]
    forecaster = CVForecaster(network) if name == "A1" else gbm
    return AeroLinkRouteAdvisorWrapper(AeroLinkAdvisor(network, forecaster, ABLATIONS[name], demand=demand))


def run_one(network: Network, scenario_file: Path, config: str, seed: int, gbm, demand) -> dict:
    scenario = load_scenario(scenario_file)
    if config.endswith("@50%"):
        scenario = replace(scenario, suggestion_acceptance_rate=0.5)
    result = run_simulation(network, scenario, seed=seed, advisor=advisor_for(config, network, gbm, demand))
    ev = result.events
    deps = {e["flight_id"]: e for e in ev if e["kind"] == "departure"}
    trips = [e["t"] - deps[e["flight_id"]]["t"] for e in ev if e["kind"] == "arrival" and e.get("flight_id") in deps]
    medical = [e["response_time_s"] for e in ev if e["kind"] == "arrival" and "response_time_s" in e]
    holding_s = sum(scenario.dt_s for r in result.truth if r["status"] == "holding")
    return {
        "scenario": scenario.name,
        "config": config,
        "seed": seed,
        "trips": len(trips),
        "trip_time_mean_s": float(np.mean(trips)) if trips else None,
        "trip_time_p95_s": float(np.quantile(trips, 0.95)) if trips else None,
        "holding_s_total": holding_s,
        "holding_s_per_trip": holding_s / max(1, len(trips)),
        "no_route": _count_events(ev, "route_unavailable"),
        "suggestions_accepted": sum(1 for e in ev if e["kind"] == "suggestion" and e.get("accepted")),
        "bunching_events": _count_events(ev, "bunching"),
        "loss_of_separation": _count_events(ev, "loss_of_separation"),
        "medical_response_mean_s": float(np.mean(medical)) if medical else None,
    }


def summarize(rows: list[dict]) -> list[dict]:
    out = []
    for c in CONFIGS:
        rs = [r for r in rows if r["config"] == c]
        if not rs:
            continue
        trips = sum(r["trips"] for r in rs)

        def wmean(key: str, rs=rs) -> float | None:
            vals = [(r[key], r["trips"]) for r in rs if r[key] is not None and r["trips"]]
            return sum(v * w for v, w in vals) / sum(w for _, w in vals) if vals else None

        med = [r["medical_response_mean_s"] for r in rs if r["medical_response_mean_s"] is not None]
        out.append(
            {
                "config": c,
                "runs": len(rs),
                "trips": trips,
                "trip_time_mean_s": wmean("trip_time_mean_s"),
                "trip_time_p95_s": wmean("trip_time_p95_s"),
                "holding_s_per_trip": sum(r["holding_s_total"] for r in rs) / max(1, trips),
                "no_route": sum(r["no_route"] for r in rs),
                "suggestions_accepted": sum(r["suggestions_accepted"] for r in rs),
                "bunching_events": sum(r["bunching_events"] for r in rs),
                "loss_of_separation": sum(r["loss_of_separation"] for r in rs),
                "medical_response_mean_s": float(np.mean(med)) if med else None,
            }
        )
    return out


def markdown(summary: list[dict], seeds: list[int] | None, n_scenarios: int) -> str:
    def f(v, d=0):
        return "–" if v is None else f"{v:,.{d}f}"

    seed_text = "each scenario's own seed" if not seeds else f"seeds {', '.join(map(str, seeds))}"
    lines = [
        f"Measured over {n_scenarios} scenarios, {seed_text} (`python -m aerolink.eval.ablations`, "
        "`backend/eval-results/m5-ablations.json`). Times in seconds; trips = completed flights.",
        "",
        "| # | Configuration | Trips | Mean trip | p95 trip | Holding / trip | No route | Suggestions | "
        "Bunching | Medical response |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summary:
        lines.append(
            f"| {s['config']} | {DESCRIPTIONS[s['config']]} | {s['trips']} | {f(s['trip_time_mean_s'])} | "
            f"{f(s['trip_time_p95_s'])} | {f(s['holding_s_per_trip'], 1)} | {s['no_route']} | "
            f"{s['suggestions_accepted']} | {s['bunching_events']} | {f(s['medical_response_mean_s'])} |"
        )
    b1 = next((s for s in summary if s["config"] == "B1"), None)
    a5 = next((s for s in summary if s["config"] == "A5"), None)
    if b1 and a5 and b1["trip_time_mean_s"] and a5["trip_time_mean_s"]:
        d = a5["trip_time_mean_s"] - b1["trip_time_mean_s"]
        h = a5["holding_s_per_trip"] - b1["holding_s_per_trip"]
        lines += [
            "",
            f"**B1 vs A5 (as measured):** mean trip time {d:+,.0f} s ({d / b1['trip_time_mean_s']:+.1%}), "
            f"holding per trip {h:+,.1f} s, bunching events {a5['bunching_events'] - b1['bunching_events']:+d}.",
        ]
    return "\n".join(lines)


def update_docs(table: str, path: Path = DOCS) -> bool:
    text = path.read_text()
    if START not in text or END not in text:
        return False
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    path.write_text(f"{head}{START}\n{table}\n{END}{tail}")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--network", type=Path, default=BACKEND.parent / "airspace" / "central-fl-v1.geojson")
    ap.add_argument("--scenarios", type=Path, default=BACKEND / "scenarios")
    ap.add_argument("--configs", nargs="+", default=CONFIGS)
    ap.add_argument("--seeds", type=int, nargs="*", default=[], help="default: each scenario's own seed")
    ap.add_argument("--no-chronos", action="store_true", help="demand counts from the time-of-day mean")
    ap.add_argument("--out", type=Path, default=BACKEND / "eval-results" / "m5-ablations.json")
    ap.add_argument("--update-docs", action="store_true")
    a = ap.parse_args(argv)

    network = Network.from_geojson(a.network)
    gbm = GBMForecaster.load(network)
    files = sorted(a.scenarios.glob("*.yaml"))
    demand = build_demand(network, load_scenario(files[0]).start_time, not a.no_chronos)
    rows = []
    for f in files:
        seeds = a.seeds or [load_scenario(f).seed]
        for seed in seeds:
            for c in a.configs:
                r = run_one(network, f, c, seed, gbm, demand)
                rows.append(r)
                print(
                    f"{r['scenario']:<22}{c:<8} seed {seed:<4} trips {r['trips']:<4} "
                    f"mean {r['trip_time_mean_s'] or 0:>7.0f}s holding/trip {r['holding_s_per_trip']:>6.1f}s",
                    flush=True,
                )
    summary = summarize(rows)
    table = markdown(summary, a.seeds, len(files))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({"summary": summary, "runs": rows}, indent=2) + "\n")
    a.out.with_suffix(".md").write_text(table + "\n")
    print("\n" + table)
    print(f"\nwrote {a.out} and {a.out.with_suffix('.md')}")
    if a.update_docs:
        print("updated ARCHITECTURE §9.3" if update_docs(table) else f"markers {START} / {END} not found in {DOCS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

#### New file: `backend/tests/test_e57_ablations.py`

*What this file does:* Checks that each config switches the right features, that the engine is bound to the simulated world, and that the table is written between the markers in the docs.

Open the file (command):

```bash
code tests/test_e57_ablations.py
```

File contents (paste into the empty tab, then Ctrl + S):

```python
"""E5.7: the AeroLink engine sees live traffic, configs switch features, the table is written."""

from pathlib import Path

from aerolink.eval.ablations import END, START, markdown, run_one, summarize, update_docs
from aerolink.forecast.cv import CVForecaster
from aerolink.network import Network
from aerolink.routing.aerolink import ABLATIONS, AeroLinkAdvisor, AeroLinkRouteAdvisorWrapper
from aerolink.sim import load_scenario, run_simulation

BACKEND = Path(__file__).resolve().parents[1]


def test_configs_switch_features():
    assert not ABLATIONS["A2"].demand and ABLATIONS["A3"].demand
    assert ABLATIONS["A4"].off_route and not ABLATIONS["A4"].en_route
    assert ABLATIONS["A5"].risk_ranking and ABLATIONS["A5"].en_route


def test_risk_only_counts_in_a5(fl_network: Network):
    a4 = AeroLinkAdvisor(fl_network, CVForecaster(fl_network), ABLATIONS["A4"])
    a5 = AeroLinkAdvisor(fl_network, CVForecaster(fl_network), ABLATIONS["A5"])
    assert a4.cfg.mu_risk_s == 0.0 and a5.cfg.mu_risk_s == 600.0


def test_engine_is_bound_to_the_world(fl_network: Network):
    adv = AeroLinkAdvisor(fl_network, CVForecaster(fl_network), ABLATIONS["A4"])
    result = run_simulation(fl_network, load_scenario(BACKEND / "scenarios" / "nominal_day.yaml"),
                            advisor=AeroLinkRouteAdvisorWrapper(adv))  # fmt: skip
    assert adv.world is result.final_world
    assert sum(e["kind"] == "arrival" for e in result.events) > 0


def test_run_one_and_table(fl_network: Network, tmp_path: Path):
    row = run_one(fl_network, BACKEND / "scenarios" / "nominal_day.yaml", "A1", 42, gbm=None, demand=None)
    assert row["config"] == "A1" and row["trips"] > 0
    table = markdown(summarize([row]), None, 1)
    assert "| A1 |" in table
    doc = tmp_path / "ARCH.md"
    doc.write_text(f"before\n{START}\nstale table\n{END}\nafter\n")
    assert update_docs(table, doc)
    assert "stale table" not in doc.read_text() and "| A1 |" in doc.read_text() and doc.read_text().endswith("after\n")
```

### 11c. Run the checks

Command:

```bash
uv run ruff format
uv run ruff check --fix
uv run pytest -q tests/test_e57_ablations.py
```

Expect **`4 passed`**.

### 11d. Update the docs

The plan and the design doc need to match what you built: all M5 stories ticked, the files listed, the E5.1 pipeline as built, the off-route rule as built, the demand scaling written down, the Chronos fine-tuning answer recorded, and two markers where the ablation table goes. Rather than many fiddly find-and-replace edits, one command does all of it and refuses to change anything it can't find exactly. Paste the whole box at once:

```bash
uv run python - <<'PYEOF'
"""Update PLAN.md and ARCHITECTURE.md for M5. Safe to run twice."""
from pathlib import Path

DOCS = Path("../docs")


def replace(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    if new in text:
        print(f"  already done: {new.splitlines()[0][:70]}")
        return
    if text.count(old) != 1:
        raise SystemExit(f"STOP: could not find this text exactly once in {path.name}:\n{old}")
    path.write_text(text.replace(old, new))
    print(f"  updated: {new.splitlines()[0][:70]}")


plan = DOCS / "PLAN.md"
text = plan.read_text()
start, end = text.index("## M5: Real Data and Trained Models"), text.index("## M6: Live System and Web Map")
m5 = text[start:end]
m5 = m5.replace(
    "- [ ] Split tracks at the new-leg flag and at gaps over 30 s; resample to 1 Hz with the `traffic` library",
    "- [ ] Split tracks at the new-leg flag and at gaps over 30 s; resample to 1 Hz",
).replace(
    "- [ ] Write Parquet locally (never committed)",
    "- [ ] Write the cleaned segments as Parquet to `backend/data/adsblol/` (committed under ODbL; raw data never stored)",
).replace(
    "- [ ] Flag aircraft outside their calibrated band for 3 or more consecutive samples",
    "- [ ] Flag aircraft outside their calibrated band for 3 or more consecutive checks (p01–p99, one check every 10 s), "
    "outside a learned route tolerance, or unable to arrive inside their ETA window",
)
m5 = m5.replace("- [ ] ", "- [x] ")
for epic, files in {
    "### E5.2 Real-Data Benchmark": "`realdata/samples.py`, `realdata/benchmark.py`, `forecast/chronos.py` (Chronos-2 API), "
    "`eval-results/realdata-e5.2.json`",
    "### E5.3 Trained Forecaster (`aerolink-gbm`)": "`forecast/features.py`, `forecast/simdata.py`, `forecast/gbm.py`, "
    "`forecast/train_gbm.py`, `models/aerolink-gbm/`, `eval-results/aerolink-gbm-e5.3.json`",
    "### E5.4 Demand Forecaster": "`demand/synthetic.py`, `demand/forecaster.py`, `demand/evaluate.py`, "
    "`data/synthetic-demand/` (synthetic), `eval-results/demand-e5.4.json`",
    "### E5.5 Off-Route Detection": "`offroute/detector.py`, `offroute/evaluate.py`, `models/offroute/`, "
    "`routing/aerolink.py` (`recalculate`, reachable mass), `eval-results/offroute-e5.5.json`",
    "### E5.6 Route Summary (LLM)": "`summary/llm.py`, `summary/evaluate.py`, `eval-results/summary-e5.6.json`",
    "### E5.7 Full Ablations": "`routing/aerolink.py`, `eval/ablations.py`, `eval-results/m5-ablations.json` and `.md`",
}.items():
    block_start = m5.index(epic)
    accept = m5.index("**Acceptance criteria**", block_start)
    line = f"**Files** (under `backend/`): {files}\n\n"
    if line not in m5:
        m5 = m5[:accept] + line + m5[accept:]
plan.write_text(text[:start] + m5 + text[end:])
print("  updated: PLAN.md M5 stories ticked, files listed")
replace(
    plan,
    "- [ ] Check whether the installed library version supports fine-tuning",
    "- [x] Check whether the installed library version supports fine-tuning (yes: `Chronos2Pipeline.fit`, full or LoRA, "
    "in chronos-forecasting 2.3.2)",
)

arch = DOCS / "ARCHITECTURE.md"
replace(
    arch,
    "6. **Resample** to 1 Hz with the [`traffic`](https://github.com/xoolive/traffic) library (MIT), in a data-prep step only.",
    "6. **Resample** to 1 Hz by linear interpolation (NumPy; track angles unwrapped), dropping segments shorter than 60 s.",
)
replace(
    arch,
    "7. **Write** Parquet to `backend/data/realdata/` (gitignored, never committed).",
    "7. **Write** one Parquet file of clean segments per day to `backend/data/adsblol/`, plus a per-day report of what "
    "each filter removed. The raw day is streamed and never written to disk (`python -m aerolink.realdata.fetch`).",
)
replace(
    arch,
    "- **Raw data:** not redistributed. The pipeline script reproduces it from the source.",
    "- **Raw data:** not redistributed. The pipeline script reproduces it from the source.\n"
    "- **Cleaned data:** the filtered segments in `backend/data/adsblol/` are a derived database, committed under ODbL "
    "with attribution (`backend/data/adsblol/README.md`).",
)
replace(
    arch,
    "- **Band check:** the aircraft is outside its calibrated forecast interval (position head p05–p95, or its ETA window) "
    "for ≥3 consecutive samples.",
    "- **Band check:** the aircraft is outside its calibrated forecast interval for ≥3 consecutive checks, one check "
    "every 10 s (1 Hz samples are not independent): the position head's p01–p99 box from 30 s earlier, a learned route "
    "tolerance (LightGBM p99 of how far clean flights stray, given speed and the turn ahead) instead of a fixed 300 m, "
    "or its ETA window (it can no longer arrive inside its p99 arrival time). An aircraft is flagged when the route "
    "tolerance or ETA window check fires, or when the position band and the anomaly score agree.",
)
replace(
    arch,
    "uv run python -m aerolink.eval --scenario all --seed 42 --ablations",
    "uv run python -m aerolink.eval.ablations --update-docs",
)
replace(
    arch,
    "| A5 | **full AeroLink** (A4 + risk ranking + en-route suggestions) |\n",
    "| A5 | **full AeroLink** (A4 + risk ranking + en-route suggestions) |\n\n"
    "<!-- ablation-table:start -->\n_Results appear here after `uv run python -m aerolink.eval.ablations --update-docs`._\n"
    "<!-- ablation-table:end -->\n",
)
replace(
    arch,
    "and it's labelled synthetic everywhere.",
    "and it's labelled synthetic everywhere. Scale: FDOT's low 2027 figure (220,000 trips) × a 27% Central Florida "
    "share (assumed, from population) ≈ 163 passenger departures per day (`aerolink.demand.synthetic`).",
)
print("Docs updated.")
PYEOF
```

> **Revised:** E5.1 already made three of these doc edits, so those lines will say `already done:`. That's expected and was checked against the current docs, and every other edit still finds its text.

You should see a list of `updated: ...` lines ending in **`Docs updated.`** If you see `STOP: could not find this text...`, someone has edited that part of the docs since this guide was written: don't edit by hand, send the message to Joshua. (Running it twice is safe: the second time says `already done`.)

### 11e. Run the ablations

Command:

```bash
uv run python -m aerolink.eval.ablations --update-docs
```

*What it does:* 7 scenarios × 8 configurations = 56 simulated hours, plus a quick Chronos-2 demand forecast at the start. Roughly **5–15 minutes**. One line per run, then the summary table, which is also written to `eval-results/m5-ablations.md` and into `docs/ARCHITECTURE.md` §9.3 (you'll see `updated ARCHITECTURE §9.3`).

How to read the table:
- **Trips:** completed flights. **Mean / p95 trip:** average and worst-5% flight time in seconds (lower is better).
- **Holding / trip:** seconds spent circling near a busy landing site, per trip (lower is better).
- **No route:** requests where no route fitted the battery. **Suggestions:** en-route suggestions accepted (only A5 makes them).
- **Bunching:** times aircraft got closer than the spacing values. **Medical response:** mean time for medical dispatches.
- The last line compares **B1 vs A5**: that's the headline claim of the project, *as measured*.

**Important:** if several rows come out identical, that is a real result, not a mistake. At the simulator's current demand levels, most trips have only one sensible route and pads are rarely busy, so better predictions may not change any decision; M4's committed results already show B0, B1 and A1 identical for the same reason. **Don't change code or scenarios to make AeroLink win.** Write down what you see for Joshua; heavier-traffic scenarios are a follow-up decision for the team.

### 11f. Commit

Command:

```bash
git add src/aerolink/sim/runner.py src/aerolink/eval/ablations.py tests/test_e57_ablations.py eval-results/m5-ablations.json eval-results/m5-ablations.md ../docs/PLAN.md ../docs/ARCHITECTURE.md
git commit -F - <<'EOF'
feat: E5.7 full ablations (B0, B1, A1-A5, A5 at 50% acceptance)

* feat: route engine bound to the simulated world (sees planned flights)
* feat: ablation CLI, results JSON and table; ARCHITECTURE §9.3 populated
* docs: PLAN M5 stories done; ARCHITECTURE updated for E5.1, E5.4, E5.5
EOF
git push
```

**E5.7 done.**

---

## Part 12: Final checks and the pull request

### 12a. Run everything GitHub will run

Command:

```bash
uv run ruff check
uv run ruff format --check
uv run ty check
uv run pytest -q
```

*What it does:* the complete set of checks, including all ~680 tests (5 to 10 minutes). Expect **`All checks passed!`**, **`... files already formatted`**, **`All checks passed!`**, and a final line like **`682 passed, 2 skipped`**.

If `ruff format --check` complains, run `uv run ruff format`, then commit the result (`git add -u && git commit -m "style: ruff format" && git push`).

### 12b. Check nothing was left out

Command:

```bash
cd ..
git status
```

It should say **`nothing to commit, working tree clean`**. If a file shows as modified or untracked, check it's one of yours and add it to a commit (ask Joshua if unsure). Then go back: `cd backend`.

> **Revised:** the files under `docs/Carla/plans/` may show as untracked (`SPEC.md`, `SIGNOFF.md`), along with your edits to this guide. Commit them or not as agreed with Joshua. The `PLAN.md` files never show up, because they're ignored locally on purpose. Don't force-add them.

### 12c. Make sure the results are all there

Command:

```bash
ls eval-results models/*
```

You should see `realdata-e5.1.json`, `realdata-e5.2.json`, `aerolink-gbm-e5.3.json`, `demand-e5.4.json`, `offroute-e5.5.json`, `summary-e5.6.json`, `m5-ablations.json`, `m5-ablations.md`, plus `models/aerolink-gbm/model.joblib`, `models/aerolink-gbm/manifest.json` and `models/offroute/offroute.joblib`.

### 12d. Open the pull request

Go to the AeroLink repository on github.com. A yellow banner says **e5.1-real-data had recent pushes** (or the name agreed in decision D3): click **Compare & pull request**. Title: **M5: Real data and trained models**. In the description, paste the B1-vs-A5 line from `eval-results/m5-ablations.md` and anything you wrote down for Joshua (surprising numbers, warnings). Also include the limits from `docs/Carla/plans/SIGNOFF.md` (D15–D16) and anything that differs from this guide (for example, a Chronos fallback or a sample cap). Click **Create pull request**. GitHub runs all the checks automatically; a green tick means they passed.

**That's all of M5.**

### Reproducing everything later (for reviewers)

Every result comes from one command, run from `backend/`, in this order:

| Epic | Command | Output |
|---|---|---|
| E5.1 | `uv run python -m aerolink.realdata.fetch --days 14` | `data/adsblol/`, `eval-results/realdata-e5.1.json` |
| E5.3 | `uv run python -m aerolink.forecast.train_gbm --with-chronos` | `models/aerolink-gbm/`, `eval-results/aerolink-gbm-e5.3.json` |
| E5.2 | `uv run python -m aerolink.realdata.benchmark --forecasters cv chronos aerolink-gbm` | `eval-results/realdata-e5.2.json` |
| E5.4 | `uv run python -m aerolink.demand.synthetic` then `uv run python -m aerolink.demand.evaluate` | `data/synthetic-demand/`, `eval-results/demand-e5.4.json` |
| E5.5 | `uv run python -m aerolink.offroute.evaluate` | `models/offroute/`, `eval-results/offroute-e5.5.json` |
| E5.6 | `uv run python -m aerolink.summary.evaluate` (needs `LLM_MODEL`, `LLM_API_KEY`) | `eval-results/summary-e5.6.json` |
| E5.7 | `uv run python -m aerolink.eval.ablations --update-docs` | `eval-results/m5-ablations.*`, ARCHITECTURE §9.3 |

All seeds are fixed in the commands' defaults, so the same data gives the same results. (Real-data days published later will differ, which is why the cleaned days are committed.)

---

## Troubleshooting

| What you see | What to do |
| --- | --- |
| The Codespace stopped, or you closed the tab | Go to [github.com/codespaces](https://github.com/codespaces) and click your AeroLink Codespace to reopen it. Open a terminal, run `cd backend`, then repeat your last command. Your files and branch are still there. |
| `uv: command not found` after reopening | Run `pip install uv` again, then retry. |
| `code: command not found` | Use the Explorer instead: right-click the folder (for example `backend/src/aerolink/demand`) → **New File...**, type the file name, press Enter, then paste. |
| `ModuleNotFoundError: No module named 'aerolink.something'` | A file from an earlier part is missing or saved in the wrong folder. Check the path in the error against this guide. |
| `IndentationError` or `SyntaxError` naming a file | Part of that file was lost when pasting. Open it, Ctrl + A, Delete, and paste its box from this guide again. |
| `FileNotFoundError: .../models/aerolink-gbm/model.joblib` | Part 7 (training) hasn't been run in this Codespace, or wasn't committed. Run 7d. |
| `fewer than 2 real days` | The E5.1 download hasn't produced at least 2 days yet. Finish 5f (or use `--data /tmp/fake-adsblol` just to try a command). |
| `no release published (yet)` | That day isn't online yet, so it's skipped. Fine for one or two days. |
| `HTTP Error 403` or `rate limit` (download) | GitHub is briefly limiting downloads. Wait 15 minutes and run the same command again; finished days are skipped. |
| Chronos-2 download or `OSError` from Hugging Face | Run the command again. If it keeps failing, use `--no-chronos` / leave `chronos` out, commit, and tell Joshua. |
| `Killed` or `MemoryError` | The machine is too small. Push your work first (`git push`), then create a new Codespace with **8-core**, check out your branch (`git checkout e5.1-real-data`), and continue. |
| `LLM_MODEL and LLM_API_KEY are not set` | Run the two `export` lines in 10c in this terminal (a new terminal forgets them). |
| `STOP: could not find this text...` (docs step) | The docs changed since this guide was written. Don't edit by hand; send the message to Joshua. |
| A test fails, or `git push` is refused | Copy the last 20 lines from the terminal and send them to Joshua. |

**When you're done for the day:** go to [github.com/codespaces](https://github.com/codespaces), click **...** next to your Codespace, and choose **Stop codespace**. It keeps everything and stops using your free monthly hours. Delete it only after your pull request is merged.

## Words used in this guide

| Word | Meaning |
| --- | --- |
| ADS-B | The signal most aircraft broadcast with their own GPS position, about twice a second |
| MLAT | A position estimated by timing one signal at several ground receivers; less accurate, so the pipeline drops it |
| adsb.lol | A volunteer network that collects ADS-B and publishes every day of it for free |
| ODbL | The data's licence: free to use and share, as long as you credit adsb.lol and share what you build from it under the same licence |
| Parquet | A compact file format for big tables |
| Segment | One continuous piece of a flight, with no gap over 30 seconds |
| Horizon | How far ahead a prediction looks (30 s, 5 min, ...) |
| p10 / p50 / p90 | A low guess, a middle guess and a high guess: 10%, 50% and 90% of outcomes should fall below them, so 80% fall between p10 and p90 |
| Coverage | How often the truth actually fell inside the predicted p10–p90 range (80% is the target) |
| Calibration | Adjusting predicted ranges so their coverage is honest |
| `cv` | Constant velocity: "it keeps going the way it's going". The simple, non-AI baseline |
| Chronos-2 | A pretrained time-series model from Amazon, used without training ("zero-shot") |
| LightGBM | A fast machine-learning library that builds many small decision trees |
| MAPIE | A library that calibrates prediction ranges (conformal prediction) |
| IsolationForest | A model that scores how unusual a data point is compared to normal examples |
| Seed | A number that fixes the "random" choices, so a run can be repeated exactly |
| Held-out data | Data kept away from training and used only for testing |
| Ablation | Switching pieces off one at a time to measure what each one adds |
| Synthetic | Made up by a program, not observed. Always labelled as such |
| LLM | Large language model: an AI that writes text |
| API key | A secret password that lets a program use a paid online service |
| Branch / Commit / Push / Pull request | Your own copy of the project / a saved snapshot / sending it to GitHub / asking for it to be reviewed and merged |

## Appendix: every file M5 adds or changes

| File (under `backend/` unless shown) | Epic | New / changed |
|---|---|---|
| `src/aerolink/realdata/__init__.py`, `config.py`, `parse.py`, `segments.py`, `source.py`, `fetch.py`, `replay.py` | E5.1 | new |
| `tests/test_realdata.py`, `data/adsblol/` | E5.1 | new |
| `src/aerolink/sim/world_config.py` | E5.1 | changed |
| `src/aerolink/sim/runner.py` | E5.1, E5.7 | changed |
| `src/aerolink/forecast/chronos.py` | E5.2 | changed (Chronos-2 API) |
| `src/aerolink/forecast/features.py`, `src/aerolink/realdata/samples.py`, `benchmark.py`, `synthetic_days.py`, `tests/test_e52_benchmark.py` | E5.2 | new |
| `src/aerolink/forecast/simdata.py`, `gbm.py`, `train_gbm.py`, `tests/test_e53_gbm.py`, `models/aerolink-gbm/` | E5.3 | new |
| `src/aerolink/demand/` (4 files), `tests/test_e54_demand.py`, `data/synthetic-demand/` | E5.4 | new |
| `src/aerolink/offroute/` (3 files), `src/aerolink/routing/aerolink.py`, `tests/test_e55_offroute.py`, `models/offroute/` | E5.5 | new |
| `src/aerolink/summary/` (3 files), `tests/test_e56_summary.py` | E5.6 | new |
| `src/aerolink/eval/ablations.py`, `tests/test_e57_ablations.py` | E5.7 | new |
| `eval-results/*.json`, `eval-results/m5-ablations.md` | all | new results |
| `pyproject.toml`, `uv.lock` | E5.1 | changed (pyarrow, mapie) |
| `docs/PLAN.md`, `docs/ARCHITECTURE.md` (repo root) | E5.7 | changed |
