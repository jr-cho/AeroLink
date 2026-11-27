/** Colours shared by the web map and the pilot screen. */
import type { TrafficLevel, VehicleType } from "./types.ts";

export const VEHICLE_COLOR: Record<VehicleType, string> = { passenger: "#1a73e8", medical: "#d93025" };
export const VEHICLE_LABEL: Record<VehicleType, string> = { passenger: "Passenger eVTOL", medical: "Medical" };

export const TRAFFIC_COLOR: Record<TrafficLevel, string> = { low: "#34a853", moderate: "#f9ab00", heavy: "#d93025" };
export const TRAFFIC_LABEL: Record<TrafficLevel, string> = { low: "Light", moderate: "Moderate", heavy: "Heavy" };

/** One colour per route option (rank 1..3), distinct from the traffic colours' meaning. */
export const ROUTE_COLORS = ["#1a73e8", "#8430ce", "#e8710a"];

/** No-go volume colours by airspace type. */
export const NOGO_COLOR: Record<string, string> = {
  class_b: "#1a73e8",
  class_c: "#a142f4",
  class_d: "#12b5cb",
  restricted: "#d93025",
  prohibited: "#a50e0e",
  moa: "#e37400",
  warning: "#f9ab00",
  alert: "#f538a0",
};
export const NOGO_LABEL: Record<string, string> = {
  class_b: "Class B",
  class_c: "Class C",
  class_d: "Class D",
  restricted: "Restricted",
  prohibited: "Prohibited",
  moa: "Military ops area",
  warning: "Warning area",
  alert: "Alert area",
};
