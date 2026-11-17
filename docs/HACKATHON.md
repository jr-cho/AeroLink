# AeroLink: Hackathon Submission

How AeroLink meets the theme and the rules. Overview: [README](../README.md). Technical details: [ARCHITECTURE](ARCHITECTURE.md).

---

## Theme

> **Cleared for Takeoff: AI for Florida's Skies.** Build an AI-powered solution, with a working interface, that makes Advanced Air Mobility in Florida safer, more efficient, or more accessible.

### What AeroLink is

> AeroLink is an AI-assisted planning service for **low-altitude AAM flights**: eVTOL **passenger**, **cargo**, and **medical** missions. It runs on corridors modeled on FDOT's Aerial Highway Network, connecting Tampa, Lakeland, Orlando, and Sebring. **Commercial and military airspace are no-go constraints; AeroLink does not manage airliners or military aircraft.**

### Florida fit

| Element | AeroLink |
|---|---|
| FDOT's Aerial Highway Network | corridors follow the interstates and highways, as FDOT plans |
| FDOT Phase 1A (Central Florida I-4 corridor) | vertiports at Tampa Executive, Orlando Executive, SunTrax, and Sebring Regional, all on the Phase 1A list |
| Sebring | Sebring Regional is planning a vertiport; Highlands County is the rural end of the network |
| FDOT's federal pilot program scope (cargo, passenger, medical response) | the same three missions |
| Real Florida constraints | FAA airspace data: TPA and MCO controlled airspace, MacDill AFB, Avon Park Air Force Range, and others, all no-go |
| Real Florida flights | trajectory model validated on helicopter and light-aircraft tracks over Central Florida (adsb.lol, ODbL) |

### Theme pillars (all three)

| Pillar | How | Measured as |
|---|---|---|
| **Safer** | uncertainty-aware strategic deconfliction; learned conformance monitoring; medical priority | loss-of-spacing events, unresolved conflicts, detection latency |
| **More efficient** | forecast-driven demand-capacity balancing and route ranking; en-route re-planning | total delay, landing-slot utilization, rejected requests |
| **More accessible** | medical flights get priority and reach rural Sebring faster | medical delay caused by other traffic, medical time-to-destination |

All claims are simulation results measured against the classical method. AeroLink is not certified and not for real flight.

---

## Requirement 1: AI is a real part of the solution

### What the AI does

| AI component | Rules category | Function |
|---|---|---|
| Trajectory forecaster: Chronos-2 (pretrained, Hugging Face) + `aerolink-gbm` (trained on simulated and real Central Florida tracks) | prediction | predicts positions (≤5 min) and arrival times at each node (≤45 min), with uncertainty |
| Demand forecaster | prediction | predicts passenger departures, cargo waves, and medical dispatches before they are filed |
| Conformance monitor | prediction (anomaly detection) | flags aircraft leaving their own predicted range |
| Uncertainty-aware deconfliction, DCB, and route ranking | optimization | plans routes and landing slots using the predictions |
| Pilot briefing | LLM summarization | explains the plan; every number verified |

### Why it is essential

The classical FAA/industry approach (filed operational intent, first-come-first-served strategic deconfliction, demand-capacity balancing, fixed conformance bounds) assumes two things:
1. Aircraft fly their filed timing exactly.
2. All demand is filed in advance.

Neither holds in practice. Speeds vary, aircraft hold, and medical flights are unscheduled. AI fills exactly those gaps:

| Classical assumption | Reality | AI |
|---|---|---|
| Fixed ±60 s time windows | timing uncertainty varies by aircraft, mission, and distance | ETA quantile windows |
| Only filed flights count | medical, cargo, and upcoming passenger flights aren't filed yet | demand forecaster reserves capacity |
| Fixed 300 m conformance bound | the right tolerance depends on speed and manoeuvre | learned conformance bounds |
| Conflict is yes/no | windows overlap with some probability | risk-weighted ranking |

### How we prove it

The ablation table ([ARCHITECTURE §9.3](ARCHITECTURE.md#93-configurations-ablations)) runs the **classical method (B1)** against **full AeroLink (A5)**, plus each AI component on its own, across 8 scenarios. The gap between B1 and A5 is what AI adds, reported as measured. The forecaster is also validated on **real Central Florida flight tracks**.

The LLM briefing isn't what the AI case rests on. Without it, the AI still makes every planning decision.

---

## Requirement 2: An interface someone can use

| Interface | User | What they do |
|---|---|---|
| **Web dashboard** (map) | operations staff, judges | live traffic by mission, filed intents, prediction cones, congestion timeline, classical-vs-AeroLink toggle, planner, medical panel, AI impact numbers |
| **AeroLink navigation device** (Rev 0 breadboard, wired to a KiCad schematic) | pilot | picks a mission and destination, sees ranked routes with reasons, files an intent, receives re-route offers |

---

## Demo script (Lakeland, 6 minutes)

The device sits by a window in Lakeland with a live GNSS fix. The dashboard is on the projector.

1. **The network (1 min).** Corridors along I-4 and US-27, vertiports, hospitals, SunTrax. No-go airspace shaded by type, including MacDill and Avon Park. The device appears live at Lakeland.
2. **Classical method (1 min).** `i4_peak` scenario with the method set to `classical`. Fixed time windows; a landing queue builds at Orlando Executive because arrivals drift late. Point out the airborne holding.
3. **Switch to AeroLink (1 min).** Toggle the method. ETA windows widen and narrow per aircraft, departures are spread out ahead of time, and the queue doesn't form. Show the delay numbers.
4. **Medical dispatch from the device (1.5 min).** On the device, choose Medical → Tampa General. The route avoids a predicted landing queue and the device shows the briefing. File the intent; the medical panel shows capacity reserved and two passenger flights re-planned.
5. **Rogue aircraft (0.5 min).** Inject a deviation; the conformance alert fires and the affected intents are re-checked.
6. **Proof (1 min).** AI impact panel: B1 vs A5 on delay, loss-of-spacing events, and medical delay; plus the real-data forecast error against constant velocity. Then show the device's KiCad wiring schematic.

---

## Judge access

| Item | Value |
|---|---|
| Dashboard URL | *TBD at deployment* |
| Credentials | shared privately with the judges |
| Repository | *TBD* |
| Eval results | `brain/eval-results/` |
| Run locally | [README Quickstart](../README.md#quickstart) |

---

## Anticipated questions

**Is this about airliners?**
No. AeroLink plans only low-altitude AAM flights. Commercial and military airspace are constraints it routes around.

**Is the LLM the AI?**
No. The core AI is prediction (arrival times, demand, conformance) driving deterministic planning. The LLM only explains the result.

**Why not just use the classical FAA/industry method?**
It is the baseline. AeroLink uses the same framework (operational intent, deconfliction, DCB, conformance) and adds prediction where the classical method assumes perfect timing and perfect advance filing. The ablation measures the difference.

**Can AI produce an unsafe route?**
No. Routes are paths in a graph built so that no corridor touches no-go airspace, and deconfliction is deterministic. AI only changes timing windows and the ranking of valid options.

**Why simulated traffic?**
Commercial eVTOL operations haven't started in Florida yet. Simulation provides ground truth and closed-loop testing. Real helicopter and light-aircraft tracks over Central Florida validate the forecaster.

**Why only 10 aircraft?**
Prototype scale. Conflicts still occur at landing pads and corridor junctions, which is where AAM capacity is tightest.

**Is there a custom PCB?**
Not yet. The device is a breadboard prototype wired to a KiCad schematic, with datasheets for every part. A custom PCB built from that schematic is planned after the hackathon.

**Is this a safety system?**
No. Its safety metrics are simulation results.

---

## Sources

- [Florida Trend: FDOT Aerial Highway Network and Phase 1A](https://www.floridatrend.com/feature/2026/11/11/vertical-integration)
- [Florida HB 1093 summary](https://www.hklaw.com/en/insights/publications/2026/04/new-legislation-clears-the-runway-for-vertiport-development-in-florida)
- [Sebring Airport vertiport planning](https://sebring-airport.com/?p=4963)
- [FAA UAM ConOps 2.0 summary](https://www.halldale.com/civil-aviation/21140-cat-faa-considering-air-taxi-corridors-passing-lanes)
- [ASTM F3548-21](https://store.astm.org/f3548-21.html)
- [FAA EB 105A Vertiport Design](https://www.faa.gov/airports/engineering/engineering_briefs/eb_105a_vertiports)

---

## Submission checklist

Confirm against the official rules.

- [ ] Repository public, README current, data attribution present
- [ ] Dashboard deployed, judge credentials sent
- [ ] Ablation table and real-data results filled from committed runs
- [ ] Demo rehearsed end to end at the venue (GNSS fix, Wi-Fi/hotspot)
- [ ] Backup demo video recorded
- [ ] Wiring schematic PDF exported; datasheets in the repo
