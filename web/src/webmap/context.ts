/** Shared state of the web map page: the map, the loaded airspace and the latest polled data. */
import type { Map as MlMap } from "maplibre-gl";
import type { AdsbLayer } from "./adsb.ts";
import type { AircraftLayer, Airspace } from "../shared/map.ts";
import type { FlightRow, HealthResponse, Method, StateResponse, TrafficEntry, TrafficResponse } from "../shared/types.ts";

export interface Ctx {
  map: MlMap;
  hasBasemap: boolean;
  airspace: Airspace;
  aircraft: AircraftLayer;
  state: StateResponse | null;
  traffic: TrafficResponse | null;
  prevTraffic: TrafficResponse | null;
  flights: FlightRow[];
  health: HealthResponse | null;
  method: Method;
  forecaster: string;
  online: boolean;
  /** Operator session: `false` disables the controls that change the server (method, forecaster, scenario). */
  operator: boolean;
  listeners: Set<() => void>;
  /** Live ADS-B background layer; its feed state feeds the layers panel note. */
  adsb: { layer: AdsbLayer; ok: boolean };
}

/** Tell every panel that the data changed. */
export function notify(ctx: Ctx): void {
  for (const fn of ctx.listeners) fn();
}

type LevelKey = "now" | "in_15_min" | "in_30_min" | "in_45_min";
const KEYS: [number, LevelKey][] = [[0, "now"], [15, "in_15_min"], [30, "in_30_min"], [45, "in_45_min"]];

/** The forecast entry nearest to `minutes` ahead. `exact` is false when the API has no horizon that far. */
export function levelAt(entry: TrafficEntry | undefined, minutes: number): { level: TrafficEntry["now"] | undefined; exact: boolean } {
  if (!entry) return { level: undefined, exact: true };
  if (entry.levels?.length) return { level: entry.levels[Math.min(entry.levels.length - 1, Math.round(minutes / 5))], exact: true };
  let best: [number, LevelKey] | undefined;
  for (const k of KEYS) if (entry[k[1]] && (!best || Math.abs(k[0] - minutes) < Math.abs(best[0] - minutes))) best = k;
  if (!best) return { level: undefined, exact: true };
  return { level: entry[best[1]], exact: Math.abs(best[0] - minutes) <= 7.5 };
}
