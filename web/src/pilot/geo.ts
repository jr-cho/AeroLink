/** Small geometry helpers for the pilot screen. */
const M_PER_DEG = 111_320;

export interface LL {
  lat: number;
  lon: number;
}

export function distanceM(a: LL, b: LL): number {
  const dLat = (b.lat - a.lat) * M_PER_DEG;
  const dLon = (b.lon - a.lon) * M_PER_DEG * Math.cos(((a.lat + b.lat) / 2 * Math.PI) / 180);
  return Math.hypot(dLat, dLon);
}

function segmentDistanceM(p: LL, a: LL, b: LL): number {
  const k = Math.cos((p.lat * Math.PI) / 180);
  const ax = (a.lon - p.lon) * k, ay = a.lat - p.lat, bx = (b.lon - p.lon) * k, by = b.lat - p.lat;
  const dx = bx - ax, dy = by - ay;
  const len2 = dx * dx + dy * dy;
  const t = len2 === 0 ? 0 : Math.max(0, Math.min(1, -(ax * dx + ay * dy) / len2));
  return Math.hypot(ax + t * dx, ay + t * dy) * M_PER_DEG;
}

/** Index in `path` of the next node to reach: the end of the leg the unit is nearest to. */
export function nextNodeIndex(pos: LL, path: LL[]): number {
  if (path.length < 2) return 0;
  let best = 0;
  let bestD = Infinity;
  for (let i = 0; i + 1 < path.length; i++) {
    const d = segmentDistanceM(pos, path[i], path[i + 1]);
    if (d < bestD) { bestD = d; best = i; }
  }
  return best + 1;
}

/** Distances on the pilot screen in nautical miles (aviation standard), via the shared formatter. */
export { fmtNm as fmtKm } from "../shared/format.ts";

/** Index of the waypoint nearest to `pt` (route waypoints are [lat, lon, ...]). Used to cut a route into its legs. */
export function nearestWaypoint(wps: number[][], pt: LL): number {
  let best = 0;
  let bestD = Infinity;
  wps.forEach((w, i) => {
    const d = distanceM(pt, { lat: w[0], lon: w[1] });
    if (d < bestD) { bestD = d; best = i; }
  });
  return best;
}

/** Initial compass bearing from `a` to `b`, degrees clockwise from north. */
export function bearingDeg(a: LL, b: LL): number {
  const dy = b.lat - a.lat;
  const dx = (b.lon - a.lon) * Math.cos(((a.lat + b.lat) / 2 * Math.PI) / 180);
  return ((Math.atan2(dx, dy) * 180) / Math.PI + 360) % 360;
}

/** Remaining length of `path` from `pos` through the next node `nextIdx`. */
export function remainingM(pos: LL, path: LL[], nextIdx: number): number {
  if (!path.length) return 0;
  let m = distanceM(pos, path[Math.min(nextIdx, path.length - 1)]);
  for (let i = nextIdx; i + 1 < path.length; i++) m += distanceM(path[i], path[i + 1]);
  return m;
}

/** "Downtown Tampa (I-4 / I-275)" -> "Downtown Tampa": the glance name; the full name stays in details and titles. */
export function shortenName(name: string): string {
  const s = name.replace(/\s*\([^)]*\)/g, "").replace(/\s+/g, " ").trim();
  return s || name;
}

type Ring = number[][];

function inRing(lon: number, lat: number, ring: Ring): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j];
    if (yi > lat !== yj > lat && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function inPolygon(lon: number, lat: number, rings: Ring[]): boolean {
  return inRing(lon, lat, rings[0]) && !rings.slice(1).some((h) => inRing(lon, lat, h));
}

/** Does the route (waypoints are [lat, lon, ...]) pass through this polygon? Samples each leg about every 300 m. */
export function routeCrosses(waypoints: number[][], geom: { type: string; coordinates: unknown }): boolean {
  const polys = (geom.type === "Polygon" ? [geom.coordinates] : geom.type === "MultiPolygon" ? geom.coordinates : []) as Ring[][];
  if (!polys.length) return false;
  for (let i = 0; i < waypoints.length; i++) {
    const a = waypoints[i], b = waypoints[i + 1] ?? a;
    const n = Math.max(1, Math.ceil(distanceM({ lat: a[0], lon: a[1] }, { lat: b[0], lon: b[1] }) / 300));
    for (let k = 0; k < n || (k === 0 && n === 1); k++) {
      const t = n === 1 ? 0 : k / n;
      const lat = a[0] + (b[0] - a[0]) * t, lon = a[1] + (b[1] - a[1]) * t;
      if (polys.some((p) => inPolygon(lon, lat, p))) return true;
      if (n === 1) break;
    }
  }
  return false;
}
