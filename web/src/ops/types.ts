/** Types for the ATC dashboard's endpoints (`backend/src/aerolink/service/ops.py`). */
import type { TrafficLevel, VehicleType } from "../shared/types.ts";

export type Source = "aircraft" | "sim" | "adsb";
export const SOURCE_LABEL: Record<Source, string> = { aircraft: "AeroLink unit", sim: "Simulated", adsb: "ADS-B (live)" };
export const SOURCE_SHORT: Record<Source, string> = { aircraft: "Unit", sim: "Sim", adsb: "ADS-B" };

export interface RouteNode {
  node: string;
  name: string;
  eta_p10: number;
  eta_p50: number;
  eta_p90: number;
}

export interface OpsPosition {
  lat: number;
  lon: number;
  alt_msl_m: number;
  ground_speed_mps: number;
  course_deg: number;
  age_s: number;
  online: boolean;
  off_route: boolean;
  stale: boolean;
}

export type AtcRequirement = "none" | "contact_tower" | "contact_approach" | "clearance";

/** What a route needs from ATC and whether the controller marked it handled (`clearance.py`; informational only). */
export interface Atc {
  requirement: AtcRequirement;
  airspaces: string[];
  /** The legs of the route that cross the airspace, as node-id pairs. */
  legs: [string, string][];
  status: "not_needed" | "pending" | "done";
  action: "cleared" | "contacted" | null;
  by: string | null;
  by_name: string | null;
  initials: string | null;
  at: string | null;
}

export interface OpsRoute {
  flight_id: string;
  aircraft_id: string;
  /** Radio callsign (registry callsign, SIM-P03 for sim-p03, else the id). */
  callsign?: string;
  source: Source;
  vehicle_type: VehicleType;
  status: string;
  /** Who flagged an off-route status (contract §7): the learned detector ("ai") or the fixed rule. */
  detected_by?: "ai" | "rule";
  medical: boolean;
  path: string[];
  path_names: string[];
  origin: string;
  destination: string;
  departure_s: number;
  nodes: RouteNode[];
  waypoints: [number, number, number][];
  eta_p10: number | null;
  eta_p50: number | null;
  eta_p90: number | null;
  prediction_history: { t: number; eta_p50: number; eta_p90: number; reason: string }[];
  cone: { min: number; p10: [number, number]; p50: [number, number]; p90: [number, number] }[];
  bunching_risk: number | null;
  position: OpsPosition | null;
  atc: Atc;
}

export interface FlightRef {
  flight_id: string;
  aircraft_id: string;
  vehicle_type: VehicleType;
  source: Source;
}

export interface Conflict {
  id: string;
  kind: "junction" | "pad" | "opposing" | "adsb";
  severity: "high" | "medium";
  medical: boolean;
  t: number;
  time_to_conflict_s: number;
  node: string | null;
  edge: [string, string] | null;
  overlap_s?: number;
  gap_s?: number;
  min_distance_m?: number;
  lat?: number;
  lon?: number;
  flights: FlightRef[];
  bunching_risk: number | null;
  adsb?: { icao: string; callsign: string | null; category: string | null; alt_ft: number | null; gs_kt: number | null; track_deg: number | null };
  locations: { kind: string; node?: string; edge?: [string, string]; t: number; severity: string }[];
}

export interface ConflictsResponse {
  generated_at: string;
  horizon_min: number;
  conflicts: Conflict[];
  groups: { node: string; t: number; time_to_conflict_s: number; count: number; medical: boolean; flights: FlightRef[] }[];
  bunching: { flight_id: string; aircraft_id: string; risk: number }[];
  node_names: Record<string, string>;
  adsb_state: string;
}

export interface WhatIfOption {
  path: string[];
  via: string;
  names: string[];
  est_time_s: number;
  wait_s: number;
  total_s: number;
  time_saved_s: number | null;
  bunching_risk: number;
  traffic: TrafficLevel;
  reason: string;
}

export interface WhatIf {
  flight_id: string;
  aircraft_id: string;
  status: string;
  from_node: string;
  from_name: string;
  destination: string;
  method: string;
  forecaster: string;
  current: { path: string[]; via: string; eta_p50: number; total_s: number } | null;
  alternatives: WhatIfOption[];
}

export interface SiteLoad {
  node: string;
  name: string;
  pads: number;
  now: TrafficLevel;
  levels: TrafficLevel[];
  peak: TrafficLevel;
  peak_in_min: number;
  arrivals_45: number;
  departures_45: number;
}

export interface Congestion {
  steps_min: number[];
  sites: SiteLoad[];
  edges: { edge: [string, string]; now: TrafficLevel; levels: TrafficLevel[]; peak: TrafficLevel }[];
}

export interface AlertRow {
  id: number;
  at: string;
  kind: string;
  severity: "info" | "warn" | "critical";
  aircraft_id: string | null;
  message: string;
  detail: Record<string, unknown>;
  acked_at: string | null;
  acked_by: string | null;
}

export interface Watchdog {
  name: string;
  state: "ok" | "warn" | "fail";
  value: unknown;
  threshold: string;
  detail: string;
  since?: string;
  checked_at?: string;
}

export interface DeviceRow {
  id: string;
  /** Registry callsign; null for units registered before callsigns existed. */
  callsign?: string | null;
  vehicle_type: VehicleType;
  demo?: boolean;
  online: boolean;
  last_seen?: string | null;
  status: "healthy" | "degraded" | "offline";
  status_reasons: string[];
  health: {
    uptime_s: number;
    reset_reason: string;
    wifi_rssi_dbm: number;
    gnss: { fix: string; satellites: number; hdop: number };
    free_heap_bytes: number;
    firmware_version: string;
  } | null;
  health_age_s: number | null;
  age_s: number | null;
  stale: boolean;
}

export interface AdsbAircraft {
  icao: string;
  callsign: string | null;
  category: string | null;
  lat: number;
  lon: number;
  alt_ft: number | null;
  gs_kt: number | null;
  track_deg: number | null;
  vert_rate_fpm?: number | null;
  seen_s: number;
  source: "adsb";
}

export interface AdsbResponse {
  attribution: string;
  feed: {
    state: "ok" | "stale" | "down" | "disabled" | "rate_limited";
    last_ok_at: string | null;
    error: string | null;
    kept: number;
    dropped: number;
    poll_interval_s?: number;
    budget_remaining?: number;
    age_s?: number;
  };
  aircraft: AdsbAircraft[];
}

export interface DeepHealth {
  ok: boolean;
  mqtt: boolean;
  state: string;
  watchdogs?: Watchdog[];
}

export interface TelemetryRow {
  timestamp: string;
  lat: number;
  lon: number;
  segment_id?: number;
  [k: string]: unknown;
}

/** An AI reroute proposal (U-API §2): the AI proposes, ATC approves, the pilot acknowledges or is unable. */
export type RerouteStatus = "proposed" | "approved" | "acknowledged" | "unable" | "dismissed" | "expired";
/** `GET /api/ai` (contract §3): the forecaster in use, the four models, the measured results and the disclaimer. */
export interface AiInfo {
  forecaster: { name: string; version: string; updated_at: string; forecast_age_s: number };
  /** `title` is the human label for screens; `name` is the technical id (contract §6). */
  models: { key: "forecaster" | "demand" | "offroute" | "summary"; title: string; name: string; what: string; drives: string }[];
  results: { eta_error_ai_s: number; eta_error_classical_s: number; window_coverage: number; offroute_precision: number; offroute_recall: number; route_choice_note: string };
  /** Two texts since 2026-11-27 (L-FIX): `ops` for this screen; `text` is the pilot text kept for compatibility. */
  disclaimer: { version: string; text: string; pilot?: string; ops?: string };
}

/** One structured fact behind the AI overview (R-contract §1). The overview only phrases these; they never leave the server. */
export type OverviewFact =
  | { kind: "busy_site"; node: string; name: string; level: "moderate" | "heavy"; from_s: number; to_s: number; arrivals: number; pads: number }
  | { kind: "heavy_corridor"; edge: string; name: string; from_s: number; to_s: number; flights: string[] }
  | { kind: "bunch"; node: string; name: string; flight_ids: string[]; callsigns: string[]; from_s: number; to_s: number; within_min: number; pads: number }
  | { kind: "reroute"; reroute_id: string; callsign: string; gain_s: number; status: string }
  /** R-contract §11.1: ONE clearance fact for all pending clearances. */
  | { kind: "clearance"; flight_ids: string[]; callsigns: string[]; count: number; earliest_due_s: number; overdue_count: number; medical: boolean }
  | { kind: "quiet" };
/** `GET /api/ops/flights/{id}/overview` (R-contract §2): at most two sentences for one flight's popover. */
export interface FlightOverview {
  flight_id: string;
  callsign: string;
  source: "llm" | "template";
  text: string;
  generated_at: string;
}
/** `GET /api/ops/overview` (R-contract §1): 1–4 plain lines for the next 30 min, written from the facts above. */
export interface OverviewResponse {
  generated_at: string;
  horizon_min: number;
  /** "template" and "llm" look the same on screen: no "fallback" alarm. */
  source: "llm" | "template";
  bullets: string[];
  facts: OverviewFact[];
  bunches: Extract<OverviewFact, { kind: "bunch" }>[];
  check: { rejected: boolean };
}
export interface RerouteEvent {
  at: string;
  kind: "proposed" | "approved" | "dismissed" | "acknowledged" | "unable" | "expired";
  by: string | null;
}
export interface Reroute {
  id: string;
  flight_id: string;
  aircraft_id: string;
  callsign: string;
  /** "sim" flights have no pilot screen: the proposal is shown, but ATC cannot send it (contract §6). */
  source: "aircraft" | "sim";
  destination: string;
  status: RerouteStatus;
  reason: string;
  trigger: "faster" | "conflict" | "medical";
  gain_s: number;
  current: { path: string[]; waypoints: [number, number, number][]; eta_s: number; atc?: { requirement: string } };
  proposed: { route_id: string; path: string[]; waypoints: [number, number, number][]; eta_s: number; eta_window_s: [number, number]; segments: { from: string; to: string; eta_s: number; level: TrafficLevel }[]; atc: { requirement: string } };
  created_at: string;
  /** Contract A: the airspace requirement before and after the swap; null when they are equal. */
  requirement_change?: { from: string; to: string } | null;
  /** Contract B: true when a simulated pilot answers (source "sim"). */
  simulated?: boolean;
  expires_at: string;
  approved_by: string | null;
  approved_at: string | null;
  responded_at: string | null;
  events: RerouteEvent[];
}
