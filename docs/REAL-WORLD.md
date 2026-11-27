# Path to real-world use

AeroLink is a simulation and real-data replay prototype. This document says what it shows today, and what would have to happen, and be approved by others, before it could be used in real operations. It does not claim that any approval exists, and it does not claim that the path is easy. Everything below is from a regulatory research review (not legal advice); items marked **UNVERIFIED** or **counsel/FAA input needed** are open and are listed again in section 3.

## 1. Where AeroLink stands today

AeroLink is a research prototype. The backend, simulator, forecasting models and "AeroLink Ops" dashboard run on simulated traffic and on replayed real flight tracks (adsb.lol). It demonstrates:

- predicted corridor and landing-pad congestion, with the ETA model scored against held-out replay data;
- route suggestions that the pilot can accept or decline;
- an operations dashboard for AeroLink or vertiport operations staff (not FAA air traffic control);
- a portable, screenless unit design that is described in the architecture but has no firmware in this repository yet.

It is **not certified, not for flight, and not an ATC system**. Its design stays inside what the FAA says about who is in charge:

- **Advisory only.** The pilot in command stays the final authority (14 CFR 91.3(a)). AeroLink suggests; it never commands.
- **Voice ATC stays the authority.** Where a clearance or radio contact is needed, it comes from real ATC, not from a software status. AeroLink tells the pilot to confirm with ATC.

That matches the FAA's published position. Three short quotes from primary sources that were re-checked:

- "Voice remains the primary and controlling air/ground communications means" ([AIM 5-3-1](https://www.faa.gov/air_traffic/publications/atpubs/aim_html/chap5_section_3.html)).
- "The use of CPDLC is approved to augment the voice communication requirements" ([FAA Order JO 7110.65BB, para 14-2-1](https://www.faa.gov/air_traffic/publications/atpubs/atc_html/chap14_section_2.html)).
- "The agreement will not include the transfer of any authority for airspace management or access upon signing." ([FAA eVTOL Integration Pilot Program notice, 90 FR 44751, docket FAA-2025-2633](https://public-inspection.federalregister.gov/2025-17844.pdf)).

Florida DOT is listed as a lead participant in the FAA's eVTOL Integration Pilot Program ([faa.gov/aam/eipp](https://www.faa.gov/aam/eipp)), so the state context for advanced air mobility is active. That is context only; it is not an endorsement of AeroLink.

## 2. Phased path

### Phase 0: Demo (now)

Clearly labelled simulation and replay.

- Persistent on-screen notice: "Prototype, not certified, not for flight, not ATC".
- Operations wording on the dashboard ("Ops reroute (advisory)", "Ops marked: clearance obtained, confirm with ATC"), not "ATC".
- AI text states what is actually checked; the traffic view is labelled internet ADS-B, delayed, advisory, with data age.
- Modelled sites are labelled as modelled, and the project states it is not affiliated with the FAA, FDOT or any airport.
- OpenStreetMap and adsb.lol (ODbL) credit on the pilot screen; code is MIT, data derived from adsb.lol stays under ODbL 1.0.

These labels were updated to match the research review's recommended fixes before outside viewers see the demo.

### Phase 1: Supervised field trial (ground or shadow mode)

A partner operator and a vertiport or hospital site, with **no advice reaching a flying crew**. AeroLink runs beside the real operation, records what it would have said, and is compared with what happened.

Would need:

- **Operator roles and verification.** Named, verified operator accounts with roles, replacing generic sign-ins. Whether a non-FAA message could ever count as an "ATC instruction" under 91.123(b) is for an attorney (**counsel/FAA input needed**).
- **Real-ATC coordination.** Any use beside a real controller would need FAA input or a letter of agreement. Whether any FAA path lets ATC use a non-FAA tool is **UNVERIFIED**. AeroLink should not be shown to controllers as a clearance source.
- **Operational data.** Live TFR, NOTAM and weather feeds; obstacle data; Class E surface areas at towered airports. The current airspace is an FAA snapshot with no live notices.
- **Site status (Florida).** Per-site status fields. Under s. 330.30(2)(a), an airport owner needs a public airport license, private airport registration or temporary registration before aircraft operate to or from it. Vertiports and heliports fall under the airport definition in s. 330.27(3). Hospital pads are not within the s. 330.30(3)(d) helistop exemption, and air-ambulance private airports need a certificate (existing ones by July 1, 2030). Each site's actual status is **UNVERIFIED** and must come from the owner or FDOT. The "medical" flag would need verification that an operator is licensed before it gives anyone priority.
- **Privacy.** A privacy notice, retention period and deletion path; a stated approach to the FAA's LADD and ADS-B privacy (PIA) programmes; tamper-evident acknowledgement records; rotatable pairing codes; removal or hashing of aircraft identifiers in public data. Which remaining Florida Digital Bill of Rights duties apply is **UNVERIFIED**.
- **Hardware.** Portable first (a screenless unit that is not installed in the aircraft), with FCC marking and an authorized GNSS module. The demo unit uses Wi-Fi and a GPS module; the link for later units is still to be decided (one option is an unlicensed ISM-band mesh radio via a ground gateway). Whether the unit's use in the air is acceptable under Part 15 and 14 CFR 91.21 (portable electronic devices) is **UNVERIFIED and pending counsel**. In Phase 1 the unit would stay on the ground or be tested with explicit permission.
- **Data licences.** ODbL share-alike and attribution compliance (a public map made from a derived database can trigger it). Commercial use of adsb.lol's free tier is **UNVERIFIED**; a written answer from adsb.lol, or a licensed data source, would be needed.
- **Engineering.** A written safety case (hazards, failure modes, what happens when data is stale or the link is down); independent validation of the ETA model on real flights, not only replay; a cybersecurity review of the uplink, accounts and device pairing; terms of use and a liability review.

### Phase 2: Advisory use in real flights under an operator's program

Real flights, with an operator that holds the necessary authority and takes responsibility for the operation. AeroLink remains advisory. The pilot flies and talks to ATC by voice.

Would need everything in Phase 1, plus:

- An operator agreement stating who is responsible, and operator-level procedures for using an advisory tool. If the operator is a Part 135 certificate holder, its operational control rules would apply to the tool (to be confirmed).
- Possibly participation in a state or federal programme such as the eIPP, which keeps airspace authority with the FAA. Whether AeroLink would fit into one is a question for the FAA and FDOT.
- Airborne hardware status settled: Part 15 operation, 91.21 handling and FCC labelling confirmed by counsel before any unit flies. The cellular alternative has its own question under 47 CFR 22.925 (airborne cellular ban). Whether it covers LTE-M IoT modems is **UNVERIFIED**, which is one reason the design uses a mesh radio.
- Traffic display framed as situational awareness only. FAA guidance (AC 90-114B para 2.4.3.1) says to use a traffic display to supplement what can be seen out the window, except when authorized to conduct ADS-B In operations described in the appendices; AeroLink's internet-fed display does not replace that.
- Reroute flow reviewed with the FAA's view of clearances: accept/decline wording, a visible change in airspace requirement, and a position re-check before a reroute is acted on.
- Field evidence from Phase 1 feeding the safety case.

### Phase 3: Scaled operations

Several operators, sites and corridors.

- More sites and corridor alternatives (today A5 and B1 choose the same routes on the current network, so outcome gains are not yet shown).
- Corridors aligned with whatever the FAA and FDOT publish; the FDOT Chapter 14-60 vertiport update is announced but not shown as adopted (**UNVERIFIED**).
- Operational-grade data contracts (traffic, weather, NOTAM), uptime commitments, incident reporting and audit logs.
- Ongoing model monitoring, retraining governance and independent assurance, consistent with the FAA's Roadmap for AI Safety Assurance.
- Any public-body use in Florida would raise public-records and procurement questions (to be confirmed).

## 3. Open questions for counsel and the FAA

These are unresolved. Nothing here should be relied on until confirmed.

1. Navigation-data product-liability case law, and whether Florida courts enforce click-through disclaimers: **UNVERIFIED**.
2. Whether any FAA process lets a non-FAA tool amend a clearance or be used by ATC, and whether a non-FAA message could be an "ATC instruction" under 91.123(b): **UNVERIFIED, counsel/FAA input needed**.
3. FAA views on internet-fed traffic displays for pilots, and on ATC use of third-party tools: **FAA input needed**.
4. Whether the airborne cellular ban (47 CFR 22.925) covers LTE-M IoT modems: **UNVERIFIED**.
5. Whether a portable mesh-radio unit may operate in flight under Part 15 and 14 CFR 91.21: **UNVERIFIED, pending counsel**.
6. Current FCC authorization status of the ESP32-S3 module (the January 2022 grant has later permissive changes the research could not confirm) and of the GNSS module: **UNVERIFIED**.
7. Whether Chapter 14-60 F.A.C. has been amended to cover vertiports: **UNVERIFIED**.
8. Which Florida Digital Bill of Rights duties beyond ss. 501.703 and 501.715 reach a small company: **UNVERIFIED**.
9. Whether adsb.lol's free tier allows commercial use: **UNVERIFIED**.
10. Each target site's state licence, registration or certificate status, and FAA approval status of any planned vertiport: **UNVERIFIED**.
11. Whether a newer revision of AC 20-173 exists: **UNVERIFIED**.

## 4. Disclaimer

This is a research summary, not legal advice. It reflects a review of public sources as of 27 November 2026 and has not been confirmed by a licensed aviation attorney or by the FAA. AeroLink is not certified, not approved for flight, not affiliated with the FAA, FDOT or any airport, and must not be used for navigation or air traffic control.
