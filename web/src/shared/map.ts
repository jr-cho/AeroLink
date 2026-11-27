/**
 * Shared MapLibre setup for the web map and the pilot screen (ARCHITECTURE §13.3).
 *
 * - `createMap` builds the basemap from same-origin PMTiles (`/basemap/florida.pmtiles`), glyphs and sprites.
 *   If the PMTiles file is not deployed yet, it falls back to a plain background so overlays still render.
 * - `addAirspaceLayers` draws corridors, sites and no-go volumes from `/api/airspace`.
 * - `AircraftLayer` draws live aircraft with smooth interpolation between 1 s polls.
 * - No third-party tile, font or sprite service is ever contacted.
 */
import * as maplibregl from "maplibre-gl";
import type { GeoJSONSource, Map as MlMap, StyleSpecification } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import { Protocol } from "pmtiles";
import { layers as basemapLayers, namedFlavor } from "@protomaps/basemaps";
import { NOGO_COLOR, ROUTE_COLORS, TRAFFIC_COLOR, VEHICLE_COLOR } from "./colors.ts";
import type { AircraftState, SiteProps, TrafficLevel, VehicleType } from "./types.ts";

export const FOCUS_BOUNDS: [[number, number], [number, number]] = [[-82.8, 27.3], [-81.1, 28.8]];
export const PMTILES_PATH = "/basemap/florida.pmtiles";
const FONT = ["Noto Sans Regular"];
const FONT_BOLD = ["Noto Sans Medium"];

let protocolRegistered = false;

/** True when `/basemap/florida.pmtiles` is really served (not an SPA fallback page or a 404). */
export async function basemapAvailable(): Promise<boolean> {
  try {
    const r = await fetch(PMTILES_PATH, { headers: { Range: "bytes=0-6" } });
    if (!r.ok) return false;
    const head = new TextDecoder().decode((await r.arrayBuffer()).slice(0, 7));
    return head === "PMTiles";
  } catch {
    return false;
  }
}

export function basemapStyle(hasBasemap: boolean): StyleSpecification {
  const origin = location.origin;
  const base: StyleSpecification = {
    version: 8,
    sources: {},
    layers: [{ id: "background", type: "background", paint: { "background-color": "#eef1f5" } }],
  };
  if (!hasBasemap) return base;
  return {
    ...base,
    glyphs: `${origin}/basemap/fonts/{fontstack}/{range}.pbf`,
    sprite: `${origin}/basemap/sprites/v4/light`,
    sources: {
      protomaps: {
        type: "vector",
        url: `pmtiles://${origin}${PMTILES_PATH}`,
        attribution: "© OpenStreetMap contributors, Protomaps",
      },
    },
    // The protomaps layer list already starts with its own "background" layer (a second one is a duplicate-id error
    // that only shows up with the real basemap, not in the mocked tests).
    layers: [
      ...(basemapLayers("protomaps", namedFlavor("light"), { lang: "en" }) as StyleSpecification["layers"]),
    ],
  };
}

export interface BaseMap {
  map: MlMap;
  hasBasemap: boolean;
}

/** Create the map and resolve once the style is loaded (safe to add sources and layers). */
export async function createMap(container: HTMLElement, opts: { center?: [number, number]; zoom?: number; pitch?: number } = {}): Promise<BaseMap> {
  if (!protocolRegistered) {
    maplibregl.setWorkerUrl(workerUrl); // bundled worker chunk, same origin
    maplibregl.addProtocol("pmtiles", new Protocol().tile);
    protocolRegistered = true;
  }
  const hasBasemap = await basemapAvailable();
  const map = new maplibregl.Map({
    container,
    style: basemapStyle(hasBasemap),
    center: opts.center ?? [-81.95, 28.05],
    zoom: opts.zoom ?? 8.2,
    pitch: opts.pitch ?? 0,
    maxPitch: 70,
    attributionControl: false,
    canvasContextAttributes: { antialias: true },
  });
  (window as unknown as { __map?: MlMap }).__map = map; // test hook
  await new Promise<void>((res) => (map.loaded() ? res() : map.once("load", () => res())));
  return { map, hasBasemap };
}

// ---------------------------------------------------------------------------------------------
// Airspace data
// ---------------------------------------------------------------------------------------------

export interface Airspace {
  sites: GeoJSON.FeatureCollection<GeoJSON.Point>;
  corridors: GeoJSON.FeatureCollection<GeoJSON.LineString>;
  nogo: GeoJSON.FeatureCollection<GeoJSON.Polygon | GeoJSON.MultiPolygon>;
  nodes: Map<string, { lon: number; lat: number; props: SiteProps }>;
}

const emptyFC = (): GeoJSON.FeatureCollection => ({ type: "FeatureCollection", features: [] });

/** Sort the `/api/airspace` collection into the three drawable layers. */
export function splitAirspace(fc: GeoJSON.FeatureCollection): Airspace {
  const sites: GeoJSON.Feature<GeoJSON.Point>[] = [];
  const corridors: GeoJSON.Feature<GeoJSON.LineString>[] = [];
  const nogo: GeoJSON.Feature<GeoJSON.Polygon | GeoJSON.MultiPolygon>[] = [];
  const nodes = new Map<string, { lon: number; lat: number; props: SiteProps }>();
  for (const f of fc.features) {
    const p = (f.properties ?? {}) as Record<string, any>;
    if (f.geometry.type === "Point") {
      const types: string[] = p.types ?? [];
      const site = types.includes("evtol") ? "evtol" : types.includes("helipad") ? "helipad" : "waypoint";
      const feat = f as GeoJSON.Feature<GeoJSON.Point>;
      feat.properties = { ...p, site };
      sites.push(feat);
      const [lon, lat] = feat.geometry.coordinates;
      nodes.set(p.id, { lon, lat, props: p as SiteProps });
    } else if (f.geometry.type === "LineString") {
      const feat = f as GeoJSON.Feature<GeoJSON.LineString>;
      feat.properties = {
        ...p,
        coordination: (p.coordination_airspace ?? []).length > 0,
        clearance: (p.clearance_airspace ?? []).length > 0,
        level: "none",
      };
      corridors.push(feat);
    } else if ((f.geometry.type === "Polygon" || f.geometry.type === "MultiPolygon") && p.blocks !== false) {
      nogo.push(f as GeoJSON.Feature<GeoJSON.Polygon | GeoJSON.MultiPolygon>);
    }
  }
  return {
    sites: { type: "FeatureCollection", features: sites },
    corridors: { type: "FeatureCollection", features: corridors },
    nogo: { type: "FeatureCollection", features: nogo },
    nodes,
  };
}

/** Does this site serve the vehicle type? Uses `serves`, else the §3.2 default by site type. */
export function siteServes(props: SiteProps, vt: VehicleType): boolean {
  if (props.types.includes("waypoint") && !props.types.includes("evtol") && !props.types.includes("helipad")) return false;
  if (props.serves) return props.serves.includes(vt);
  return vt === "passenger" ? props.types.includes("evtol") : props.types.includes("helipad");
}

export function setData(map: MlMap, source: string, data: GeoJSON.GeoJSON): void {
  (map.getSource(source) as GeoJSONSource | undefined)?.setData(data);
}

const levelColor: any = ["match", ["get", "level"], "low", TRAFFIC_COLOR.low, "moderate", TRAFFIC_COLOR.moderate, "heavy", TRAFFIC_COLOR.heavy, "#7b8794"];

/** Add the airspace sources and layers. `hasGlyphs` enables text labels. */
export function addAirspaceLayers(map: MlMap, a: Airspace, hasGlyphs: boolean): void {
  map.addSource("nogo", { type: "geojson", data: a.nogo });
  map.addSource("corridors", { type: "geojson", data: a.corridors });
  map.addSource("sites", { type: "geojson", data: a.sites });
  const nogoColor: any = ["match", ["get", "type"], ...Object.entries(NOGO_COLOR).flat(), "#9aa0a6"];
  map.addLayer({
    id: "nogo-extrusion",
    type: "fill-extrusion",
    source: "nogo",
    paint: {
      "fill-extrusion-color": nogoColor,
      "fill-extrusion-base": ["coalesce", ["get", "floor_m"], 0],
      "fill-extrusion-height": ["coalesce", ["get", "ceiling_m"], 1000],
      "fill-extrusion-opacity": 0.28,
    },
  });
  map.addLayer({ id: "nogo-outline", type: "line", source: "nogo", paint: { "line-color": nogoColor, "line-width": 1, "line-opacity": 0.7 } });
  map.addLayer({ id: "corridor-casing", type: "line", source: "corridors", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#ffffff", "line-width": ["interpolate", ["linear"], ["zoom"], 7, 5, 12, 11] } });
  map.addLayer({
    id: "corridors",
    type: "line",
    source: "corridors",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": levelColor, "line-width": ["interpolate", ["linear"], ["zoom"], 7, 3, 12, 7], "line-dasharray": ["case", ["==", ["get", "kind"], "connector"], ["literal", [1, 1.5]], ["literal", [1, 0]]] },
  });
  // Legs that need ATC coordination (Class D) or clearance (Class B): marked with a coloured edge.
  map.addLayer({ id: "corridor-coordination", type: "line", source: "corridors", filter: ["==", ["get", "coordination"], true], layout: { "line-cap": "butt" }, paint: { "line-color": "#e37400", "line-width": 2, "line-offset": ["interpolate", ["linear"], ["zoom"], 7, 3, 12, 6], "line-dasharray": [2, 1.5] } });
  map.addLayer({ id: "corridor-clearance", type: "line", source: "corridors", filter: ["==", ["get", "clearance"], true], layout: { "line-cap": "butt" }, paint: { "line-color": "#a142f4", "line-width": 2, "line-offset": ["interpolate", ["linear"], ["zoom"], 7, -3, 12, -6], "line-dasharray": [2, 1.5] } });
  map.addLayer({
    id: "sites",
    type: "circle",
    source: "sites",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 7, ["match", ["get", "site"], "waypoint", 2, 5], 12, ["match", ["get", "site"], "waypoint", 4, 9]],
      "circle-color": ["match", ["get", "site"], "evtol", "#ffffff", "helipad", "#d93025", "#9aa0a6"],
      "circle-stroke-color": ["match", ["get", "site"], "evtol", "#1a73e8", "helipad", "#ffffff", "#ffffff"],
      "circle-stroke-width": ["match", ["get", "site"], "waypoint", 1, 2.5],
    },
  });
  if (hasGlyphs) {
    map.addLayer({
      id: "site-labels",
      type: "symbol",
      source: "sites",
      minzoom: 8.5,
      filter: ["!=", ["get", "site"], "waypoint"],
      layout: { "text-field": ["get", "name"], "text-font": FONT_BOLD, "text-size": 11, "text-offset": [0, 1.1], "text-anchor": "top", "text-max-width": 9, "text-optional": true },
      paint: { "text-color": "#202124", "text-halo-color": "#ffffff", "text-halo-width": 1.5 },
    });
  }
}

export function setNogoVisible(map: MlMap, visible: boolean): void {
  for (const id of ["nogo-extrusion", "nogo-outline"]) map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
}

/** Colour corridors by traffic level. `levelOf(from, to)` returns undefined for "no data". */
export function colorCorridors(map: MlMap, a: Airspace, levelOf: ((from: string, to: string) => TrafficLevel | undefined) | null): void {
  for (const f of a.corridors.features) {
    const p = f.properties as Record<string, any>;
    p.level = (levelOf && levelOf(p.from, p.to)) || "none";
  }
  setData(map, "corridors", a.corridors);
}

// ---------------------------------------------------------------------------------------------
// Route lines (directions, planned flights)
// ---------------------------------------------------------------------------------------------

/** Add the route-line source/layers once; `setRoutes` feeds it. */
export function addRouteLayers(map: MlMap): void {
  map.addSource("routes", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "routes-casing", type: "line", source: "routes", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#ffffff", "line-width": ["case", ["get", "selected"], 10, 7] } });
  map.addLayer({ id: "routes", type: "line", source: "routes", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": ["get", "color"], "line-width": ["case", ["get", "selected"], 6, 4], "line-opacity": ["case", ["get", "selected"], 1, 0.75], "line-dasharray": ["case", ["get", "dashed"], ["literal", [2, 2]], ["literal", [1, 0]]] } });
}

export interface RouteLine {
  id: string;
  waypoints: [number, number, number][];
  color: string;
  selected?: boolean;
  dashed?: boolean;
}

export function setRoutes(map: MlMap, lines: RouteLine[]): void {
  setData(map, "routes", {
    type: "FeatureCollection",
    features: lines.map((l) => ({
      type: "Feature",
      properties: { id: l.id, color: l.color, selected: !!l.selected, dashed: !!l.dashed },
      geometry: { type: "LineString", coordinates: l.waypoints.map(([lat, lon]) => [lon, lat]) },
    })),
  });
}

export function routeColor(rank: number): string {
  return ROUTE_COLORS[(rank - 1) % ROUTE_COLORS.length];
}

export function fitTo(map: MlMap, points: [number, number][], padding: maplibregl.PaddingOptions | number = 80): void {
  if (!points.length) return;
  const b = new maplibregl.LngLatBounds(points[0], points[0]);
  for (const p of points) b.extend(p);
  map.fitBounds(b, { padding, maxZoom: 11, duration: 700 });
}

// ---------------------------------------------------------------------------------------------
// Live aircraft
// ---------------------------------------------------------------------------------------------

export type AircraftKind = "ok" | "stale" | "lost" | "ground";

/** Fallback only: seconds without a new fix before the client calls an online aircraft stale (the server's `stale` wins). */
export const STALE_AFTER_S = 5;

interface Tracked {
  state: AircraftState;
  from: [number, number];
  to: [number, number];
  t0: number;
  lastFixAt: number;
  lastStamp: string | number;
}

const ANIM_MS = 1000;
const HEADING_M = 700;

/** Live aircraft: coloured by vehicle type; stale (no new fix for 5 s) faded, lost (offline) grey and
 *  hollow, off-route gets a red ring. Positions glide between 1 s polls. */
export class AircraftLayer {
  private tracked = new Map<string, Tracked>();
  private raf = 0;
  private lastDraw = 0;
  private visible = true;

  private map: MlMap;

  constructor(map: MlMap) {
    this.map = map;
    map.addSource("aircraft", { type: "geojson", data: emptyFC() });
    map.addSource("aircraft-heading", { type: "geojson", data: emptyFC() });
    map.addLayer({ id: "aircraft-heading", type: "line", source: "aircraft-heading", paint: { "line-color": ["get", "color"], "line-width": 2.5, "line-opacity": ["match", ["get", "kind"], "ok", 0.9, 0.3] } });
    map.addLayer({ id: "aircraft-ring", type: "circle", source: "aircraft", filter: ["all", ["==", ["get", "off_route"], true], ["!=", ["get", "kind"], "ground"]], paint: { "circle-radius": 14, "circle-color": "#d93025", "circle-opacity": 0.18, "circle-stroke-color": "#d93025", "circle-stroke-width": 2 } });
    map.addLayer({
      id: "aircraft",
      type: "circle",
      source: "aircraft",
      paint: {
        "circle-radius": ["case", ["==", ["get", "kind"], "ground"], 3.5, 7],
        "circle-color": ["case", ["==", ["get", "kind"], "lost"], "#ffffff", ["==", ["get", "kind"], "ground"], "#9aa0a6", ["get", "color"]],
        "circle-opacity": ["match", ["get", "kind"], "ok", 1, "stale", 0.5, "ground", 0.7, 0.9],
        "circle-stroke-color": ["case", ["==", ["get", "kind"], "lost"], "#80868b", "#ffffff"],
        "circle-stroke-width": ["case", ["==", ["get", "kind"], "lost"], 2.5, ["==", ["get", "kind"], "ground"], 1, 2],
        "circle-stroke-opacity": ["match", ["get", "kind"], "stale", 0.6, 1],
      },
    });
    if (map.getStyle().glyphs) {
      map.addLayer({ id: "aircraft-labels", type: "symbol", source: "aircraft", minzoom: 9.5, layout: { "text-field": ["get", "label"], "text-font": FONT, "text-size": 10, "text-offset": [0, -1.4], "text-optional": true }, paint: { "text-color": ["case", ["==", ["get", "kind"], "ground"], "#80868b", "#3c4043"], "text-halo-color": "#fff", "text-halo-width": 1.2 } });
    }
    this.loop();
  }

  /** Feed the latest `/api/state` aircraft. */
  update(list: AircraftState[], now = performance.now()): void {
    const seen = new Set<string>();
    for (const a of list) {
      seen.add(a.id);
      const t = this.tracked.get(a.id);
      const pos: [number, number] = [a.lon, a.lat];
      if (!t) {
        this.tracked.set(a.id, { state: a, from: pos, to: pos, t0: now, lastFixAt: now, lastStamp: a.timestamp });
        continue;
      }
      t.from = this.position(t, now);
      t.to = pos;
      t.t0 = now;
      if (a.timestamp !== t.lastStamp) {
        t.lastStamp = a.timestamp;
        t.lastFixAt = now;
      }
      t.state = a;
    }
    for (const id of [...this.tracked.keys()]) if (!seen.has(id)) this.tracked.delete(id);
  }

  kindOf(t: Tracked, now = performance.now()): AircraftKind {
    if (t.state.on_ground) return "ground"; // parked: neither airborne nor stale/lost, whatever its fix age
    if (!t.state.online) return "lost";
    if (typeof t.state.stale === "boolean") return t.state.stale ? "stale" : "ok"; // the server knows the fix age
    return now - t.lastFixAt > STALE_AFTER_S * 1000 ? "stale" : "ok";
  }

  /** Classification of every aircraft (used by the alert list). */
  summary(): { state: AircraftState; kind: AircraftKind }[] {
    const now = performance.now();
    return [...this.tracked.values()].map((t) => ({ state: t.state, kind: this.kindOf(t, now) }));
  }

  setVisible(v: boolean): void {
    this.visible = v;
    for (const id of ["aircraft", "aircraft-ring", "aircraft-heading", "aircraft-labels"]) if (this.map.getLayer(id)) this.map.setLayoutProperty(id, "visibility", v ? "visible" : "none");
  }

  private position(t: Tracked, now: number): [number, number] {
    const k = Math.min(1, Math.max(0, (now - t.t0) / ANIM_MS));
    return [t.from[0] + (t.to[0] - t.from[0]) * k, t.from[1] + (t.to[1] - t.from[1]) * k];
  }

  private loop = (): void => {
    this.raf = requestAnimationFrame(this.loop);
    const now = performance.now();
    if (!this.visible || now - this.lastDraw < 33) return;
    this.lastDraw = now;
    const pts: GeoJSON.Feature[] = [];
    const heads: GeoJSON.Feature[] = [];
    for (const t of this.tracked.values()) {
      const [lon, lat] = this.position(t, now);
      const a = t.state;
      const kind = this.kindOf(t, now);
      const color = VEHICLE_COLOR[a.vehicle_type] ?? "#5f6368";
      pts.push({ type: "Feature", properties: { id: a.id, label: a.callsign || a.id, kind, color, off_route: a.off_route, vehicle_type: a.vehicle_type }, geometry: { type: "Point", coordinates: [lon, lat] } });
      if (kind === "ground") continue; // no heading arrow for a parked aircraft
      const rad = (a.course_deg * Math.PI) / 180;
      const dLat = (Math.cos(rad) * HEADING_M) / 111_320;
      const dLon = (Math.sin(rad) * HEADING_M) / (111_320 * Math.cos((lat * Math.PI) / 180));
      heads.push({ type: "Feature", properties: { color, kind }, geometry: { type: "LineString", coordinates: [[lon, lat], [lon + dLon, lat + dLat]] } });
    }
    setData(this.map, "aircraft", { type: "FeatureCollection", features: pts });
    setData(this.map, "aircraft-heading", { type: "FeatureCollection", features: heads });
  };

  destroy(): void {
    cancelAnimationFrame(this.raf);
  }
}

/** Constant-velocity prediction cone for an aircraft: a wedge that widens with time. A client-side
 *  estimate, used only when the API does not serve cones itself. */
export function cvCone(a: AircraftState, horizonS = 120): GeoJSON.Feature<GeoJSON.Polygon> {
  const origin = [a.lon, a.lat];
  const dist = Math.max(a.ground_speed_mps, 1) * horizonS;
  const half = 12; // degrees: growth of heading uncertainty
  const pt = (bearing: number, d: number): [number, number] => {
    const r = (bearing * Math.PI) / 180;
    return [a.lon + (Math.sin(r) * d) / (111_320 * Math.cos((a.lat * Math.PI) / 180)), a.lat + (Math.cos(r) * d) / 111_320];
  };
  const ring = [origin as [number, number], pt(a.course_deg - half, dist), pt(a.course_deg, dist * 1.04), pt(a.course_deg + half, dist), origin as [number, number]];
  return { type: "Feature", properties: { id: a.id, vehicle_type: a.vehicle_type }, geometry: { type: "Polygon", coordinates: [ring] } };
}

export function addConeLayer(map: MlMap): void {
  map.addSource("cones", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "cones", type: "fill", source: "cones", layout: { visibility: "none" }, paint: { "fill-color": ["match", ["get", "vehicle_type"], "medical", VEHICLE_COLOR.medical, VEHICLE_COLOR.passenger], "fill-opacity": 0.14 } }, "aircraft-heading");
  map.addLayer({ id: "cones-outline", type: "line", source: "cones", layout: { visibility: "none" }, paint: { "line-color": ["match", ["get", "vehicle_type"], "medical", VEHICLE_COLOR.medical, VEHICLE_COLOR.passenger], "line-width": 1, "line-dasharray": [2, 2], "line-opacity": 0.6 } }, "aircraft-heading");
}

export function addPlannedLayer(map: MlMap): void {
  map.addSource("planned", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "planned", type: "line", source: "planned", layout: { visibility: "none", "line-cap": "round" }, paint: { "line-color": ["match", ["get", "vehicle_type"], "medical", VEHICLE_COLOR.medical, VEHICLE_COLOR.passenger], "line-width": 3, "line-dasharray": [0.5, 2], "line-opacity": 0.9 } }, "aircraft-heading");
}

export function setLayerVisible(map: MlMap, ids: string[], visible: boolean): void {
  for (const id of ids) if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
}
