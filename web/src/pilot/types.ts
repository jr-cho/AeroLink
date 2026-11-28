/** Shapes of the pilot API (backend `service/pilot.py` module docstring). */
import type { RouteResponse, TrafficLevel, VehicleType } from "../shared/types.ts";

export interface Fix {
  lat: number;
  lon: number;
  alt_msl_m: number;
  ground_speed_mps: number;
  course_deg: number;
  timestamp: string;
  fix: { type: string; hdop?: number; satellites?: number; replayed?: boolean };
}

export interface CurrentFlight {
  flight_id: string;
  status: FlightStatusName;
  route_id: string;
  request_id: string;
  destination: string;
  path: string[];
  departure_s: number;
  eta_by_node: Record<string, number>;
  /** Who flagged the off-route state (U-API contract §7): the AI detector or the fixed rule. */
  detected_by?: DetectedBy;
  /** What ATC must be told about this route and whether the controller marked it done (E6.14; informational only). */
  atc?: Atc;
}

export type AtcRequirement = "none" | "contact_tower" | "contact_approach" | "clearance";

export interface Atc {
  requirement: AtcRequirement;
  airspaces: string[];
  status: "not_needed" | "pending" | "done";
  action?: "cleared" | "contacted" | null;
  by_role?: "ATC" | null;
  at?: string | null;
}

export type DetectedBy = "ai" | "rule" | null;

export type FlightStatusName = "planned" | "active" | "off_route" | "replaced" | "ended";

/** An ATC route amendment (U-API contract §2): ATC approves an AI proposal; the pilot acknowledges or is unable. */
export interface Reroute {
  id: string;
  flight_id: string;
  callsign?: string | null;
  destination: string;
  status: "proposed" | "approved" | "acknowledged" | "unable" | "dismissed" | "expired";
  reason: string;
  trigger: "faster" | "conflict" | "medical";
  /** Seconds saved against the current route. */
  gain_s: number;
  current: { path: string[]; waypoints: number[][]; eta_s: number; atc?: { requirement: AtcRequirement } };
  proposed: {
    route_id: string;
    path: string[];
    waypoints: number[][];
    eta_s: number;
    eta_window_s: [number, number];
    segments?: RouteSegmentWire[];
    atc?: { requirement: AtcRequirement };
  };
  /** Set only when the proposal changes the airspace requirement (contract A); null when equal. */
  requirement_change?: { from: AtcRequirement; to: AtcRequirement } | null;
  created_at: string;
  expires_at: string;
  approved_by: string | null;
  approved_at: string | null;
  responded_at: string | null;
}

export interface RouteSegmentWire {
  from: string;
  to: string;
  eta_s: number;
  level: TrafficLevel;
}

/** One ADS-B aircraft near the unit (U-API contract §4). Advisory only; never routed. */
export interface AdsbTarget {
  icao: string;
  callsign: string | null;
  lat: number;
  lon: number;
  alt_ft: number;
  gs_kt: number;
  track_deg: number;
  /** Altitude relative to this unit, feet (+ above). */
  rel_alt_ft: number;
}

export type AdsbState = "ok" | "stale" | "down" | "rate_limited" | "disabled";

export interface DeviceState {
  id: string;
  /** The unit's radio callsign from the registry (null for units registered before callsigns existed). Never editable. */
  callsign?: string | null;
  vehicle_type: VehicleType;
  demo: boolean;
  online: boolean;
  last_seen: string | null;
  latest_fix: Fix | null;
  current_flight: CurrentFlight | null;
  latest_route_response: (RouteResponse & { graph_version?: string }) | null;
  /** The approved ATC reroute awaiting this pilot (U-API contract §2); null otherwise. */
  pending_reroute?: Reroute | null;
  /** The disclaimer version this unit's pilot acknowledged (U-API contract §3); null if never. */
  ai_ack_version?: string | null;
  /** ADS-B aircraft within 10 NM of the unit (U-API contract §4). */
  adsb?: AdsbTarget[];
  /** The ADS-B feed's state (U-API contract §10): anything but "ok" means no aircraft are shown as current. */
  adsb_state?: AdsbState;
}

export interface FlightStatus {
  aircraft_id: string;
  flight_id: string;
  status: FlightStatusName;
  reason: string | null;
  /** Set on off_route: "ai" when the AI detector flagged it, "rule" for the fixed rule (U-API contract §7). */
  detected_by?: DetectedBy;
  timestamp: string;
}

export interface PilotTraffic {
  aircraft_id: string;
  forecast_time: string;
  aircraft: { id: string; vehicle_type: VehicleType; lat: number; lon: number; alt_msl_m: number; course_deg: number; off_route: boolean }[];
  edges: { edge: string; now: TrafficLevel; in_15_min: TrafficLevel; in_30_min: TrafficLevel }[];
  adsb?: AdsbTarget[];
  adsb_state?: AdsbState;
}

export type WsMessage =
  | { type: "state"; payload: DeviceState }
  | { type: "route-response"; payload: RouteResponse & { graph_version?: string } }
  | { type: "flight-status"; payload: FlightStatus }
  | { type: "traffic"; payload: PilotTraffic }
  | { type: "clearance"; payload: Atc & { flight_id: string } }
  | { type: "online" | "offline"; payload: { at: string } }
  | { type: "vehicle-type"; payload: { vehicle_type: VehicleType; demo: boolean } }
  | { type: "reroute"; payload: Reroute }
  | { type: "heartbeat"; payload: { t: string; online: boolean } };
