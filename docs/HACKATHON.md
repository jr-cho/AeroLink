# AeroLink: Hackathon Submission

How AeroLink meets the theme and the rules. Overview: [README](../README.md). Technical details: [ARCHITECTURE](ARCHITECTURE.md).

---

## Theme

> **Cleared for Takeoff: AI for Florida's Skies.** Build an AI-powered solution, with a working interface, that makes Advanced Air Mobility in Florida safer, more efficient, or more accessible.

### What AeroLink is

> AeroLink is a **navigation app for Florida's low-altitude air corridors**, like Google Maps for eVTOL **passenger** and **medical** aircraft. It shows route options with **AI-predicted traffic and arrival times**, and suggests faster routes in flight. It runs on corridors modeled on FDOT's Aerial Highway Network, connecting Tampa, Lakeland, Orlando, and Sebring. **AeroLink advises; it never controls. Commercial and military airspace are no-go constraints.**

### Florida fit

| Element | AeroLink |
|---|---|
| FDOT's Aerial Highway Network | corridors follow the interstates and highways, with shapes from FDOT's own highway GIS |
| FDOT AAM Business Plan (Nov 2025) | covers FDOT's first routes (Orlando–Tampa, Orlando–SunTrax, Tampa–SunTrax); corridors sit in FDOT's 1,000–3,000 ft eVTOL band; synthetic demand is scaled from FDOT's 2027 passenger-trip forecast |
| FDOT Phase 1A (Central Florida I-4 corridor) | eVTOL sites at Water Street (downtown Tampa), Parramore (downtown Orlando), Orlando Executive, and Sebring Regional, plus SunTrax and Lakeland Linder |
| Sebring | Sebring Regional is planning a vertiport; Highlands County is the rural end of the network |
| FDOT's federal pilot program scope (cargo, passenger, medical response) | passenger and medical, each with its own landing sites (eVTOL sites, hospital helipads); aerial cargo and drone delivery are out of scope |
| Florida aviation data | 48 medical helipads from the state's FDEP/FDOT aviation layer, 22 connected to the network |
| Real Florida constraints | FAA airspace data (TPA and MCO Class B, MacDill AFB, Avon Park Air Force Range, and others), applied with the FDOT plan's airspace zones: Class D only with ATC coordination and avoided when possible; Class B only on designated clearance routes; Class C and special use no-go |
| Real Florida flights | trajectory model validated on helicopter and light-aircraft tracks over Central Florida (adsb.lol, ODbL) |

### Theme pillars (all three)

| Pillar | How | Measured as |
|---|---|---|
| **Safer** | traffic-aware routing spreads aircraft out at junctions and pads; learned off-route detection | bunching events, off-route detection latency |
| **More efficient** | AI-predicted traffic and arrival times; en-route "faster route" suggestions | trip time, waiting at landing pads, time saved by suggestions |
| **More accessible** | medical flights are calculated first and reach rural Sebring faster | medical wait caused by other traffic, medical time-to-destination |

All claims are simulation results measured against classical navigation. AeroLink is not certified and not for real flight.

---

## Requirement 1: AI is a real part of the solution

### What the AI does

| AI component | Rules category | Function |
|---|---|---|
| Trajectory forecaster: Chronos-2 (pretrained, Hugging Face) + `aerolink-gbm` (trained on simulated and real Central Florida tracks) | prediction | predicts positions (≤5 min) and arrival times at each waypoint (≤45 min), with calibrated uncertainty |
| Demand forecaster | prediction | predicts passenger departures and medical dispatches before they're planned |
| Traffic forecast | prediction | turns those predictions into low / moderate / heavy traffic per corridor, up to 45 min ahead |
| Off-route detection | prediction (anomaly detection) | flags aircraft leaving their own predicted range → "recalculating" |
| Risk-weighted ranking and capacity model | optimization | ranks route options by predicted time, wait, and bunching risk; suggests faster routes |
| Route summary | LLM summarization | explains the options; every number verified |

### Why it is essential

Without prediction, a navigation app can only show distance and a fixed travel time. The classical method assumes:
1. Aircraft fly their planned timing exactly.
2. All traffic is known in advance.

Neither holds. Speeds vary, aircraft wait for pads, and medical flights are unscheduled. AI fills exactly those gaps:

| Classical assumption | Reality | AI |
|---|---|---|
| Fixed ±60 s arrival windows | timing uncertainty varies by aircraft, vehicle type, and distance | ETA ranges from conformal prediction, with a stated and measured coverage |
| Only planned flights count | medical dispatches and upcoming passenger flights aren't planned yet | demand forecaster adds them to the traffic forecast |
| Fixed 300 m off-route bound | the right tolerance depends on speed and manoeuvre | learned off-route detection |
| Traffic is yes/no | arrivals overlap with some probability | risk-weighted ranking and suggestions |

Take the AI out and the "traffic" layer, the ETA ranges, and the "faster route" suggestions all disappear.

### How we prove it

The ablation table ([ARCHITECTURE §9.3](ARCHITECTURE.md#93-configurations-ablations)) runs **classical navigation (B1)** against **full AeroLink (A5)**, plus each AI component on its own, across 7 scenarios. The gap between B1 and A5 is what AI adds, reported as measured. The forecaster is also validated on **real Central Florida flight tracks**.

The LLM summary isn't what the AI case rests on. Without it, the AI still produces every prediction and ranking.

---

## Requirement 2: An interface someone can use

| Interface | User | What they do |
|---|---|---|
| **AeroLink device** (Rev 0 breadboard, wired to a KiCad schematic) | pilot | pans and zooms a real map, taps "Where to?", compares route options with predicted times and traffic, taps Go, gets "faster route" and "recalculating" in flight |
| **Web map** | pilots, operators, judges | the same map in a browser: directions for any vehicle type, traffic layer with a +45 min slider, live aircraft, classical-vs-AeroLink toggle, medical view, AI impact numbers |

---

## Demo script (Lakeland, 6 minutes)

The device sits by a window in Lakeland with a live GNSS fix. The web map is on the projector.

1. **The map (1 min).** On the device, pan from Lakeland toward Tampa and zoom in: corridors along I-4 and US-27, coloured by traffic. On the projector, the same map with eVTOL sites, hospital helipads, and no-go airspace (MacDill, Avon Park). The device appears live at Lakeland.
2. **One device per aircraft (0.5 min).** The device shows its vehicle type. Press the demo button: Passenger ↔ Medical, and "Where to?" switches between eVTOL sites and hospital helipads. Explain that a real unit's type is burned into the chip.
3. **Classical navigation (1 min).** `i4_peak` scenario with the method set to `classical`. Fixed arrival times; a queue builds at Orlando Executive because arrivals drift late. Point out the aircraft circling.
4. **Switch to AeroLink (1 min).** Toggle the method. ETA ranges appear per aircraft, the traffic layer shows the queue 20 minutes before it happens, and route options spread aircraft out. Show the waiting-time numbers.
5. **Medical flight from the device (1.5 min).** As Medical, "Where to?" → Tampa General. Two routes appear on the device map; the fastest avoids predicted heavy traffic at Lakeland, and the summary says why. Tap Go. On the projector, two passenger aircraft get "Faster route available" because their predictions changed.
6. **Proof (1 min).** Inject a rogue aircraft: it's flagged off route and its device shows "recalculating". Then the AI impact panel: B1 vs A5 on trip time, pad waiting, bunching, and medical delay, plus the real-data forecast error against constant velocity. Close on the device's KiCad wiring schematic.

---

## Judge access

| Item | Value |
|---|---|
| Web map URL | *TBD at deployment* |
| Credentials | shared privately with the judges |
| Repository | *TBD* |
| Eval results | `backend/eval-results/` |
| Run locally | [README Quickstart](../README.md#quickstart) |

---

## Anticipated questions

**Is this air traffic control?**
No. AeroLink is a navigation app. It predicts traffic and suggests routes; the pilot decides. It never approves, rejects, or holds a flight. The backend uses traffic-management calculations only to make its predictions.

**Is this about airliners?**
No. AeroLink routes only low-altitude AAM flights. Commercial and military airspace are constraints it routes around.

**Is the LLM the AI?**
No. The core AI is prediction (arrival times, demand, traffic, off-route). The LLM only explains the result.

**How is this different from just showing the shortest route?**
That's baseline B0. Classical navigation (B1) adds fixed travel times and known traffic. AeroLink adds predicted traffic and timing uncertainty. The ablation measures each step.

**Do corridors fly through controlled airspace?**
Only the way FDOT's plan allows. Corridors avoid controlled airspace when possible. Class D airports (FDOT's "yellow zone") are crossed only when there's no clear altitude, and those legs are flagged as needing a call to the tower. Class B, which FDOT says needs "special procedures", is crossed only on six designated clearance routes into downtown Tampa and Orlando, like today's helicopter routes. Class C and restricted or military airspace are never entered.

**Can AI produce an unsafe route?**
No. Routes are paths in a graph built so that no corridor touches no-go airspace. AI only changes predicted times and the ranking of valid options.

**Why can't a pilot change the device's aircraft type?**
A navigation unit belongs to an aircraft. An air taxi shouldn't be offered a hospital helipad. The type is burned into the chip's eFuse and checked by the backend; the demo button exists only on the demo board.

**Does the device run the website?**
No. It's a native touchscreen app that draws its own map from tiles on an SD card, using the same basemap as the website. That's why it pans smoothly and keeps working if Wi-Fi drops; only traffic and routes need the backend.

**Why simulated traffic?**
Commercial eVTOL operations haven't started in Florida yet. Simulation provides ground truth and closed-loop testing. Real helicopter and light-aircraft tracks over Central Florida validate the forecaster.

**Where did the landing sites come from?**
eVTOL sites come from FDOT's Phase 1A plan. Helipads come from Florida's aviation facility data (FDEP/FDOT, with FAA identifiers), filtered to active hospital and EMS pads. Each position is checked on aerial imagery.

**Why no cargo?**
AeroLink covers passenger and medical eVTOL flights only. Aerial cargo of any kind, including drone delivery, is out of scope. There's also no reliable public data on cargo takeoff/landing sites in the region, and we didn't want to invent them.

**Is there a custom PCB?**
Not yet. The device is a breadboard prototype wired to a KiCad schematic, with datasheets for every part. A custom PCB built from that schematic is planned after the hackathon.

**Did you build everything from scratch?**
No. We reused existing tools wherever they fit: Chronos-2 (pretrained, Hugging Face), LightGBM and MAPIE, OR-Tools, FAA airspace open data, FDOT highway GIS, adsb.lol flight data, the `traffic` library, Mosquitto, MapLibre, Protomaps, TileServer GL, the `map_tiles` LVGL component, and Espressif's display, touch, GNSS, and MQTT components. Custom code covers what doesn't exist openly: the route engine, the corridor network, the simulator, and off-route detection.

**Is this a safety system?**
No. Its safety metrics are simulation results.

---

## Sources

- [Florida Trend: FDOT Aerial Highway Network and Phase 1A](https://www.floridatrend.com/feature/2026/11/11/vertical-integration)
- [Florida HB 1093 summary](https://www.hklaw.com/en/insights/publications/2026/04/new-legislation-clears-the-runway-for-vertiport-development-in-florida)
- [Sebring Airport vertiport planning](https://sebring-airport.com/?p=4963)
- [FAA UAM ConOps 2.0 summary](https://www.halldale.com/civil-aviation/21140-cat-faa-considering-air-taxi-corridors-passing-lanes)
- [ASTM F3548-21](https://store.astm.org/f3548-21.html)
- [FDOT AAM Business Plan summary](https://www.urbanairmobilitynews.com/new-city-projects/florida-dot-publishes-its-aam-network-development-plan/)
- [FAA EB 105A Vertiport Design](https://www.faa.gov/airports/engineering/engineering_briefs/eb_105a_vertiports)

---

## Submission checklist

Confirm against the official rules.

- [ ] Repository public, README current, data attribution present
- [ ] Web map deployed, judge credentials sent
- [ ] Ablation table and real-data results filled from committed runs
- [ ] Demo rehearsed end to end at the venue (GNSS fix, Wi-Fi/hotspot, SD card)
- [ ] Backup demo video recorded
- [ ] Wiring schematic PDF exported; datasheets in the repo
