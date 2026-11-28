/** Small helpers for the ATC dashboard. */
import type { Airspace } from "../shared/map.ts";
import { fmtClock } from "../shared/format.ts";
import { levelAt } from "../webmap/context.ts";
import type { TrafficLevel } from "../shared/types.ts";
import type { Conflict, OpsRoute } from "./types.ts";

/** [lon, lat] points along a node path, following the corridor geometry in `/api/airspace`. */
export function pathCoords(a: Airspace, path: string[]): [number, number][] {
  const out: [number, number][] = [];
  for (let i = 0; i + 1 < path.length; i++) {
    const [u, v] = [path[i], path[i + 1]];
    const f = a.corridors.features.find((x) => {
      const p = x.properties as Record<string, any>;
      return (p.from === u && p.to === v) || (p.from === v && p.to === u);
    });
    let c = (f?.geometry.coordinates ?? []) as [number, number][];
    if (f && (f.properties as Record<string, any>).from !== u) c = [...c].reverse();
    if (!c.length) {
      const [p, q] = [a.nodes.get(u), a.nodes.get(v)];
      c = p && q ? [[p.lon, p.lat], [q.lon, q.lat]] : [];
    }
    for (const pt of out.length ? c.slice(1) : c) out.push(pt);
  }
  return out;
}

/** Route polyline: the server's waypoints (lat, lon, alt) as [lon, lat]. */
export const routeCoords = (r: OpsRoute): [number, number][] => r.waypoints.map(([lat, lon]) => [lon, lat]);

export function nodeName(a: Airspace, id: string): string {
  return (a.nodes.get(id)?.props.name as string | undefined) ?? id;
}

/** Where to show a conflict on the map ([lon, lat]). */
export function conflictPoint(c: Conflict, a: Airspace): [number, number] | null {
  if (c.lat !== undefined && c.lon !== undefined) return [c.lon, c.lat];
  if (c.node) {
    const n = a.nodes.get(c.node);
    return n ? [n.lon, n.lat] : null;
  }
  if (c.edge) {
    const [p, q] = [a.nodes.get(c.edge[0]), a.nodes.get(c.edge[1])];
    return p && q ? [(p.lon + q.lon) / 2, (p.lat + q.lat) / 2] : null;
  }
  return null;
}

/** "in 4 min" / "in 35 s" / "now" for a time-to-conflict. */
export function inText(s: number): string {
  if (s < 10) return "now";
  if (s < 90) return `in ${Math.round(s)} s`;
  return `in ${Math.round(s / 60)} min`;
}

export const etaWindow = (r: { eta_p10: number | null; eta_p50: number | null; eta_p90: number | null }): string =>
  r.eta_p50 === null ? "none" : `${fmtClock(r.eta_p50)} (${fmtClock(r.eta_p10 ?? r.eta_p50)} to ${fmtClock(r.eta_p90 ?? r.eta_p50)})`;

export function ago(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "never";
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  return `${(seconds / 3600).toFixed(1)} h ago`;
}

export function uptime(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

/** Callsigns by aircraft id (from /api/state, /api/ops/routes, /api/devices). ATC speaks callsigns; ids stay the keys. */
const callsigns = new Map<string, string>();
export function learnCallsigns(rows: { id?: string; aircraft_id?: string; callsign?: string | null }[]): void {
  for (const r of rows) {
    const id = r.id ?? r.aircraft_id;
    if (id && r.callsign) callsigns.set(id, r.callsign);
  }
}
/** What the controller sees for an aircraft id: its callsign, else the id. */
export const call = (id: string): string => callsigns.get(id) ?? id;

/** Order-stable JSON, to skip re-rendering a panel when a poll brought nothing new. */
export const sig = (x: unknown): string => JSON.stringify(x);

export const M_TO_FT = 3.28084;
export const MPS_TO_KT = 1.94384;

/** The AI's level for one edge or node at a minute ahead: the minute-keyed `by_minute` (U-API §1) when the server sent it, else the 5-minute `levels` list. */
export function predictedLevel(entry: unknown, minutes: number): TrafficLevel | undefined {
  const byMinute = (entry as { by_minute?: Record<string, TrafficLevel> } | undefined)?.by_minute;
  return byMinute?.[String(minutes)] ?? levelAt(entry as Parameters<typeof levelAt>[0], minutes).level;
}

/** The one small AI mark (UI-AI-PLAN C): used on predictions and on anything the model produced, nowhere else. */
export { aiMark } from "../shared/icon.ts";

/** Route polyline from the server's waypoints (lat, lon, alt) as [lon, lat]. */
export const wpCoords = (wps: [number, number, number][]): [number, number][] => wps.map(([lat, lon]) => [lon, lat]);
