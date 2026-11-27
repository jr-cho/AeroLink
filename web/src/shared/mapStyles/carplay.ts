/**
 * Pilot map theme: a calm, muted day/night basemap in the style of a modern car-navigation maps app.
 *
 * Own palette on the open Protomaps layer set: no third-party tiles, icons or fonts. The theme is applied by
 * swapping the basemap layers *under* the airspace layers (so a day/night change keeps routes and aircraft).
 * Minimal on purpose: no POI icons, no building footprints, no minor road names: the pilot sees land, water,
 * the main roads and a few place names, and our own route on top.
 */
import { layers as basemapLayers, namedFlavor } from "@protomaps/basemaps";
import type { Map as MlMap, LayerSpecification } from "maplibre-gl";

export type Theme = "day" | "night";

/** Colours the pilot UI and the map overlays share per theme. */
export const THEME_COLORS = {
  day: { route: "#0a84ff", routeAlt: "#8e9aa8", casing: "#ffffff", label: "#2a3441", halo: "#ffffff", corridor: "#9aa7b5" },
  night: { route: "#4da3ff", routeAlt: "#6b7787", casing: "#0b1220", label: "#c9d3df", halo: "#0b1220", corridor: "#5f6c7b" },
} as const;

const FLAVOR_OVERRIDES = {
  day: {
    background: "#eef0f2", earth: "#eceee9", park_a: "#dfe9d6", park_b: "#dfe9d6", wood_a: "#dfe9d6", wood_b: "#dfe9d6",
    scrub_a: "#e4eadc", scrub_b: "#e4eadc", water: "#bcd8ee", sand: "#efece3", beach: "#efece3", buildings: "#e6e7e5",
    hospital: "#eceee9", industrial: "#eceee9", school: "#eceee9", pedestrian: "#eceee9", zoo: "#eceee9", military: "#eceee9",
    aerodrome: "#e6e8ea", runway: "#d9dde1",
    highway: "#ffffff", major: "#ffffff", minor_a: "#f8f8f6", minor_b: "#f8f8f6", minor_service: "#f8f8f6", other: "#f8f8f6", link: "#ffffff",
    highway_casing_early: "#d8dbdd", highway_casing_late: "#d8dbdd", major_casing_early: "#dfe1e2", major_casing_late: "#dfe1e2",
    minor_casing: "#e6e8e8", link_casing: "#dfe1e2", minor_service_casing: "#e6e8e8",
    roads_label_major: "#6b7682", roads_label_major_halo: "#ffffff", roads_label_minor: "#8a939c", roads_label_minor_halo: "#ffffff",
    city_label: "#2a3441", city_label_halo: "#ffffff", subplace_label: "#6b7682", subplace_label_halo: "#ffffff",
    state_label: "#8a939c", state_label_halo: "#ffffff", ocean_label: "#6b95b8", boundaries: "#c9ced3", railway: "#d4d8db",
  },
  night: {
    background: "#12181f", earth: "#161d25", park_a: "#18262a", park_b: "#18262a", wood_a: "#18262a", wood_b: "#18262a",
    scrub_a: "#18232a", scrub_b: "#18232a", water: "#0e2a44", sand: "#1a2028", beach: "#1a2028", buildings: "#1a222b",
    hospital: "#161d25", industrial: "#161d25", school: "#161d25", pedestrian: "#161d25", zoo: "#161d25", military: "#161d25",
    aerodrome: "#1b232c", runway: "#232c36",
    highway: "#3a4553", major: "#303a47", minor_a: "#262f3a", minor_b: "#262f3a", minor_service: "#222a34", other: "#222a34", link: "#303a47",
    highway_casing_early: "#161d25", highway_casing_late: "#161d25", major_casing_early: "#161d25", major_casing_late: "#161d25",
    minor_casing: "#161d25", link_casing: "#161d25", minor_service_casing: "#161d25",
    roads_label_major: "#7f8c9b", roads_label_major_halo: "#12181f", roads_label_minor: "#66717e", roads_label_minor_halo: "#12181f",
    city_label: "#c9d3df", city_label_halo: "#12181f", subplace_label: "#8a96a4", subplace_label_halo: "#12181f",
    state_label: "#66717e", state_label_halo: "#12181f", ocean_label: "#4a7aa3", boundaries: "#2a3440", railway: "#242d38",
  },
} as const;

/** Layers we leave out: points of interest, buildings and anything that is noise at cockpit glance distance. */
const SKIP_SOURCE_LAYERS = new Set(["pois", "buildings", "physical_point"]);
const SKIP_IDS = /^(roads_labels_minor|roads_oneway|roads_shields|address)/;

export function carplayLayers(theme: Theme, hasGlyphs: boolean): LayerSpecification[] {
  const flavor = { ...namedFlavor(theme === "night" ? "dark" : "light"), ...FLAVOR_OVERRIDES[theme] };
  const all = basemapLayers("protomaps", flavor, { lang: "en" }) as LayerSpecification[];
  return all.filter((l) => {
    const sl = (l as { "source-layer"?: string })["source-layer"];
    if (sl && SKIP_SOURCE_LAYERS.has(sl)) return false;
    if (SKIP_IDS.test(l.id)) return false;
    return hasGlyphs || l.type !== "symbol";
  });
}

const OURS_FIRST = ["nogo-extrusion", "nogo-outline", "corridor-casing", "corridors", "sites", "routes-casing"];

/**
 * Put the theme's basemap layers under the airspace layers. Call after the airspace layers exist; safe to call
 * again for a day/night change. Without a basemap (file not deployed) only the background colour changes.
 */
export function applyCarplayTheme(map: MlMap, theme: Theme, hasBasemap: boolean): void {
  const flavorBg = FLAVOR_OVERRIDES[theme].background;
  if (!hasBasemap) {
    if (map.getLayer("background")) map.setPaintProperty("background", "background-color", flavorBg);
    return;
  }
  for (const l of map.getStyle().layers) {
    if ((l as { source?: string }).source === "protomaps" || l.id === "background") {
      // our own layers never use the protomaps source; the style's first background is replaced below
      map.removeLayer(l.id);
    }
  }
  const before = OURS_FIRST.find((id) => map.getLayer(id));
  for (const l of carplayLayers(theme, true)) map.addLayer(l, before);
}

/** Muted look for the airspace overlay layers the shared setup adds (corridors, sites, labels, no-go). */
export function styleOverlays(map: MlMap, theme: Theme): void {
  const c = THEME_COLORS[theme];
  const set = (id: string, prop: string, v: unknown) => { if (map.getLayer(id)) map.setPaintProperty(id, prop as never, v as never); };
  const hide = (id: string) => { if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", "none"); };
  hide("nogo-extrusion");
  hide("corridor-coordination");
  hide("corridor-clearance");
  hide("nogo-outline"); // airspace outlines only show where the selected route crosses them (pilot app)
  hide("sites"); // the pilot app draws its own neutral site markers
  set("corridor-casing", "line-color", c.casing);
  set("corridor-casing", "line-opacity", 0.6);
  set("corridors", "line-opacity", 0.55);
  set("routes-casing", "line-color", c.casing);
  set("site-labels", "text-color", c.label);
  set("site-labels", "text-halo-color", c.halo);
  set("sites", "circle-color", ["match", ["get", "site"], "evtol", c.casing, "helipad", "#d93025", c.corridor]);
  set("sites", "circle-stroke-color", ["match", ["get", "site"], "evtol", c.route, "helipad", c.casing, c.casing]);
}
