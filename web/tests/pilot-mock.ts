/** Pilot-screen mock: the device API and the WebSocket of `service/pilot.py`, driven by the test. */
import type { Page, Route, WebSocketRoute } from "@playwright/test";
import { findPath, geometry, length, lines, mockApi, nodes } from "./mock.ts";

export const DEVICE = "dev-001";
export const CODE = "K7QF-2XM4TB";
const UUID = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const iso = (s: number) => new Date(Date.now() + s * 1000).toISOString();

export const DISCLAIMER_VERSION = "2026-11-27";
export const PILOT_DISCLAIMER = "AeroLink is a prototype and is advisory only. It is not certified or approved by the FAA, and it is not an air traffic control service or a collision-avoidance system. Its AI predicts traffic and suggests routes, and it can be wrong or out of date. Traffic from other aircraft comes from a public feed that can be incomplete or delayed, so an empty screen does not mean the sky is empty. You, as pilot in command, remain responsible for the safe operation of your aircraft: keep looking outside and follow the clearances and instructions ATC gives you directly. A message in AeroLink, including an \"ATC reroute\", is not a clearance. AeroLink records your unit's position, routes and answers and shows them to ATC operators.";
export const OPS_DISCLAIMER = "AeroLink is a prototype and is advisory only. It is not an FAA system, and nothing here replaces your approved displays, procedures or authority. Its AI predicts traffic and phrases overviews and may be wrong or out of date. Live traffic is from a public feed that can be incomplete or delayed. Messages you send through AeroLink are not ATC clearances or instructions; use your normal channels. AeroLink records your actions under your username.";
/** `/api/ai` as the server serves it (U-API contract §3). Numbers mirror the committed eval files. */
export const AI_INFO = {
  forecaster: { name: "aerolink-gbm", version: "test", updated_at: "2026-11-26T00:00:00Z", forecast_age_s: 8 },
  models: [
    { key: "forecaster", title: "Traffic & arrival-time forecaster", name: "aerolink-gbm", what: "Predicts corridor congestion 0 to 45 min ahead.", drives: "Corridor colours, ETA ranges, route choice." },
    { key: "demand", title: "Pad demand forecaster", name: "aerolink-demand", what: "Predicts pad demand by hour.", drives: "Pad wait estimates." },
    { key: "offroute", title: "Off-route detector", name: "aerolink-offroute", what: "Learns when a flight has left its route.", drives: "The off-route banner." },
    { key: "summary", title: "Route summary writer", name: "aerolink-summary", what: "Writes the plain-words reason for each route.", drives: "Route card reasons." },
  ],
  results: { eta_error_ai_s: 61, eta_error_classical_s: 104, window_coverage: 0.84, offroute_precision: 1.0, offroute_recall: 0.875, route_choice_note: "Route choices match the classical planner on today's network." },
  disclaimer: { version: DISCLAIMER_VERSION, text: PILOT_DISCLAIMER, pilot: PILOT_DISCLAIMER, ops: OPS_DISCLAIMER },
};

export interface PilotMock {
  paired: boolean;
  online: boolean;
  vehicleType: "passenger" | "medical";
  origin: string;
  flight: any | null;
  /** Seconds until route options expire. */
  expiresIn: number;
  wsAllowed: boolean;
  dest?: string;
  /** ADS-B aircraft near the unit (U-API contract §4), served in the state and the traffic message. */
  adsb: any[];
  /** The ADS-B feed state served with the device state and the traffic message (U-API contract §10). */
  adsbState: "ok" | "stale" | "down" | "rate_limited" | "disabled";
  /** The disclaimer version this unit has acknowledged (`ai_ack_version`); acknowledged by default so flows are not blocked. */
  aiAck: string | null;
  aiAcks: string[];
  /** The ATC reroute the server holds for this unit (U-API contract §2); returned as `pending_reroute` while approved. */
  reroute: any | null;
  /** Answers the pilot sent: "acknowledge" or "unable". */
  rerouteAnswers: string[];
  /** Predicted level of leg k (0 = first leg) at the aircraft's ETA, for every route's `segments`. Default: low. */
  legLevels: Record<number, "low" | "moderate" | "heavy">;
  /** What ATC must be told about every route in this mock (E6.14); "none" by default. */
  atc: { requirement: "none" | "contact_tower" | "contact_approach" | "clearance"; airspaces: string[]; status: "not_needed" | "pending" | "done" };
  requests: { method: string; path: string; body: any }[];
  stateCalls: number;
  pairCalls: any[];
  sock: WebSocketRoute | null;
  /** Server -> screen message. */
  push(type: string, payload: unknown): void;
  /** Send a heartbeat every 5 s like the server does; a test sets this false to simulate a silent link. */
  heartbeats: boolean;
  /** Close the open socket (the page will reconnect, or poll while it can't). */
  drop(): void;
  stateBody(): any;
  routes(dest: string, requestId: string, revision?: number, ids?: number): any;
}

export function fixAt(node: string) {
  const [lon, lat] = nodes.get(node).geometry.coordinates;
  return { lat, lon, alt_msl_m: 900, ground_speed_mps: 40, course_deg: 90, timestamp: iso(0), fix: { type: "3d", hdop: 0.9, satellites: 14, replayed: false } };
}

/** A reroute proposal as the server sends it (U-API contract §2) for the flight in the mock: avoid the first leg. */
export function makeReroute(m: PilotMock, over: Record<string, unknown> = {}) {
  const cur = m.flight?.path ?? findPath(m.origin, m.dest!)!;
  // ban the second leg of the current route (findPath bans edge ids): the proposal shares the first leg, then diverges
  const second = lines.find((l: any) => (l.properties.from === cur[1] && l.properties.to === cur[2]) || (l.properties.to === cur[1] && l.properties.from === cur[2]));
  const path = findPath(m.origin, m.dest!, new Set([second.properties.id])) ?? cur;
  const t0 = Date.now() / 1000;
  const segments = path.slice(1).map((n, k) => ({ from: path[k], to: n, eta_s: Math.round(t0 + 120 * (k + 1)), level: "low" }));
  return {
    id: "rr_test_1", flight_id: m.flight?.flight_id ?? "f-x", aircraft_id: DEVICE, callsign: "N123AL", destination: m.dest, status: "approved",
    reason: "Predicted congestion at Kissimmee 7:42 to 7:50 PM and possible conflict with SIM-P03", trigger: "faster", gain_s: 240,
    current: { path: cur, waypoints: geometry(cur), eta_s: 900 },
    proposed: { route_id: UUID(777), path, waypoints: geometry(path), eta_s: t0 + 600, eta_window_s: [-30, 60], segments, atc: { requirement: "none" } },
    requirement_change: null,
    created_at: iso(0), expires_at: iso(600), approved_by: "joshua", approved_at: iso(0), responded_at: null, events: [],
    ...over,
  };
}

/** The forecast the pilot screen gets from `/api/devices/{id}/traffic` (U-API contract §5, same payload as /api/traffic).
 *  Levels: heavy at +15 on every 5th edge, moderate on every 3rd; `by_minute` per edge (contract §1). */
function trafficBody() {
  const level = (i: number, min: number) => (i % 5 === 0 && min >= 15 ? "heavy" : i % 3 === 0 && min >= 15 ? "moderate" : "low");
  const edges: Record<string, any> = {};
  lines.forEach((l, i) => {
    const by = { "0": level(i, 0), "15": level(i, 15), "30": level(i, 30), "45": level(i, 45) };
    edges[`${l.properties.from}->${l.properties.to}`] = { now: by["0"], in_15_min: by["15"], in_30_min: by["30"], in_45_min: by["45"], by_minute: by };
  });
  return { forecast_time: iso(0), forecast_age_s: 0, stale: false, steps_min: [0, 5, 10, 15, 20, 25, 30, 35, 40, 45], nodes: {}, edges };
}

export async function mockPilot(page: Page, init: Partial<PilotMock> = {}): Promise<PilotMock> {
  await mockApi(page); // airspace, basemap 404
  await page.route(/\/api\/ai$/, (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify(AI_INFO) }));
  const m: PilotMock = {
    paired: false, online: true, vehicleType: "passenger", origin: "ev-water-st", flight: null, expiresIn: 300, wsAllowed: true, heartbeats: true,
    legLevels: {}, reroute: null, rerouteAnswers: [], aiAck: DISCLAIMER_VERSION, aiAcks: [], adsb: [], adsbState: "ok",
    atc: { requirement: "none", airspaces: [], status: "not_needed" },
    requests: [], stateCalls: 0, pairCalls: [], sock: null,
    push(type, payload) { m.sock?.send(JSON.stringify({ type, payload })); },
    drop() { m.sock?.close({ code: 1006 }); m.sock = null; },
    stateBody() {
      return { id: DEVICE, vehicle_type: m.vehicleType, demo: true, online: m.online, last_seen: iso(-1), latest_fix: fixAt(m.origin), current_flight: m.flight && m.atc.requirement !== "none" ? { ...m.flight, atc: { ...m.atc, action: m.atc.status === "done" ? "cleared" : null, by_role: m.atc.status === "done" ? "ATC" : null, at: null } } : m.flight, latest_route_response: null, pending_reroute: m.reroute?.status === "approved" ? m.reroute : null, ai_ack_version: m.aiAck, adsb: m.adsb, adsb_state: m.adsbState };
    },
    routes(dest, requestId, revision = 0, ids = 0) {
      const main = findPath(m.origin, dest)!;
      const alt = findPath(m.origin, dest, new Set())!;
      const mk = (p: string[], rank: number, t: number, traffic: string, reason: string) => ({
        route_id: UUID(100 + ids * 10 + rank), rank, label: rank === 1 ? "fastest" : "alternative", distance_m: length(p), est_time_s: t, expected_wait_s: 0,
        bunching_risk: 0.1, traffic, cost_terms: { est_time_s: t, expected_wait_s: 0, mu: 0, bunching_risk: 0.1 }, reason,
        eta: p.map((n, k) => ({ node: n, eta_p10: iso(t * (k / (p.length - 1)) - 30), eta_p50: iso(t * (k / (p.length - 1))), eta_p90: iso(t * (k / (p.length - 1)) + 60) })),
        waypoints: geometry(p),
      });
      // Each route's `segments` (always sent, U-API contract §1) carry the AI level of every leg; its `traffic` and `reason`
      // are derived from those legs so the mock never contradicts itself (heavy legs never say "light traffic").
      const LVL: Record<string, number> = { low: 0, moderate: 1, heavy: 2 };
      const nameOf = (id: string) => String(nodes.get(id).properties.name).replace(/\s*\([^)]*\)/g, "").trim();
      const build = (p: string[], rank: number, t: number, base: string) => {
        const sg = p.slice(1).map((n, k) => ({ from: p[k], to: n, eta_s: Math.round(Date.now() / 1000 + t * ((k + 1) / (p.length - 1))), level: m.legLevels[k] ?? "low" }));
        const worst = sg.reduce((w, x) => (LVL[x.level] > LVL[w.level] ? x : w), sg[0]);
        const reason = worst.level === "low" ? `${base}, light traffic` : `Predicted ${worst.level} traffic near ${nameOf(worst.to)}`;
        return { ...mk(p, rank, t, worst.level, reason), segments: sg };
      };
      const routes = [build(main, 1, 900, "Shortest"), build(alt, 2, 1000, "Slightly longer")];
      return { schema_version: "4.0.0", request_id: requestId, aircraft_id: DEVICE, revision, status: "ok", method: "aerolink", forecast_time: iso(0), expires_at: iso(m.expiresIn), forecaster: "cv", graph_version: "central-fl-v1", summary: "Two routes found.", reason: null, routes };
    },
    ...init,
  };
  const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  const err = (route: Route, status: number, error: string) => json(route, { detail: { error, message: error } }, status);

  await page.route(/\/api\/devices\//, async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    let body: any = null;
    try { body = req.postDataJSON(); } catch { /* GET */ }
    m.requests.push({ method: req.method(), path, body });
    if (path.endsWith("/pair")) {
      m.pairCalls.push(body);
      const code = String(body.code).replace(/[^a-z0-9]/gi, "").toUpperCase();
      if (code !== CODE.replace("-", "")) return err(route, 403, "invalid_code");
      m.paired = true;
      return json(route, { device_id: DEVICE, expires_at: iso(7 * 86400) });
    }
    if (!m.paired) return err(route, 401, "session_required");
    if (path.endsWith("/atc-requirement")) return json(route, { requirement: m.atc.requirement, airspaces: m.atc.airspaces, legs: [] });
    if (path.endsWith("/traffic")) return json(route, trafficBody());
    if (req.method() === "GET") { m.stateCalls++; return json(route, m.stateBody()); }
    if (path.endsWith("/ai-ack")) {
      m.aiAcks.push(body.version);
      m.aiAck = body.version;
      return route.fulfill({ status: 204, body: "" });
    }
    if (path.includes("/reroutes/")) {
      const parts = path.split("/"); // /api/devices/<id>/reroutes/<rid>/<answer>
      const rr = m.reroute;
      if (!rr || rr.status !== "approved" || rr.id !== parts[5]) return err(route, 409, "reroute_not_approved");
      const answer = parts[6];
      m.rerouteAnswers.push(answer);
      rr.status = answer === "acknowledge" ? "acknowledged" : "unable";
      rr.responded_at = iso(0);
      if (answer === "acknowledge" && m.flight) { m.flight.route_id = rr.proposed.route_id; m.flight.path = rr.proposed.path; }
      m.reroute = null;
      return json(route, { reroute: rr, flight_status: { aircraft_id: DEVICE, flight_id: m.flight?.flight_id ?? null, status: m.flight?.status ?? "active", reason: null, timestamp: iso(0) } });
    }
    if (!m.online) return err(route, 409, "unit_offline");
    if (path.endsWith("/route-request")) {
      m.dest = body.destination;
      return json(route, m.routes(body.destination, body.request_id));
    }
    if (path.endsWith("/flight/end")) {
      const fid = m.flight?.flight_id ?? null;
      m.flight = null;
      return json(route, { aircraft_id: DEVICE, flight_id: fid, status: "ended", reason: "ended by pilot", timestamp: iso(0) });
    }
    if (path.endsWith("/route-selection")) {
      const fs = { aircraft_id: DEVICE, flight_id: UUID(900), status: "planned", reason: null, timestamp: iso(0) };
      m.flight = { flight_id: UUID(900), status: "planned", route_id: body.route_id, request_id: body.request_id, destination: m.dest, path: findPath(m.origin, m.dest), departure_s: Date.now() / 1000, eta_by_node: { [m.dest]: Date.now() / 1000 + 900 } };
      return json(route, fs);
    }
    return err(route, 404, "unknown_device");
  });

  await page.routeWebSocket(/\/ws\/devices\//, (ws) => {
    if (!m.wsAllowed) { ws.close({ code: 1006 }); return; }
    m.sock = ws;
    ws.send(JSON.stringify({ type: "state", payload: m.stateBody() }));
    const beat = setInterval(() => {
      if (m.heartbeats) ws.send(JSON.stringify({ type: "heartbeat", payload: { t: new Date().toISOString(), online: m.online } }));
    }, 5000);
    ws.onClose(() => { clearInterval(beat); ws.close(); }); // answer the page's own close, as a real server does
  });
  return m;
}

