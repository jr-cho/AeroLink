/** The two drafted disclaimers (.work/lawyer/R2-C.md, C-02): the pilot text and the ops text. */
const PILOT_TEXT = "AeroLink is a prototype and is advisory only. It is not certified or approved by the FAA, and it is not an air traffic control service or a collision-avoidance system. Its AI predicts traffic and suggests routes, and it can be wrong or out of date. Traffic from other aircraft comes from a public feed that can be incomplete or delayed, so an empty screen does not mean the sky is empty. You, as pilot in command, remain responsible for the safe operation of your aircraft: keep looking outside and follow the clearances and instructions ATC gives you directly. A message in AeroLink, including an \"ATC reroute\", is not a clearance. AeroLink records your unit's position, routes and answers and shows them to ATC operators.";
const OPS_TEXT = "AeroLink is a prototype and is advisory only. It is not an FAA system, and nothing here replaces your approved displays, procedures or authority. Its AI predicts traffic and phrases overviews and may be wrong or out of date. Live traffic is from a public feed that can be incomplete or delayed. Messages you send through AeroLink are not ATC clearances or instructions; use your normal channels. AeroLink records your actions under your username.";

/** API mock for the ATC dashboard: the shapes of `service/ops.py`, `api.py`, `adsb.py` and the `/ws/ops` socket. */
import type { Page, Route, WebSocketRoute } from "@playwright/test";
import { findPath, geometry, lines, mockApi, nodes } from "./mock.ts";

export const NOW = Math.floor(Date.now() / 1000);
const iso = (s: number) => new Date((NOW + s) * 1000).toISOString();
const at = (n: string) => nodes.get(n).geometry.coordinates as [number, number];
const nameOf = (n: string) => nodes.get(n).properties.name as string;

const NO_ATC = { requirement: "none", airspaces: [], legs: [], status: "not_needed", action: null, by: null, by_name: null, initials: null, at: null };
const needs = (requirement: string, airspaces: string[], legs: string[][]) => ({ ...NO_ATC, requirement, airspaces, legs, status: "pending" });

function mkRoute(flight_id: string, aircraft_id: string, source: string, vt: "passenger" | "medical", status: string, from: string, to: string, depart: number, extra: Record<string, unknown> = {}) {
  const path = findPath(from, to)!;
  const wp = geometry(path);
  const per = 150; // seconds between nodes in the mock
  const nodesW = path.map((n, i) => ({ node: n, name: nameOf(n), eta_p10: NOW + depart + i * per - 30, eta_p50: NOW + depart + i * per, eta_p90: NOW + depart + i * per + 30 + i * 5 }));
  const last = nodesW[nodesW.length - 1];
  const cone = Array.from({ length: 10 }, (_, k) => {
    const [lon, lat] = at(path[Math.min(path.length - 1, Math.floor((k * 5 * 60) / per))]);
    return { min: k * 5, p10: [lat, lon], p50: [lat, lon], p90: [lat, lon] };
  });
  return {
    flight_id, aircraft_id, source, vehicle_type: vt, status, medical: vt === "medical", path, path_names: path.map(nameOf), origin: from, destination: to, departure_s: NOW + depart,
    nodes: nodesW, waypoints: wp, eta_p10: last.eta_p10, eta_p50: last.eta_p50, eta_p90: last.eta_p90,
    prediction_history: [{ t: NOW - 120, eta_p50: last.eta_p50 - 60, eta_p90: last.eta_p90 - 60, reason: "initial estimate" }, { t: NOW - 30, eta_p50: last.eta_p50, eta_p90: last.eta_p90, reason: "medical flight dev-002 planned on shared nodes" }],
    cone, bunching_risk: 0.12, position: status === "planned" ? null : { lat: wp[1][0], lon: wp[1][1], alt_msl_m: 900, ground_speed_mps: 40, age_s: 1, online: true, off_route: false, stale: false, course_deg: 90 },
    atc: NO_ATC,
    ...extra,
  };
}

/** Realistic, mixed: one bunch at a one-pad site (2 arrivals), one heavy corridor, one clearance due, the rest light. */
function overviewPayload() {
  const pads1 = "ev-orl";
  const bunch = { kind: "bunch", node: pads1, name: nameOf(pads1), flight_ids: ["f-al02", "f-al07"], callsigns: ["AL-02", "AL-07"], from_s: NOW + 1320, to_s: NOW + 1740, within_min: 3, pads: 1 };
  return {
    generated_at: new Date(Date.now() - 8_000).toISOString(), horizon_min: 30, source: "llm", check: { rejected: false },
    bullets: [
      `${nameOf(pads1)} gets busy at 7:42 PM: AL-02 and AL-07 arrive within 3 min. Expect a short pad wait.`,
      "Heavy traffic near Kissimmee, 7:42 to 7:50 PM. N999AL crosses it; a reroute saves about 4 min.",
      "N123AL needs Class B clearance before 8:05 PM.",
      "Everything else is light.",
    ],
    facts: [
      bunch,
      { kind: "heavy_corridor", edge: "ev-kissimmee|ev-lake-wales", name: "Kissimmee", from_s: NOW + 1320, to_s: NOW + 1500, flights: ["N999AL"] },
      { kind: "clearance", flight_ids: ["f-dev1"], callsigns: ["N123AL"], count: 1, earliest_due_s: NOW + 3600, overdue_count: 0, medical: false },
      { kind: "reroute", reroute_id: "rr_overview", callsign: "N999AL", gain_s: 240, status: "proposed" },
    ],
    bunches: [bunch],
  };
}

export interface OpsMock {
  requests: { method: string; path: string; headers: Record<string, string> }[];
  alerts: any[];
  /** An operator session exists (the cookie the real server sets). Set false to test the login redirect or expiry. */
  session: boolean;
  /** Seconds left on the session `/api/auth/me` reports (the page signs out by itself just before it ends). */
  sessionSeconds: number;
  /** The next login attempts are answered 429 (rate limited). */
  limited: boolean;
  /** Bodies of the login attempts the page sent. */
  logins: { username: string; password: string }[];
  adsbState: string;
  routes: any[];
  /** Clearance marks the page sent: {flight, action}. */
  marks: { flight: string; action: string }[];
  /** Answer the next mark requests 422. */
  markFails: boolean;
  conflicts: any;
  /** Answer approve requests 409 (the reroute was answered elsewhere) without changing the proposal. */
  refuseApprove: boolean;
  /** The disclaimer version the operator last acknowledged (null: the card shows at sign-in). Defaults to the current one. */
  aiAck: string | null;
  /** Bump to change the `/api/models` payload (a background refresh that finds something new). */
  modelsVersion: number;
  /** AI reroute proposals (U-API §2), as the server holds them. */
  reroutes: any[];
  /** The AI overview (R-contract §1) as the server answers it. */
  overview: any;
  /** Answer `/api/ops/overview` 404 (an older backend): the card hides, nothing else changes. */
  overviewOff: boolean;
  /** Answer `/api/ops/overview` 503 `overview_unavailable` (a cold-start failure, R-contract §11.3). */
  overviewUnavailable: boolean;
  /** Answer `/api/ops/flights/{id}/overview` 503 `overview_unavailable` (R-contract §11.3). */
  flightOverviewUnavailable: boolean;
  /** Add a proposal for a flight (a passenger or medical route from the mock) and return it; it is not pushed by itself. */
  addReroute(id: string, flightId: string, extra?: Record<string, unknown>): any;
  /** Move a proposal to a new status, record the event, and push it over /ws/ops like the server does. */
  moveReroute(id: string, status: string, by?: string): void;
  sock: WebSocketRoute | null;
  clientMessages: string[];
  push(type: string, payload: unknown): void;
  drop(): void;
}

export async function mockOps(page: Page, init: Partial<OpsMock> = {}): Promise<OpsMock> {
  await mockApi(page); // airspace, basemap 404, the web map's endpoints
  const routes = [
    mkRoute("f-al01", "AL-01", "sim", "passenger", "active", "ev-water-st", "ev-suntrax", -150),
    mkRoute("f-al02", "AL-02", "sim", "passenger", "active", "ev-lal", "ev-orl", -100, { atc: needs("contact_tower", ["Lakeland Class D"], []) }),
    mkRoute("f-dev1", "dev-001", "aircraft", "passenger", "planned", "ev-sef", "ev-lal", 60, { callsign: "N123AL", atc: needs("clearance", ["Tampa Class B"], [["ev-sef", "ev-lal"]]) }),
    mkRoute("f-dev2", "dev-002", "aircraft", "medical", "active", "hp-61fl", "hp-fd28", -60, { bunching_risk: 0.41, callsign: "N456MD" }),
    // AL-07 (CP2): airborne, arriving at Orlando Exec with AL-02, so the overview's bunch has both flights in the list
    mkRoute("f-al07", "AL-07", "sim", "passenger", "active", "ev-parramore", "ev-orl", -40),
  ];
  const ref = (r: any) => ({ flight_id: r.flight_id, aircraft_id: r.aircraft_id, vehicle_type: r.vehicle_type, source: r.source });
  // Only two flights are in a predicted conflict (AL-02 and N456MD), so the screenshots look like a real afternoon.
  const conflicts = {
    generated_at: iso(0), horizon_min: 45,
    conflicts: [
      { id: "c1", kind: "junction", severity: "high", medical: true, t: NOW + 240, time_to_conflict_s: 240, node: "wp-orlando", edge: null, overlap_s: 70, gap_s: 20, flights: [ref(routes[1]), ref(routes[3])], bunching_risk: 0.41, locations: [] },
      { id: "c2", kind: "adsb", severity: "high", medical: false, t: NOW + 90, time_to_conflict_s: 90, node: null, edge: null, min_distance_m: 240, lat: 28.05, lon: -81.7, flights: [ref(routes[1])], adsb: { icao: "a1b2c3", callsign: "N123AB", category: "A7", alt_ft: 2800, gs_kt: 85, track_deg: 90 }, bunching_risk: null, locations: [] },
      { id: "c3", kind: "opposing", severity: "medium", medical: false, t: NOW + 600, time_to_conflict_s: 600, node: null, edge: ["wp-lakeland", "wp-auburndale"], overlap_s: 40, gap_s: 90, flights: [ref(routes[1]), ref(routes[3])], bunching_risk: 0.2, locations: [] },
    ],
    groups: [{ node: "wp-orlando", t: NOW + 240, time_to_conflict_s: 240, count: 3, medical: true, flights: [ref(routes[1]), ref(routes[3]), ref(routes[2])] }],
    bunching: [{ flight_id: "f-dev2", aircraft_id: "dev-002", risk: 0.41 }, { flight_id: "f-al01", aircraft_id: "AL-01", risk: 0.12 }],
    node_names: {}, adsb_state: "ok",
  };
  const m: OpsMock = {
    requests: [], session: true, sessionSeconds: 43200, limited: false, logins: [], adsbState: "ok", routes, marks: [], markFails: false, conflicts, sock: null, clientMessages: [],
    reroutes: [],
    overview: overviewPayload(),
    overviewOff: false,
    overviewUnavailable: false,
    flightOverviewUnavailable: false,
    modelsVersion: 1,
    aiAck: "2026-11-27",
    refuseApprove: false,
    addReroute(id, flightId, extra = {}) {
      const r = routes.find((x) => x.flight_id === flightId)!;
      // the proposal's route avoids the first corridor of the current one, like the what-if alternatives do
      const banned = new Set(lines.filter((l) => r.path.includes(l.properties.from) && r.path.includes(l.properties.to) && l.properties.kind === "corridor").slice(0, 1).map((l) => l.properties.id));
      const alt = findPath(r.origin, r.destination, banned) ?? r.path;
      const rr = {
        id, flight_id: flightId, aircraft_id: r.aircraft_id, callsign: r.callsign ?? r.aircraft_id, source: r.source === "sim" ? "sim" : "aircraft", destination: r.destination, status: "proposed",
        reason: "Predicted congestion at Kissimmee 7:42 to 7:50 PM and possible conflict with SIM-P03", trigger: r.medical ? "medical" : "faster", gain_s: 240, requirement_change: null, simulated: r.source === "sim",
        current: { path: r.path, waypoints: geometry(r.path), eta_s: NOW + 900, atc: { requirement: "none" } },
        proposed: { route_id: `${id}-alt`, path: alt, waypoints: geometry(alt), eta_s: NOW + 660, eta_window_s: [NOW + 630, NOW + 690], segments: [], atc: { requirement: "none" } },
        created_at: iso(0), expires_at: iso(180), approved_by: null, approved_at: null, responded_at: null,
        events: [{ at: iso(-10), kind: "proposed", by: "AeroLink AI" }], ...extra,
      };
      m.reroutes.push(rr);
      return rr;
    },
    moveReroute(id, status, by = "dev-001") {
      const rr = m.reroutes.find((x) => x.id === id)!;
      rr.status = status;
      if (status !== "expired") rr.responded_at = iso(0);
      rr.events.push({ at: iso(0), kind: status, by });
      m.push("reroute", rr);
    },
    alerts: [
      { id: 3, at: iso(-20), kind: "gnss_fix_lost", severity: "warn", aircraft_id: "dev-001", message: "dev-001 lost its GNSS fix", detail: {}, acked_at: null, acked_by: null },
      { id: 2, at: iso(-80), kind: "off_route", severity: "warn", aircraft_id: "AL-02", message: "AL-02 is off route", detail: {}, acked_at: null, acked_by: null },
      { id: 1, at: iso(-300), kind: "unit_offline", severity: "critical", aircraft_id: "dev-002", message: "dev-002 went offline", detail: {}, acked_at: iso(-200), acked_by: "ops1" },
    ],
    push(type, payload) { if (type === "alert") m.alerts.unshift(payload); m.sock?.send(JSON.stringify({ type, payload })); },
    drop() { m.sock?.close({ code: 1006 }); m.sock = null; },
    ...init,
  };
  const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  let tick = 0;

  await page.route(/\/(api|healthz)(\/|$|\?)/, async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname;
    m.requests.push({ method: req.method(), path, headers: req.headers() });
    const gone = () => json(route, { detail: { error: "session_required", message: "sign in to the operations dashboard" } }, 401);
    if (path === "/api/auth/me") {
      return m.session ? json(route, { username: "ops1", name: "Ops One", expires_at: new Date(Date.now() + m.sessionSeconds * 1000).toISOString(), ai_ack_version: m.aiAck }) : gone();
    }
    if (path === "/api/auth/login" && req.method() === "POST") {
      const body = JSON.parse(req.postData() ?? "{}");
      m.logins.push(body);
      if (m.limited) return json(route, { detail: { error: "too_many_attempts", message: "too many attempts; wait a minute" } }, 429);
      if (body.username === "ops1" && body.password === "right-password-1") { m.session = true; return json(route, { username: "ops1", name: "Ops One", expires_at: iso(43200) }); }
      return json(route, { detail: { error: "invalid_login", message: "Wrong username or password" } }, 401);
    }
    if (path === "/api/auth/logout" && req.method() === "POST") { m.session = false; return json(route, { ok: true }); }
    if (path === "/api/auth/ai-ack" && req.method() === "POST") { m.aiAck = JSON.parse(req.postData() ?? "{}").version; return route.fulfill({ status: 204 }); }
    if (!m.session && path !== "/api/airspace" && path !== "/healthz" && path !== "/api/ai") return gone(); // the real Caddy + backend: no session, no data
    if (path === "/api/state") {
      tick++;
      const pos = (n: string, k: number) => { const [lon, lat] = at(n); return { lon: lon + 0.01 * k, lat }; };
      const row = (id: string, source: string, vt: string, n: string, extra = {}) => ({ id, callsign: ({ "dev-001": "N123AL", "dev-002": "N456MD" } as Record<string, string>)[id] ?? id, source, vehicle_type: vt, ...pos(n, tick % 3), alt_msl_m: 900, ground_speed_mps: 40, course_deg: 90, timestamp: iso(0), online: true, off_route: false, flight_status: "active", stale: false, last_fix_age_s: 1, ...extra });
      return json(route, { method: "aerolink", aircraft: [
        row("dev-001", "aircraft", "passenger", "ev-sef", { stale: true, last_fix_age_s: 20 }), row("dev-002", "aircraft", "medical", "hp-61fl"),
        row("AL-01", "sim", "passenger", "ev-water-st"), row("AL-02", "sim", "passenger", "ev-lal", { off_route: true }), row("AL-03", "sim", "passenger", "ev-orl"),
        row("AL-07", "sim", "passenger", "ev-parramore"),
      ] });
    }
    if (path === "/api/adsb") {
      return json(route, { attribution: "Live ADS-B data from adsb.lol, ODbL 1.0", feed: { state: m.adsbState, last_ok_at: iso(-2), error: m.adsbState === "down" ? "timeout" : null, kept: 2, dropped: 31 },
        aircraft: [
          { icao: "a1b2c3", callsign: "N123AB", category: "A7", lat: 28.05, lon: -81.75, alt_ft: 2800, gs_kt: 85, track_deg: 90, vert_rate_fpm: 0, seen_s: 1.2, source: "adsb" },
          { icao: "d4e5f6", callsign: null, category: "A1", lat: 27.7, lon: -81.9, alt_ft: 1500, gs_kt: 90, track_deg: 200, seen_s: 3, source: "adsb" },
        ] });
    }
    const mark = /^\/api\/ops\/flights\/([^/]+)\/clearance$/.exec(path);
    if (mark && req.method() === "POST") {
      const id = decodeURIComponent(mark[1]);
      const action = JSON.parse(req.postData() ?? "{}").action;
      m.marks.push({ flight: id, action });
      const r = m.routes.find((x: any) => x.flight_id === id || x.aircraft_id === id);
      if (!r) return json(route, { detail: { error: "unknown_flight", message: "no such flight" } }, 404);
      if (m.markFails) return json(route, { detail: { error: "wrong_action", message: "this route needs 'cleared', not 'contacted'" } }, 422);
      if (r.atc.requirement === "none") return json(route, { detail: { error: "not_needed", message: "no ATC action needed" } }, 409);
      if (action === "revoke") r.atc = { ...r.atc, status: "pending", action: null, by: null, by_name: null, initials: null, at: null };
      else r.atc = { ...r.atc, status: "done", action, by: "ops1", by_name: "Ops One", initials: "OO", at: iso(0) };
      return json(route, r.atc);
    }
    if (path === "/api/ops/routes") return json(route, m.routes);
    const fo = /^\/api\/ops\/flights\/([^/]+)\/overview$/.exec(path);
    if (fo && req.method() === "GET") {
      if (m.flightOverviewUnavailable) return json(route, { error: "overview_unavailable" }, 503);
      const r = m.routes.find((x: any) => x.flight_id === decodeURIComponent(fo[1]));
      if (!r) return json(route, { detail: "unknown flight" }, 404);
      // R-contract §2: at most two sentences, plain text, written from the flight's facts
      return json(route, { flight_id: r.flight_id, callsign: r.callsign ?? r.aircraft_id, source: "template", generated_at: iso(-3),
        text: `Arrives ${nameOf(r.destination)} around 7:42 to 7:49 PM. Its route crosses heavy traffic near Kissimmee at about 7:44 PM; the Lake Wales route avoids it.` });
    }
    if (path === "/api/ops/overview") {
      if (m.overviewUnavailable) return json(route, { error: "overview_unavailable" }, 503);
      return m.overviewOff ? json(route, { detail: "Not Found" }, 404) : json(route, m.overview);
    }
    if (path === "/api/ops/reroutes" && req.method() === "GET") {
      const flight = url.searchParams.get("flight_id");
      if (flight) return json(route, { reroutes: m.reroutes.filter((x) => x.flight_id === flight).sort((a, b) => b.created_at.localeCompare(a.created_at)) });
      const open = url.searchParams.get("status") === "open";
      return json(route, { reroutes: m.reroutes.filter((x) => !open || x.status === "proposed" || x.status === "approved") });
    }
    const rrAct = /^\/api\/ops\/reroutes\/([^/]+)\/(approve|dismiss)$/.exec(path);
    if (rrAct && req.method() === "POST") {
      const rr = m.reroutes.find((x) => x.id === rrAct[1]);
      if (!rr) return json(route, { detail: { error: "unknown_reroute", message: "no such reroute" } }, 404);
      if (rr.status !== "proposed" || (rrAct[2] === "approve" && m.refuseApprove)) return json(route, { detail: { error: "not_proposed", message: `this reroute is already ${rr.status === "proposed" ? "approved" : rr.status}` } }, 409);
      if (rrAct[2] === "approve") Object.assign(rr, { status: "approved", approved_by: "ops1", approved_at: iso(0) });
      else rr.status = "dismissed";
      rr.events.push({ at: iso(0), kind: rrAct[2] === "approve" ? "approved" : "dismissed", by: "ops1" });
      m.push("reroute", rr);
      // a simulated pilot follows AeroLink's rules and answers on its own after a few seconds (contract §8)
      if (rrAct[2] === "approve" && rr.source === "sim") setTimeout(() => m.moveReroute(rr.id, "acknowledged", "simulated pilot"), 2500);
      return json(route, rr);
    }
    if (path === "/api/ops/conflicts") return json(route, m.conflicts);
    if (path.startsWith("/api/ops/whatif/")) {
      const id = decodeURIComponent(path.split("/").pop()!);
      const r = m.routes.find((x) => x.flight_id === id || x.aircraft_id === id);
      if (!r) return json(route, { detail: "no such planned or active flight" }, 404);
      const alt = findPath(r.origin, r.destination, new Set(lines.filter((l) => r.path.includes(l.properties.from) && r.path.includes(l.properties.to) && l.properties.kind === "corridor").slice(0, 1).map((l) => l.properties.id))) ?? r.path;
      return json(route, { flight_id: r.flight_id, aircraft_id: r.aircraft_id, status: r.status, from_node: r.path[0], from_name: nameOf(r.path[0]), destination: r.destination, method: "aerolink", forecaster: "cv",
        current: { path: r.path, via: r.path_names.join(", then "), eta_p50: NOW + 900, total_s: 900 },
        alternatives: [{ path: alt, via: alt.map(nameOf).join(", then "), names: alt.map(nameOf), est_time_s: 640, wait_s: 20, total_s: 660, time_saved_s: 240, bunching_risk: 0.05, traffic: "low", reason: "cruise 640s, pad wait 20s" }] });
    }
    if (path === "/api/ops/congestion") {
      const levels = (peak: string) => ["low", "low", "low", "moderate", "moderate", peak, peak, "moderate", "low", "low"];
      return json(route, { steps_min: [0, 5, 10, 15, 20, 25, 30, 35, 40, 45], sites: [{ node: "hp-fd28", name: nameOf("hp-fd28"), pads: 2, now: "low", levels: levels("heavy"), peak: "heavy", peak_in_min: 25, arrivals_45: 2, departures_45: 0 }, { node: "ev-orl", name: nameOf("ev-orl"), pads: 4, now: "low", levels: levels("moderate"), peak: "moderate", peak_in_min: 15, arrivals_45: 1, departures_45: 1 }], edges: [] });
    }
    if (path === "/api/devices") {
      const health = (rssi: number, fix: string) => ({ uptime_s: 3725, reset_reason: "power_on", wifi_rssi_dbm: rssi, gnss: { fix, satellites: fix === "none" ? 0 : 11, hdop: fix === "none" ? 99.9 : 0.9 }, free_heap_bytes: 150000, firmware_version: "0.4.2" });
      return json(route, [
        { id: "dev-001", callsign: "N123AL", vehicle_type: "passenger", demo: true, online: true, status: "degraded", status_reasons: ["no_gnss_fix"], health: health(-61, "none"), health_age_s: 4, age_s: 3, stale: false },
        { id: "dev-002", callsign: "N456MD", vehicle_type: "medical", demo: false, online: true, status: "healthy", status_reasons: [], health: health(-52, "3d"), health_age_s: 6, age_s: 1, stale: false },
        { id: "dev-003", callsign: null, vehicle_type: "passenger", demo: false, online: false, status: "offline", status_reasons: [], health: null, health_age_s: null, age_s: 400, stale: true },
      ]);
    }
    if (path === "/healthz") {
      const wd = ["mqtt", "forecast", "simulator", "adsb", "database", "models"].map((n) => ({ name: n, state: n === "adsb" && m.adsbState !== "ok" ? "fail" : n === "simulator" ? "warn" : "ok", value: 1, threshold: "< 20 s", detail: `${n} detail`, since: iso(-100), checked_at: iso(0) }));
      return json(route, { ok: true, mqtt: true, state: "warn", stats: {}, ...(url.searchParams.get("deep") ? { watchdogs: wd } : {}) });
    }
    if (/^\/api\/aircraft\/[^/]+\/telemetry$/.test(path)) {  // six stored points in two segments (a reconnect in between)
      const row = (i: number, seg: number) => ({ timestamp: iso(i - 6), lat: 28 + i * 0.001, lon: -81.5, segment_id: seg });
      return json(route, [0, 1, 2].map((i) => row(i, 1000)).concat([3, 4, 5].map((i) => row(i, 2000))));
    }
    if (path === "/api/alerts" && req.method() === "GET") return json(route, m.alerts);
    const ack = /^\/api\/alerts\/(\d+)\/ack$/.exec(path);
    if (ack && req.method() === "POST") {
      const a = m.alerts.find((x) => x.id === Number(ack[1]));
      if (!a) return json(route, { detail: "unknown alert" }, 404);
      a.acked_at ??= iso(0);
      a.acked_by ??= "ops1"; // the logged-in operator: the page sends no name
      return json(route, a);
    }
    if (path === "/api/traffic") {
      const level = (i: number, t: number) => (i % 5 === 0 && t >= 15 ? "heavy" : i % 3 === 0 && t >= 15 ? "moderate" : "low");
      const edges: Record<string, any> = {};
      // U-API §1: `levels` (every 5 min, the list the web reads) is kept; `by_minute` is the new minute-keyed view (p90 levels)
      lines.forEach((l, i) => { edges[`${l.properties.from}->${l.properties.to}`] = { levels: [0, 5, 10, 15, 20, 25, 30, 35, 40, 45].map((t) => level(i, t)), by_minute: { "0": level(i, 0), "15": level(i, 15), "30": level(i, 30), "45": level(i, 45) }, now: level(i, 0) }; });
      const cone_steps = m.routes.map((r) => ({ aircraft_id: r.aircraft_id, flight_id: r.flight_id, status: r.status, vehicle_type: r.vehicle_type, path: r.path, steps: r.cone.map((c: any) => ({ min: c.min, p10: c.p10, p50: c.p50, p90: c.p90 })) }));
      const cones = { type: "FeatureCollection", features: m.routes.map((r) => ({ type: "Feature", properties: { flight_id: r.flight_id, aircraft_id: r.aircraft_id, status: r.status, vehicle_type: r.vehicle_type, horizon_min: 45 }, geometry: { type: "Polygon", coordinates: [(() => { const pts = r.waypoints.map(([la, lo]: number[]) => [lo, la]); const w = 0.004; return [...pts.map(([x, y]: number[]) => [x - w, y + w]), ...pts.map(([x, y]: number[]) => [x + w, y - w]).reverse(), [pts[0][0] - w, pts[0][1] + w]]; })()] } })) };
      return json(route, { forecast_time: iso(0), forecast_age_s: 4, stale: false, steps_min: [0, 5, 10, 15, 20, 25, 30, 35, 40, 45], nodes: {}, edges, cones, cone_steps });
    }
    if (path === "/api/ai") {
      // U-API §3: numbers as the committed eval files give them (the mock mirrors the M5 results, not a live run)
      return json(route, {
        forecaster: { name: "aerolink-gbm", version: "0.4.0", updated_at: iso(-420), forecast_age_s: 8 },
        models: [
          { key: "forecaster", title: "Traffic & arrival-time forecaster", name: "aerolink-gbm", what: "Predicts each corridor's traffic 0 to 45 min ahead and each flight's ETA window.", drives: "Congestion colours, ETA windows and conflict predictions." },
          { key: "demand", title: "Demand forecaster", name: "demand forecaster", what: "Expects arrivals and departures at each site from the schedule pattern.", drives: "Pad load at the sites." },
          { key: "offroute", title: "Off-route detector", name: "off-route detector", what: "Learns how far a flight may drift from its route before it counts as off route.", drives: "The Off route call on the flight list." },
          { key: "summary", title: "Route summary (language model)", name: "route-summary model", what: "Writes the plain-words reason for a route or a reroute, checked against the numbers.", drives: "Route reasons and reroute cards." },
        ],
        results: { eta_error_ai_s: 61, eta_error_classical_s: 104, window_coverage: 0.84, offroute_precision: 1.0, offroute_recall: 0.875, route_choice_note: "On today's network the chosen routes match the classical planner; the AI earns its keep on ETA accuracy and on predicting congestion." },
        disclaimer: { version: "2026-11-27", text: PILOT_TEXT, pilot: PILOT_TEXT, ops: OPS_TEXT },
      });
    }
    if (path === "/api/models") return json(route, { method: "aerolink", forecaster: "cv", models: [{ name: "cv", loaded: true, version: m.modelsVersion }, { name: "chronos", loaded: false, error: null }, { name: "aerolink-gbm", loaded: true }] });
    return route.fallback();
  });

  await page.routeWebSocket(/\/ws\/ops$/, (ws) => {
    m.sock = ws;
    ws.onMessage((msg) => m.clientMessages.push(String(msg)));
    ws.send(JSON.stringify({ type: "state", payload: { unacked: m.alerts.filter((a) => !a.acked_at), watchdogs: [] } }));
  });
  return m;
}
