/** Live ADS-B background traffic on the web map: quiet grey markers with a heading tick, never routed, never alerted on. */
import type { Map as MlMap } from "maplibre-gl";
import { setData } from "../shared/map.ts";

export interface AdsbAircraft { icao: string; callsign: string | null; lat: number; lon: number; track_deg: number | null; seen_s: number }
export interface AdsbResponse { attribution: string; feed: { state: string }; aircraft: AdsbAircraft[] }

/** The backend polls faster while a viewer is active; never ask more often than this. */
export const ADSB_POLL_MS = 10_000;
const SRC = "adsb-traffic";
const LAYERS = ["adsb-icons", "adsb-labels"];
const COLOR = "#8e9aa8";

/** SDF marker (white on transparent): a small dot with a tick pointing up; `icon-rotate` turns it to the track. */
function marker(): ImageData {
  const s = 32;
  const c = document.createElement("canvas");
  c.width = c.height = s;
  const g = c.getContext("2d")!;
  g.fillStyle = "#fff";
  g.strokeStyle = "#fff";
  g.lineWidth = 3;
  g.lineCap = "round";
  g.beginPath(); g.arc(16, 19, 5, 0, Math.PI * 2); g.fill();
  g.beginPath(); g.moveTo(16, 14); g.lineTo(16, 3); g.stroke();
  return g.getImageData(0, 0, s, s);
}

export class AdsbLayer {
  private map: MlMap;
  private visible = true;
  constructor(map: MlMap) {
    this.map = map;
    map.addImage("adsb-marker", marker(), { sdf: true });
    map.addSource(SRC, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({ id: "adsb-icons", type: "symbol", source: SRC, layout: {
      "icon-image": "adsb-marker", "icon-size": 0.6, "icon-rotate": ["get", "course"], "icon-rotation-alignment": "map", "icon-allow-overlap": true, "icon-ignore-placement": true,
    }, paint: { "icon-color": COLOR, "icon-opacity": ["case", ["get", "faded"], 0.4, 0.75] } });
    if (map.getStyle().glyphs) {
      map.addLayer({ id: "adsb-labels", type: "symbol", source: SRC, minzoom: 10, layout: {
        "text-field": ["get", "label"], "text-font": ["Noto Sans Regular"], "text-size": 10, "text-offset": [0, 1.2], "text-anchor": "top", "text-optional": true,
      }, paint: { "text-color": "#80868b", "text-halo-color": "#fff", "text-halo-width": 1.2 } });
    }
  }
  /** Draw `aircraft`, or nothing for `null` (feed not ok). */
  update(aircraft: AdsbAircraft[] | null): void {
    const features: GeoJSON.Feature[] = (aircraft ?? []).map((a) => ({
      type: "Feature", properties: { label: a.callsign?.trim() || a.icao, course: a.track_deg ?? 0, faded: a.seen_s > 30 }, geometry: { type: "Point", coordinates: [a.lon, a.lat] },
    }));
    setData(this.map, SRC, { type: "FeatureCollection", features });
  }
  setVisible(on: boolean): void {
    this.visible = on;
    for (const id of LAYERS) if (this.map.getLayer(id)) this.map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
  }
  get shown(): boolean { return this.visible; }
}
