/** API mocks for the Playwright tests. Fixtures come from the repo's real network, constraints and eval files. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { Page, Route } from "@playwright/test";

const REPO = resolve(import.meta.dirname, "../..");
const read = (p: string) => JSON.parse(readFileSync(resolve(REPO, p), "utf8"));

const network = read("airspace/central-fl-v1.geojson");
const constraints = read("airspace/constraints-fl.geojson");
export const evalRows = read("eval-results/m4-results.json");
/** Shape of `ablations` in `/api/eval` (eval/ablations.py). Values are test data; B1 and A5 are equal on some metrics on purpose. */
const ablations = {
  summary: [
    { config: "B1", runs: 3, trips: 120, trip_time_mean_s: 910, door_to_door_mean_s: 1010, door_to_door_p95_s: 1500, ground_delay_mean_s: 12.5, ground_delay_p95_s: 60, holding_s_per_trip: 0, eta_error_mean_s: 80, eta_error_p90_s: 190, eta_error_cruise_mean_s: 40, eta_coverage: 0.7, suggestions_offered: 0, suggestions_accepted: 0, bunching_events: 4, loss_of_separation: 0, medical_response_mean_s: 700, no_route: 0 },
    { config: "A5", runs: 3, trips: 120, trip_time_mean_s: 910, door_to_door_mean_s: 1010, door_to_door_p95_s: 1520, ground_delay_mean_s: 12.5, ground_delay_p95_s: 60, holding_s_per_trip: 0, eta_error_mean_s: 55, eta_error_p90_s: 140, eta_error_cruise_mean_s: 38, eta_coverage: 0.8, suggestions_offered: 9, suggestions_accepted: 6, bunching_events: 4, loss_of_separation: 0, medical_response_mean_s: 700, no_route: 0 },
  ],
  runs: [
    { scenario: "i4_peak", config: "B1", seed: 1, trips: 40, door_to_door_mean_s: 1100 },
    { scenario: "i4_peak", config: "A5", seed: 1, trips: 40, door_to_door_mean_s: 1100 },
    { scenario: "fdot_high_peak", config: "B1", seed: 1, trips: 80, door_to_door_mean_s: 960 },
    { scenario: "fdot_high_peak", config: "A5", seed: 1, trips: 80, door_to_door_mean_s: 955 },
  ],
};

/** What `/api/airspace` should return: the network plus the blocking no-go volumes of the focus region. */
export const airspace = {
  ...network,
  features: [...network.features, ...constraints.features.filter((f: any) => f.properties.blocks && f.properties.in_focus_region)],
};

export const nodes = new Map<string, any>(network.features.filter((f: any) => f.geometry.type === "Point").map((f: any) => [f.properties.id, f]));
export const lines: any[] = network.features.filter((f: any) => f.geometry.type === "LineString");

function neighbours(n: string, banned: Set<string>) {
  return lines.filter((l) => (l.properties.from === n || l.properties.to === n) && !banned.has(l.properties.id));
}

/** Shortest path (by length) over corridors and connectors; `banned` removes edges to force an alternative. */
export function findPath(a: string, b: string, banned = new Set<string>()): string[] | null {
  const dist = new Map<string, number>([[a, 0]]);
  const prev = new Map<string, string>();
  const todo = new Set([a]);
  while (todo.size) {
    const u = [...todo].sort((x, y) => dist.get(x)! - dist.get(y)!)[0];
    todo.delete(u);
    if (u === b) break;
    for (const l of neighbours(u, banned)) {
      const v = l.properties.from === u ? l.properties.to : l.properties.from;
      const d = dist.get(u)! + l.properties.length_m;
      if (d < (dist.get(v) ?? Infinity)) { dist.set(v, d); prev.set(v, u); todo.add(v); }
    }
  }
  if (!prev.has(b)) return null;
  const path = [b];
  while (path[0] !== a) path.unshift(prev.get(path[0])!);
  return path;
}

export function geometry(path: string[]): [number, number, number][] {
  const out: [number, number, number][] = [];
  for (let i = 0; i + 1 < path.length; i++) {
    const l = lines.find((x) => (x.properties.from === path[i] && x.properties.to === path[i + 1]) || (x.properties.to === path[i] && x.properties.from === path[i + 1]));
    const c: number[][] = l.properties.from === path[i] ? l.geometry.coordinates : [...l.geometry.coordinates].reverse();
    for (const p of out.length ? c.slice(1) : c) out.push([p[1], p[0], 900]);
  }
  return out;
}

export const length = (path: string[]) => path.slice(1).reduce((s, n, i) => s + lines.find((l) => [l.properties.from, l.properties.to].sort().join() === [path[i], n].sort().join()).properties.length_m, 0);

export const NOW = Date.parse("2026-11-25T14:00:00Z");
export const iso = (s: number) => new Date(NOW + s * 1000).toISOString();

export interface MockOptions {
  /** Delay in ms for POST /api/plan (to check the loading and response-time display). */
  planDelay?: number;
  failForecaster?: boolean;
  /** `/api/eval` without the M5 ablation table (the M4 fallback). */
  noAblations?: boolean;
  /** `/api/traffic` without `cones` (an older server). */
  noCones?: boolean;
  /** No operator session: `/api/auth/me` and the three operator PUTs answer 401 (G2). Default: signed in. */
  signedOut?: boolean;
  /** Add AL-11, parked on a pad (`on_ground`, F3), and give every aircraft a callsign. */
  withGround?: boolean;
  /** `/api/adsb` feed state; default "ok" with two aircraft. */
  adsb?: "ok" | "down";
}

export interface MockState {
  method: "classical" | "aerolink";
  forecaster: string;
  requests: string[];
  scenario?: string;
  planCalls: { method: string; body: any }[];
  stateCalls: number;
  noAblations?: boolean;
  withGround?: boolean;
  adsbCalls: number;
}

export const ADSB_ATTRIBUTION = "Aircraft data from adsb.lol, licensed under the Open Database License (ODbL) 1.0. https://adsb.lol";
const ADSB = [
  { icao: "a1b2c3", callsign: "N123AB", category: "A1", lat: 28.45, lon: -81.4, alt_ft: 2500, gs_kt: 110, track_deg: 90, vert_rate_fpm: 0, seen_s: 1, source: "adsb" },
  { icao: "abcdef", callsign: null, category: null, lat: 28.3, lon: -81.6, alt_ft: 4000, gs_kt: 140, track_deg: 200, vert_rate_fpm: 0, seen_s: 2, source: "adsb" },
];

function planResponse(method: "classical" | "aerolink", body: any, forecaster: string) {
  const main = findPath(body.origin_node, body.destination_node)!;
  const alt = findPath(body.origin_node, body.destination_node, new Set(lines.filter((l) => main.includes(l.properties.from) && main.includes(l.properties.to)).filter((l) => l.properties.kind === "corridor").slice(0, 1).map((l) => l.properties.id)));
  const variants = [main, ...(alt && alt.join() !== main.join() ? [alt] : [])];
  // Classical ignores traffic. AeroLink knows the direct (shortest) route is congested and adds a predicted delay to it.
  const JAM_S = 150;
  const scored = variants.map((p, i) => {
    const dist = length(p);
    const jam = method === "aerolink" && i === 0 ? JAM_S : 0;
    return { p, i, dist, jam, t: dist / 38 + jam };
  });
  scored.sort((x, y) => x.t - y.t); // option 1 = lowest cost (time here; no wait or risk terms in the mock)
  const routes = scored.map((s, k) => {
    const direct = s.i === 0;
    const other = scored[1 - k];
    const diff = other ? Math.round(Math.abs(other.t - s.t) / 60) : 0;
    let reason: string;
    if (!other) reason = direct ? "Only route available" : "Only route available";
    else if (k === 0) reason = direct ? (method === "aerolink" ? "Shortest and clear of traffic" : "Shortest path") : `Avoids heavy traffic on the direct route, ${diff} min quicker`;
    else reason = direct ? `Direct but heavy traffic, ${diff} min slower` : `Longer, ${diff} min slower`;
    return {
      route_id: `00000000-0000-4000-8000-00000000000${k + 1}`,
      rank: k + 1,
      label: k === 0 ? "fastest" : "alternative",
      distance_m: s.dist,
      est_time_s: s.t,
      expected_wait_s: 0,
      bunching_risk: s.jam ? 0.4 : 0.1,
      traffic: s.jam ? "heavy" : "low",
      cost_terms: { est_time_s: s.t, expected_wait_s: 0, mu: 0, bunching_risk: s.jam ? 0.4 : 0.1 },
      reason,
      eta: s.p.map((n, j) => ({ node: n, eta_p10: iso(60 * j + s.t - 40), eta_p50: iso(60 * j + s.t), eta_p90: iso(60 * j + s.t + 90) })),
      waypoints: geometry(s.p),
    };
  });
  return {
    schema_version: "4.0.0", message_id: "00000000-0000-4000-8000-0000000000aa", request_id: "00000000-0000-4000-8000-0000000000bb",
    aircraft_id: "web", revision: 0, status: "ok", method, forecast_time: iso(0), expires_at: iso(300), forecaster,
    graph_version: "central-fl-v1", constraints_cycle: "2026-10-29", summary: `${method === "aerolink" ? "AeroLink" : "Classical"} routing: ${routes.length} options, best ${Math.round(routes[0].est_time_s / 60)} min.`, reason: null, routes,
  };
}

const AIRCRAFT = [
  ["ev-orl", "ev-suntrax", "passenger"], ["ev-water-st", "ev-lal", "passenger"], ["ev-parramore", "ev-sef", "passenger"], ["ev-lal", "ev-orl", "passenger"],
  ["ev-suntrax", "ev-water-st", "passenger"], ["hp-61fl", "hp-fd28", "medical"], ["hp-fd28", "hp-7fd7", "medical"], ["ev-sef", "ev-orl", "passenger"],
  ["ev-lal", "ev-suntrax", "passenger"], ["ev-water-st", "ev-parramore", "passenger"],
] as const;

function stateBody(m: MockState) {
  const tick = m.stateCalls++;
  const parked = m.withGround ? [{ id: "AL-11", callsign: "N11PAD", source: "sim", vehicle_type: "passenger", lat: nodes.get("ev-orl").geometry.coordinates[1], lon: nodes.get("ev-orl").geometry.coordinates[0], alt_msl_m: 30, ground_speed_mps: 0, course_deg: 0, timestamp: iso(tick), online: true, off_route: false, flight_status: null, stale: false, last_fix_age_s: 1, on_ground: true }] : [];
  return {
    method: m.method,
    aircraft: [...AIRCRAFT.map(([a, b, vt], i) => {
      const p = nodes.get(a).geometry.coordinates, q = nodes.get(b).geometry.coordinates;
      const k = (0.15 + 0.07 * i + tick * 0.001) % 1;
      const lost = i === 8, stale = i === 7, off = i === 4, far = i === 9;
      return {
        id: `AL-${String(i + 1).padStart(2, "0")}`, source: "sim", vehicle_type: vt,
        lat: p[1] + (q[1] - p[1]) * k + (far ? -3 : 0), lon: p[0] + (q[0] - p[0]) * k,
        alt_msl_m: 900, ground_speed_mps: 40, course_deg: (Math.atan2(q[0] - p[0], q[1] - p[1]) * 180) / Math.PI,
        timestamp: stale ? "2026-11-25T14:00:00Z" : iso(tick), online: !lost, off_route: off, flight_status: "in_flight",
        stale: stale, last_fix_age_s: stale ? 30 : 1,
        ...(m.withGround ? { callsign: `CS-${i + 1}`, on_ground: false } : {}),
      };
    }), ...parked],
  };
}

function trafficBody(call: number, noCones = false) {
  const level = (i: number, t: number) => (i % 5 === 0 && t >= 15 ? "heavy" : i % 3 === 0 && (t >= 15 || call > 0) ? "moderate" : "low");
  const edges: Record<string, any> = {};
  lines.forEach((l, i) => { edges[`${l.properties.from}->${l.properties.to}`] = { now: level(i, 0), in_15_min: level(i, 15), in_30_min: level(i, 30), in_45_min: level(i, 45) }; });
  // server cones: one ribbon per planned flight, as the real endpoint serves them (properties as in service/api.py)
  const ribbon = (path: string[], w: number) => {
    const pts = geometry(path).map(([lat, lon]) => [lon, lat]);
    const left = pts.map(([x, y]) => [x - w, y + w]), right = pts.map(([x, y]) => [x + w, y - w]).reverse();
    return [[...left, ...right, left[0]]];
  };
  const cones = { type: "FeatureCollection", features: [
    { type: "Feature", properties: { flight_id: "f1", aircraft_id: "AL-01", status: "active", vehicle_type: "passenger", horizon_min: 45 }, geometry: { type: "Polygon", coordinates: ribbon(findPath("ev-orl", "ev-suntrax")!, 0.004) } },
    { type: "Feature", properties: { flight_id: "f2", aircraft_id: "AL-06", status: "planned", vehicle_type: "medical", horizon_min: 45 }, geometry: { type: "Polygon", coordinates: ribbon(findPath("hp-61fl", "hp-fd28")!, 0.004) } },
  ] };
  return { forecast_time: iso(0), steps_min: [0, 5, 10, 15, 20, 25, 30, 35, 40, 45], nodes: {}, edges, ...(noCones ? {} : { cones, cone_steps: [] }) };
}

export async function mockApi(page: Page, opts: MockOptions = {}): Promise<MockState> {
  const m: MockState = { method: "aerolink", forecaster: "cv", requests: [], planCalls: [], stateCalls: 0, adsbCalls: 0, noAblations: opts.noAblations, withGround: opts.withGround };
  let trafficCalls = 0;
  const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  await page.route(/\/(api|healthz)(\/|$|\?)/, async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    m.requests.push(`${req.method()} ${url.pathname}`);
    const path = url.pathname;
    if (path === "/api/airspace") return json(route, airspace);
    if (path === "/api/state") return json(route, stateBody(m));
    if (path === "/api/traffic") return json(route, trafficBody(trafficCalls++, opts.noCones));
    if (path === "/api/flights") {
      return json(route, [
        { flight_id: "f1", aircraft_id: "AL-01", vehicle_type: "passenger", status: "in_flight", destination: "ev-suntrax", path: findPath("ev-orl", "ev-suntrax"), departure_s: NOW / 1000, eta_by_node: { "ev-suntrax": NOW / 1000 + 900 }, eta_p10: NOW / 1000 + 840, eta_p50: NOW / 1000 + 960, eta_p90: NOW / 1000 + 1080,
          prediction_history: [
            { t: NOW / 1000, eta_p50: NOW / 1000 + 900, eta_p90: NOW / 1000 + 1000, reason: "initial estimate" },
            { t: NOW / 1000 + 60, eta_p50: NOW / 1000 + 960, eta_p90: NOW / 1000 + 1080, reason: "medical flight AL-06 planned on shared nodes" },
          ] },
        { flight_id: "f2", aircraft_id: "AL-06", vehicle_type: "medical", status: "planned", destination: "hp-fd28", path: findPath("hp-61fl", "hp-fd28"), departure_s: NOW / 1000, eta_by_node: { "hp-fd28": NOW / 1000 + 1200 }, eta_p10: NOW / 1000 + 1150, eta_p50: NOW / 1000 + 1200, eta_p90: NOW / 1000 + 1260,
          prediction_history: [{ t: NOW / 1000, eta_p50: NOW / 1000 + 1200, eta_p90: NOW / 1000 + 1260, reason: "initial estimate" }] },
      ]);
    }
    if (path === "/api/adsb") {
      m.adsbCalls++;
      const ok = (opts.adsb ?? "ok") === "ok";
      return json(route, { attribution: ADSB_ATTRIBUTION, feed: { state: ok ? "ok" : "down", poll_interval_s: 30, budget_remaining: 900, age_s: 4 }, aircraft: ok ? ADSB : [] });
    }
    if (path === "/api/models") return json(route, { method: m.method, forecaster: m.forecaster, models: [] });
    if (path === "/healthz") return json(route, { ok: true, mqtt: true, stats: { flights_planned: 2, route_requests: 14, api_forecaster_set: 0 } });
    if (path === "/api/eval") return json(route, m.noAblations ? { scenarios: evalRows, ablations: null } : { scenarios: evalRows, ablations, m5_ablations: ablations });
    if (path === "/api/scenarios") return json(route, { scenarios: ["nominal_day", "i4_peak", "fdot_high_peak", "orlando_event_egress"], current: m.scenario ?? "nominal_day" });
    if (path === "/api/auth/me") return opts.signedOut ? json(route, { detail: "Not signed in" }, 401) : json(route, { username: "op", name: "Operator", expires_at: iso(3600) });
    if (opts.signedOut && req.method() === "PUT" && ["/api/method", "/api/forecaster", "/api/scenario"].includes(path)) return json(route, { detail: "Operator sign-in required" }, 401);
    if (path === "/api/method" && req.method() === "PUT") { m.method = req.postDataJSON().method; return json(route, { method: m.method }); }
    if (path === "/api/forecaster" && req.method() === "PUT") {
      await new Promise((r) => setTimeout(r, 400));
      if (opts.failForecaster) return json(route, { detail: "forecaster unavailable" }, 422);
      m.forecaster = req.postDataJSON().forecaster;
      return json(route, { forecaster: m.forecaster });
    }
    if (path === "/api/scenario" && req.method() === "PUT") { m.scenario = req.postDataJSON().scenario; return json(route, { scenario: m.scenario }); }
    if (path === "/api/plan") {
      const body = req.postDataJSON();
      m.planCalls.push({ method: m.method, body });
      if (opts.planDelay) await new Promise((r) => setTimeout(r, opts.planDelay));
      return json(route, planResponse(m.method, body, m.forecaster));
    }
    return json(route, { detail: "not mocked" }, 404);
  });
  // No basemap file in tests: make the probe fail the way a missing file does.
  await page.route("**/basemap/**", (route) => route.fulfill({ status: 404, body: "not found" }));
  return m;
}
