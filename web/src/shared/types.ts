/** Types for the backend HTTP API (ARCHITECTURE §11.1, schemas in `contracts/schemas/`). */

export type VehicleType = "passenger" | "medical";
export type TrafficLevel = "low" | "moderate" | "heavy";
export type Method = "classical" | "aerolink";
export type Position3 = [lat: number, lon: number, alt_msl_m: number];

export interface AircraftState {
  id: string;
  /** Radio callsign: a unit's registered one, SIM-P03 for the simulated sim-p03, else the id (G2). */
  callsign?: string;
  /** On a pad / stand, not flying (F3). Older servers omit it. */
  on_ground?: boolean;
  source?: string;
  vehicle_type: VehicleType;
  lat: number;
  lon: number;
  alt_msl_m: number;
  ground_speed_mps: number;
  course_deg: number;
  /** Time of the last fix (ISO string or epoch seconds). */
  timestamp: string | number;
  online: boolean;
  off_route: boolean;
  flight_status: string | null;
  /** Server verdict: no fix for more than 5 s. Older servers omit it (the client then infers it). */
  stale?: boolean;
  last_fix_age_s?: number;
}

export interface StateResponse {
  aircraft: AircraftState[];
  method: Method;
}

export interface EtaPoint {
  node: string;
  eta_p10: string;
  eta_p50: string;
  eta_p90: string;
}

/** One leg of a route with the AI's predicted level when the aircraft reaches it (U-API contract §1). */
export interface RouteSegment {
  from: string;
  to: string;
  /** Epoch seconds at which the aircraft reaches `to` (p50). */
  eta_s: number;
  level: TrafficLevel;
}

export interface RouteOption {
  route_id: string;
  /** Per-leg predicted level at the aircraft's ETA over that leg (U-API contract §1); absent on older servers. */
  segments?: RouteSegment[];
  rank: number;
  label: "fastest" | "alternative";
  distance_m: number;
  est_time_s: number;
  expected_wait_s: number;
  bunching_risk: number;
  traffic: TrafficLevel;
  cost_terms: { est_time_s: number; expected_wait_s: number; mu: number; bunching_risk: number };
  reason: string;
  eta: EtaPoint[];
  waypoints: Position3[];
}

export interface RouteResponse {
  request_id: string;
  aircraft_id: string;
  revision: number;
  status: "ok" | "no_route" | "error";
  method: Method;
  forecast_time: string;
  expires_at: string;
  forecaster: string;
  summary: string | null;
  reason: string | null;
  routes: RouteOption[];
}

export interface PlanRequest {
  vehicle_type: VehicleType;
  destination_node: string;
  origin_node?: string;
}

export interface TrafficEntry {
  /** One level per `steps_min` entry (0, 5, ... 45 min). */
  levels?: TrafficLevel[];
  now: TrafficLevel;
  in_15_min?: TrafficLevel;
  in_30_min?: TrafficLevel;
  in_45_min?: TrafficLevel;
  /** Predicted level per minute offset (U-API contract §1): "0", "15", "30", "45". Absent on older servers. */
  by_minute?: Partial<Record<"0" | "15" | "30" | "45", TrafficLevel>>;
}

export interface TrafficResponse {
  forecast_time: string;
  nodes: Record<string, TrafficEntry>;
  /** Keyed `"from->to"`. */
  edges: Record<string, TrafficEntry>;
  steps_min?: number[];
  /** Server prediction cones: one ribbon per planned flight along its route, as wide as the p10-p90 spread. */
  cones?: GeoJSON.FeatureCollection;
}

export interface FlightRow {
  flight_id: string;
  aircraft_id: string;
  vehicle_type: VehicleType;
  status: string;
  destination: string;
  path: string[];
  departure_s: number;
  /** Node → ETA (epoch seconds). */
  eta_by_node: Record<string, number>;
  /** Predicted arrival at the destination, waits included (epoch seconds). */
  eta_p10?: number;
  eta_p50?: number;
  eta_p90?: number;
  /** Why this flight's prediction moved, oldest first (max 20). */
  prediction_history?: PredictionChange[];
  [k: string]: unknown;
}

export interface PredictionChange {
  t: number;
  eta_p50: number;
  eta_p90: number;
  reason: string;
}

export interface ModelsResponse {
  method: Method;
  forecaster: string;
  default_method?: Method;
  default_forecaster?: string;
  models?: unknown;
}

/** One row of `eval-results/m4-results.json` (one scenario x configuration run). */
export interface EvalRow {
  scenario: string;
  config: string;
  seed?: number;
  trips: number;
  completed_trips: number;
  no_route_count?: number;
  total_trip_time_s: number;
  total_pad_wait_s?: number;
  bunching_events?: number;
  loss_of_separation_events?: number;
  off_route_events?: number;
  medical_dispatches?: number;
  medical_wait_s?: number;
  eta_error_sum_s?: number;
  eta_predictions?: number;
  [k: string]: unknown;
}

/** One configuration's row of `/api/eval` -> `ablations.summary` (all scenarios pooled); a metric may be absent. */
export interface AblationSummary {
  config: string;
  runs?: number;
  trips?: number;
  trip_time_mean_s?: number | null;
  door_to_door_mean_s?: number | null;
  door_to_door_p95_s?: number | null;
  ground_delay_mean_s?: number | null;
  ground_delay_p95_s?: number | null;
  holding_s_per_trip?: number | null;
  eta_error_mean_s?: number | null;
  eta_error_p90_s?: number | null;
  eta_error_cruise_mean_s?: number | null;
  eta_coverage?: number | null;
  suggestions_offered?: number | null;
  suggestions_accepted?: number | null;
  bunching_events?: number | null;
  loss_of_separation?: number | null;
  medical_response_mean_s?: number | null;
  no_route?: number | null;
  [k: string]: unknown;
}

/** One scenario x configuration run of the ablation study. */
export interface AblationRun extends AblationSummary {
  scenario: string;
  seed?: number;
}

export interface EvalResponse {
  scenarios?: EvalRow[];
  ablations?: { summary: AblationSummary[]; runs?: AblationRun[] } | null;
}

export interface ScenariosResponse {
  scenarios: string[];
  current: string | null;
}

export interface HealthResponse {
  ok: boolean;
  mqtt: boolean;
  stats: Record<string, number>;
}

/** A node (landing site or waypoint) of the corridor network, as served in `/api/airspace`. */
export interface SiteProps {
  id: string;
  kind: "node";
  name: string;
  types: ("evtol" | "helipad" | "waypoint")[];
  pads?: number;
  serves?: VehicleType[];
}

/** `GET /api/ai` (U-API contract §3): the AI's identity, the models in one line each, measured results, and the disclaimer. */
export interface AiInfo {
  forecaster: { name: string; version: string; updated_at: string; forecast_age_s: number };
  /** `title` is the plain-words name shown first (U-API contract §6); `name` is the model's own name, shown small. */
  models: { key: "forecaster" | "demand" | "offroute" | "summary"; title?: string; name: string; what: string; drives: string }[];
  results: {
    eta_error_ai_s: number;
    eta_error_classical_s: number;
    window_coverage: number;
    offroute_precision: number;
    offroute_recall: number;
    route_choice_note: string;
  };
  /** `pilot` and `ops` are the two drafted texts (L-FIX); `text` is kept as the pilot text for older clients. */
  disclaimer: { version: string; text: string; pilot?: string; ops?: string };
}
