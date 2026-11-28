/**
 * Map layers of the ATC dashboard, CarPlay-Maps style: small heading arrows for traffic, the selected flight's route
 * in blue, a red line between two aircraft that conflict, and (only on request) congestion. Colour only with meaning.
 */
import type { Map as MlMap } from "maplibre-gl";
import { setData, type Airspace } from "../shared/map.ts";
import { THEME_COLORS, type Theme } from "../shared/mapStyles/carplay.ts";
import type { AircraftState, TrafficLevel } from "../shared/types.ts";
import type { AdsbAircraft, OpsRoute, Source } from "./types.ts";

const FONT = ["Noto Sans Regular"];
const empty = (): GeoJSON.FeatureCollection => ({ type: "FeatureCollection", features: [] });
const featuresOf = (fc: GeoJSON.Feature[]): GeoJSON.FeatureCollection => ({ type: "FeatureCollection", features: fc });

/** Marker colours per theme: blue for AeroLink traffic, purple for medical, grey for ADS-B. Red is never a marker colour. */
export const MARKER = {
  day: { aircraft: "#0a84ff", medical: "#8b5cf6", adsb: "#8e9aa8", halo: "#ffffff", route: "#0a84ff", warn: "#d98a00", ok: "#2e9e5b", danger: "#d92d20", whatif: "#6b7787" },
  night: { aircraft: "#4da3ff", medical: "#a98bff", adsb: "#7d8a99", halo: "#12181f", route: "#4da3ff", warn: "#f0b24a", ok: "#4cc97e", danger: "#ff6259", whatif: "#a5b0bd" },
} as const;

/** SDF arrow (white on transparent) so `icon-color` tints it; it points up and is rotated to the course. */
function arrow(): ImageData {
  const s = 48;
  const c = document.createElement("canvas");
  c.width = c.height = s;
  const g = c.getContext("2d")!;
  g.fillStyle = "#fff";
  g.beginPath();
  g.moveTo(24, 5); g.lineTo(40, 42); g.lineTo(24, 33); g.lineTo(8, 42); g.closePath();
  g.fill();
  return g.getImageData(0, 0, s, s);
}

/** Colour by severity: medium amber, high red (same hues as the congestion overlay). */
const sevOf = (th: Theme): any => ["match", ["get", "sev"], "medium", CONGESTION[th].moderate, CONGESTION[th].heavy];

/** Congestion hues (UI-AI-PLAN decision 2): moderate orange, heavy red. Dark uses the same hues a shade brighter. */
export const CONGESTION = {
  day: { moderate: "#FF9F0A", heavy: "#FF3B30" },
  night: { moderate: "#FF9F0A", heavy: "#FF453A" },
} as const;

/** Severity of a predicted conflict: high is red, medium amber (the same hues as the congestion colours). */
export type Sev = "high" | "medium";
export interface ConflictMark { at: [number, number]; sev: Sev }

export interface TrafficOpts {
  show: Record<Source, boolean>;
  theme: Theme;
  /** Aircraft id of the selected flight, if any. */
  selectedAircraft: string | null;
  /** Aircraft id -> callsign for the labels. */
  names: (id: string) => string;
}

export interface OpsLayers {
  setTraffic(state: AircraftState[], adsb: AdsbAircraft[], opts: TrafficOpts): void;
  /** The selected flight's route and the legs of it that need ATC (null clears). */
  setRoute(route: OpsRoute | null, coords: [number, number][], atcLegs: [number, number][][]): void;
  setCone(fc: GeoJSON.Feature | null): void;
  /** Predicted congestion: one line per corridor that is moderate or heavy at the chosen minute (plain corridors otherwise). */
  setCongestion(features: GeoJSON.Feature[]): void;
  /** Conflicts at rest are rings and outlines; the connecting line is only passed for a selected or hovered conflict. */
  /** Proposed AI reroutes, drawn dashed in the accent blue beside the solid current route. */
  setReroute(current: [number, number][][], proposed: [number, number][][]): void;
  setConflicts(c: { rings: ConflictMark[]; aircraft: ConflictMark[]; lines: { coords: [number, number][]; sev: Sev }[] }): void;
  setSiteLoad(sites: { node: string; levels: TrafficLevel[]; pads: number }[], step: number, airspace: Airspace): void;
  setPredicted(points: { id: string; at: [number, number] }[]): void;
  setTrack(segments: [number, number][][]): void;
  setWhatIf(lines: [number, number][][]): void;
  paint(theme: Theme): void;
}

export function addOpsLayers(map: MlMap, theme: Theme): OpsLayers {
  map.addImage("ops-arrow", arrow(), { sdf: true });
  const glyphs = !!map.getStyle().glyphs;
  for (const id of ["cone", "route", "route-atc", "whatif", "track", "predicted", "load", "congestion", "reroute", "reroute-current", "conflict-lines", "conflict-rings", "conflict-aircraft", "traffic"]) map.addSource(`ops-${id}`, { type: "geojson", data: empty() });

  const heat = (low: string, mid: string, hi: string): any => ["match", ["get", "level"], "heavy", hi, "moderate", mid, low];
  const M = MARKER[theme];
  const SCALE: any = ["case", ["get", "sel"], 1.35, ["==", ["get", "src"], "adsb"], 0.75, 1];
  // predicted congestion (AI): saturated Apple-Maps hues, drawn about 1.6x the corridor width so they read from across the room
  map.addLayer({ id: "ops-congestion", type: "line", source: "ops-congestion", layout: { "line-cap": "round", "line-join": "round" }, paint: {
    "line-color": ["match", ["get", "level"], "heavy", CONGESTION[theme].heavy, CONGESTION[theme].moderate],
    "line-width": ["interpolate", ["linear"], ["zoom"], 7, 5, 12, 11],
    "line-opacity": 1,
  } });
  map.addLayer({ id: "ops-load", type: "circle", source: "ops-load", paint: {
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 7, ["*", ["get", "r"], 1.2], 12, ["*", ["get", "r"], 2.6]],
    "circle-color": heat("#2e9e5b", "#e8a317", "#d92d20"),
    "circle-opacity": ["match", ["get", "level"], "low", 0.0, 0.2],
    "circle-stroke-color": heat("#2e9e5b", "#e8a317", "#d92d20"),
    "circle-stroke-width": ["match", ["get", "level"], "low", 0, 1.5],
  } });
  map.addLayer({ id: "ops-cone", type: "fill", source: "ops-cone", paint: { "fill-color": M.route, "fill-opacity": 0.1 } });
  map.addLayer({ id: "ops-track-line", type: "line", source: "ops-track", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": M.whatif, "line-width": 3, "line-opacity": 0.55 } });
  map.addLayer({ id: "ops-route-casing", type: "line", source: "ops-route", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": M.halo, "line-width": 10 } });
  map.addLayer({ id: "ops-route", type: "line", source: "ops-route", layout: { "line-cap": "round", "line-join": "round" }, paint: {
    "line-color": M.route, "line-width": 6, "line-dasharray": ["case", ["==", ["get", "status"], "planned"], ["literal", [2, 1.6]], ["literal", [1, 0]]],
  } });
  // the legs that need ATC: a thin amber overlay while to do, green once marked
  map.addLayer({ id: "ops-route-atc", type: "line", source: "ops-route-atc", layout: { "line-cap": "butt", "line-join": "round" }, paint: { "line-color": ["case", ["get", "done"], M.ok, M.warn], "line-width": 3, "line-dasharray": [1.2, 1.2] } });
  // the current route of a card's flight stays solid; the proposed one is dashed beside it
  map.addLayer({ id: "ops-reroute-current", type: "line", source: "ops-reroute-current", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": M.route, "line-width": 5, "line-opacity": 0.55 } });
  map.addLayer({ id: "ops-reroute", type: "line", source: "ops-reroute", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": M.route, "line-width": 4, "line-dasharray": [2, 1.6] } });
  map.addLayer({ id: "ops-whatif-line", type: "line", source: "ops-whatif", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": M.whatif, "line-width": 5, "line-dasharray": [1.5, 1.5], "line-opacity": 0.95 } });
  map.addLayer({ id: "ops-predicted", type: "circle", source: "ops-predicted", paint: { "circle-radius": 5, "circle-color": M.halo, "circle-stroke-color": M.route, "circle-stroke-width": 2 } });
  // a conflict is drawn as one red line between the two aircraft, nothing else
  const sevColor = sevOf(theme);
  map.addLayer({ id: "ops-conflict-lines", type: "line", source: "ops-conflict-lines", layout: { "line-cap": "round" }, paint: { "line-color": sevColor, "line-width": 2.5, "line-dasharray": [2, 1.5] } });
  map.addLayer({ id: "ops-conflict-aircraft", type: "circle", source: "ops-conflict-aircraft", paint: { "circle-radius": 11, "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": sevColor, "circle-stroke-width": 1.5 } });
  map.addLayer({ id: "ops-conflict-rings", type: "circle", source: "ops-conflict-rings", paint: { "circle-radius": 8, "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": sevColor, "circle-stroke-width": 2 } });
  map.addLayer({ id: "ops-selected-ring", type: "circle", source: "ops-traffic", filter: ["==", ["get", "sel"], true], paint: { "circle-radius": 17, "circle-color": M.route, "circle-opacity": 0.14, "circle-stroke-color": M.route, "circle-stroke-width": 2 } });
  map.addLayer({ id: "ops-ground", type: "circle", source: "ops-traffic", filter: ["==", ["get", "ground"], true], paint: { "circle-radius": 3.5, "circle-color": ["get", "col"], "circle-opacity": 0.5, "circle-stroke-color": M.halo, "circle-stroke-width": 1 } });
  map.addLayer({ id: "ops-traffic", type: "symbol", source: "ops-traffic", filter: ["!=", ["get", "ground"], true], layout: {
    "icon-image": "ops-arrow",
    "icon-size": ["interpolate", ["linear"], ["zoom"], 7, ["*", 0.42, SCALE], 12, ["*", 0.62, SCALE]],
    "icon-rotate": ["get", "course"],
    "icon-rotation-alignment": "map",
    "icon-allow-overlap": true,
    "icon-ignore-placement": true,
  }, paint: { "icon-color": ["get", "col"], "icon-halo-color": M.halo, "icon-halo-width": 2, "icon-opacity": ["case", ["get", "faded"], 0.55, 1] } });
  if (glyphs) {
    map.addLayer({ id: "ops-traffic-labels", type: "symbol", source: "ops-traffic", minzoom: 9, filter: ["all", ["!=", ["get", "src"], "adsb"], ["!=", ["get", "ground"], true]], layout: {
      "text-field": ["get", "label"], "text-font": FONT, "text-size": 12, "text-offset": [0, 1.5], "text-anchor": "top", "text-optional": true,
    }, paint: { "text-color": THEME_COLORS[theme].label, "text-halo-color": THEME_COLORS[theme].halo, "text-halo-width": 1.5 } });
  }

  return {
    setTraffic(state, adsb, { show, theme: th, selectedAircraft, names }) {
      const C = MARKER[th];
      const feats: GeoJSON.Feature[] = [];
      const add = (id: string, src: Source, vt: string, label: string, ground: boolean, faded: boolean, course: number, lon: number, lat: number) => {
        const col = src === "adsb" ? C.adsb : vt === "medical" ? C.medical : C.aircraft;
        feats.push({ type: "Feature", properties: { id, src, vt, label, col, ground, faded, course, sel: id === selectedAircraft }, geometry: { type: "Point", coordinates: [lon, lat] } });
      };
      for (const a of state) {
        const src: Source = a.source === "sim" ? "sim" : "aircraft";
        if (!show[src]) continue;
        add(a.id, src, a.vehicle_type, names(a.id), (a as { on_ground?: boolean }).on_ground === true, !a.online || !!a.stale, a.course_deg, a.lon, a.lat);
      }
      if (show.adsb) {
        for (const a of adsb) add(a.icao, "adsb", "passenger", a.callsign || a.icao, (a as { on_ground?: boolean }).on_ground === true, a.seen_s > 30, a.track_deg ?? 0, a.lon, a.lat);
      }
      setData(map, "ops-traffic", featuresOf(feats));
    },
    setRoute(route, coords, atcLegs) {
      setData(map, "ops-route", route ? featuresOf([{ type: "Feature", properties: { status: route.status }, geometry: { type: "LineString", coordinates: coords } }]) : empty());
      setData(map, "ops-route-atc", featuresOf(route ? atcLegs.filter((l) => l.length > 1).map((l) => ({ type: "Feature", properties: { done: route.atc.status === "done" }, geometry: { type: "LineString", coordinates: l } })) : []));
    },
    setCone(f) {
      setData(map, "ops-cone", featuresOf(f ? [f] : []));
    },
    setCongestion(features) {
      setData(map, "ops-congestion", featuresOf(features));
    },
    setReroute(current, proposed) {
      const line = (l: [number, number][]) => ({ type: "Feature" as const, properties: {}, geometry: { type: "LineString" as const, coordinates: l } });
      setData(map, "ops-reroute-current", featuresOf(current.map(line)));
      setData(map, "ops-reroute", featuresOf(proposed.map(line)));
    },
    setConflicts({ rings, aircraft, lines }) {
      const pts = (ps: ConflictMark[]) => featuresOf(ps.map((p) => ({ type: "Feature", properties: { sev: p.sev }, geometry: { type: "Point", coordinates: p.at } })));
      setData(map, "ops-conflict-rings", pts(rings));
      setData(map, "ops-conflict-aircraft", pts(aircraft));
      setData(map, "ops-conflict-lines", featuresOf(lines.map((l) => ({ type: "Feature", properties: { sev: l.sev }, geometry: { type: "LineString", coordinates: l.coords } }))));
    },
    setSiteLoad(sites, step, a) {
      const feats: GeoJSON.Feature[] = [];
      for (const s of sites) {
        const n = a.nodes.get(s.node);
        const level = s.levels[Math.min(step, s.levels.length - 1)];
        if (!n || !level) continue;
        feats.push({ type: "Feature", properties: { id: s.node, level, r: 10 + Math.min(s.pads, 6) * 2 }, geometry: { type: "Point", coordinates: [n.lon, n.lat] } });
      }
      setData(map, "ops-load", featuresOf(feats));
    },
    setPredicted(points) {
      setData(map, "ops-predicted", featuresOf(points.map((p) => ({ type: "Feature", properties: { id: p.id }, geometry: { type: "Point", coordinates: p.at } }))));
    },
    setTrack(segments) {
      setData(map, "ops-track", featuresOf(segments.filter((s) => s.length > 1).map((s, n) => ({ type: "Feature", properties: { n }, geometry: { type: "LineString", coordinates: s } }))));
    },
    setWhatIf(lines) {
      setData(map, "ops-whatif", featuresOf(lines.map((l) => ({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: l } }))));
    },
    paint(th) {
      const C = MARKER[th];
      const set = (id: string, prop: string, v: unknown) => { if (map.getLayer(id)) map.setPaintProperty(id, prop as never, v as never); };
      set("ops-cone", "fill-color", C.route);
      set("ops-track-line", "line-color", C.whatif);
      set("ops-route-casing", "line-color", C.halo);
      set("ops-route", "line-color", C.route);
      set("ops-route-atc", "line-color", ["case", ["get", "done"], C.ok, C.warn]);
      set("ops-whatif-line", "line-color", C.whatif);
      set("ops-reroute", "line-color", C.route);
      set("ops-reroute-current", "line-color", C.route);
      set("ops-predicted", "circle-color", C.halo);
      set("ops-predicted", "circle-stroke-color", C.route);
      set("ops-conflict-lines", "line-color", sevOf(th));
      set("ops-conflict-rings", "circle-stroke-color", sevOf(th));
      set("ops-conflict-aircraft", "circle-stroke-color", sevOf(th));
      set("ops-congestion", "line-color", ["match", ["get", "level"], "heavy", CONGESTION[th].heavy, CONGESTION[th].moderate]);
      set("ops-selected-ring", "circle-color", C.route);
      set("ops-selected-ring", "circle-stroke-color", C.route);
      set("ops-ground", "circle-stroke-color", C.halo);
      set("ops-traffic", "icon-halo-color", C.halo);
      set("ops-traffic-labels", "text-color", THEME_COLORS[th].label);
      set("ops-traffic-labels", "text-halo-color", THEME_COLORS[th].halo);
    },
  };
}
