/** The paired pilot screen: map, Where to?, route options, navigation and status. */
import type { GeoJSONSource, Map as MlMap } from "maplibre-gl";
import { api } from "../shared/api.ts";
import { TRAFFIC_COLOR, TRAFFIC_LABEL, VEHICLE_LABEL } from "../shared/colors.ts";
import { clear, h } from "../shared/dom.ts";
import { fmtClock, fmtDuration } from "../shared/format.ts";
import { routeBadges, predictedTime } from "../shared/labels.ts";
import { THEME_COLORS, applyCarplayTheme, styleOverlays, type Theme } from "../shared/mapStyles/carplay.ts";
import {
  AircraftLayer, FOCUS_BOUNDS, addAirspaceLayers, addRouteLayers, colorCorridors, createMap, fitTo, setData, setRoutes, siteServes, splitAirspace,
  type Airspace, type RouteLine,
} from "../shared/map.ts";
import type { AircraftState, RouteOption, RouteResponse, TrafficLevel, VehicleType } from "../shared/types.ts";
import { bearingDeg, distanceM, fmtKm, nearestWaypoint, nextNodeIndex, remainingM, routeCrosses, shortenName, type LL } from "./geo.ts";
import { icon } from "./icons.ts";
import { ChevronRight } from "lucide";
import { aiMark, icon as lucideIcon, Sparkles } from "../shared/icon.ts";
import { PilotError, PilotSocket, pilotApi, uuid, type LinkState } from "./net.ts";
import type { AdsbState, AdsbTarget, Atc, AtcRequirement, CurrentFlight, DeviceState, Fix, FlightStatus, PilotTraffic, Reroute, WsMessage } from "./types.ts";
import type { AiInfo } from "../shared/types.ts";

type Resp = RouteResponse & { graph_version?: string };
type Ui = "home" | "options";
const THEME_KEY = "aerolink.pilot.theme";
/** Traffic further than this from the unit is not drawn: nearby traffic only. */
const NEARBY_M = 20_000;
const LEVEL_RANK: Record<TrafficLevel, number> = { low: 0, moderate: 1, heavy: 2 };
const ACTIVE = new Set(["planned", "active", "off_route"]);

export interface Controller {
  destroy(): void;
}

class Pilot {
  private id: string;
  private root: HTMLElement;
  private onSessionEnd: () => void;
  private map!: MlMap;
  private airspace!: Airspace;
  private hasBasemap = false;
  private theme: Theme = autoTheme();
  private themeChoice: Theme | "auto" = "auto";
  private headingUp = false;
  private expanded = false;
  private details = false;
  private aircraft!: AircraftLayer;
  private socket: PilotSocket;
  private link: LinkState = "down";
  private timers: number[] = [];
  private destroyed = false;

  // device state
  private vt: VehicleType = "passenger";
  private demo = false;
  private online = true;
  private lastSeen: string | null = null;
  private callsign: string | null = null;
  private fix: Fix | null = null;
  private flight: CurrentFlight | null = null;
  private flightSince = 0;
  private traffic: PilotTraffic | null = null;
  private latestResponse: Resp | null = null;
  private responses = new Map<string, Resp[]>();

  // ui state
  private ui: Ui = "home";
  private destination: string | null = null;
  private options: Resp | null = null;
  private loading = false;
  private message = "";
  private selectedRank = 1;
  private query = "";
  private follow = true;
  private navRoute: RouteOption | null = null;
  /** ATC requirement of each offered route (by route id), fetched once; informational only. */
  private routeAtc = new Map<string, Atc>();
  private routeAtcAsked = new Set<string>();
  private dismissed = new Set<string>();
  /** Flights the pilot ended: a poll that was already on its way must not bring them back. */
  private endedFlights = new Set<string>();
  /** Set (ms) by the first tap on End; a second tap within 4 s confirms. */
  private endArmedAt = 0;
  private sitesKey = "";
  private areaKey = "";
  private stale = false;
  private showStatus = false;
  private wakeLock: { release(): Promise<void> } | null = null;
  private tickN = 0;
  /** When the socket last told us online/offline or a type change: older HTTP answers must not undo it. */
  private pushedAt = 0;
  /** When the socket last pushed a flight-status: a poll asked before that must not overwrite the pushed status. */
  private flightPushedAt = 0;
  /** The map and data credit (ODbL, OSM, Protomaps), compact on the map and repeated in the status sheet. */
  private credit(id?: string): HTMLElement {
    return h("div", { class: "map-credit", id }, "Aircraft data: ", h("a", { href: "https://adsb.lol", target: "_blank", rel: "noopener" }, "adsb.lol"), " (ODbL). Map data from ",
      h("a", { href: "https://www.openstreetmap.org/copyright", target: "_blank", rel: "noopener" }, "OpenStreetMap"), " and ", h("a", { href: "https://protomaps.com", target: "_blank", rel: "noopener" }, "Protomaps"));
  }

  /** The standing notice (L-FIX): small, muted, never over a control. One element per screen, so the id is passed in. */
  private protoNote(id: string): HTMLElement {
    return h("p", { class: "proto-note", id }, "Prototype. Not certified. Not for flight. Not ATC.");
  }

  /** The notice and, under it, the map credit: both sit in the panel's flow, so neither hides behind a card or a bar. */
  private footer(): HTMLElement {
    return h("div", { class: "pilot-foot" }, this.protoNote("proto-note"), this.credit("map-credit"));
  }

  /** ADS-B aircraft near the unit: advisory only, drawn small; the relative-altitude tag only when close. */
  private adsb: AdsbTarget[] = [];
  /** When the traffic message last carried ADS-B: an older state answer must not replace it. */
  private adsbAt = 0;
  /** The ADS-B feed's state (U-API contract §10). */
  private adsbState: AdsbState = "ok";
  /** The AI identity and disclaimer (`/api/ai`), and the disclaimer version this unit's pilot has acknowledged. */
  private ai: AiInfo | null = null;
  private aiAck: string | null = null;
  /** The ATC reroute the server holds for this unit (approved; the pilot answers). */
  private reroute: Reroute | null = null;
  /** The reroute the map currently shows (its id), and whether its reason is expanded to full text. */
  private rrShown: string | null = null;
  /** A pointer is down on the sheet: rows are not swapped under it (a tap would land on a detached row). */
  private sheetHeld = false;
  private sheetDirty = false;
  /** What the home list shows when it was last built; while it is unchanged only the distances are patched in place. */
  private homeKey = "";

  // dom
  private sheet = h("section", { class: "sheet", id: "sheet", "aria-live": "polite" });
  private offline = h("div", { class: "offline", id: "offline", role: "alert", hidden: true }, h("strong", {}, "Unit not connected"), h("span", {}, "Routing is locked until the unit is back online."));
  private panelCol = h("div", { class: "panel-col" }, this.offline, this.sheet);
  private statusPanel = h("section", { class: "status-panel", id: "status-panel", hidden: true });
  private dot = h("button", { class: "status-dot", id: "status-btn", type: "button", "aria-label": "Unit status", onclick: () => { this.showStatus = !this.showStatus; this.statusPanel.hidden = !this.showStatus; if (this.showStatus) this.renderStatus(); } });
  private recenter = h("button", { class: "fab", id: "recenter", type: "button", "aria-label": "Recenter on my aircraft", hidden: true, onclick: () => this.recenterNow() }, icon("locate", 26));

  constructor(root: HTMLElement, id: string, onSessionEnd: () => void) {
    this.root = root;
    this.id = id;
    this.onSessionEnd = onSessionEnd;
    this.socket = new PilotSocket(id, (m) => this.onWs(m), (s) => this.onLink(s));
  }

  async start(): Promise<void> {
    clear(this.root);
    this.loadTheme();
    this.root.className = `pilot ${this.theme}`;
    const mapEl = h("div", { class: "map", id: "map" });
    this.root.append(mapEl, this.dot, this.recenter, this.panelCol, this.statusPanel);
    // The state first: it gives the vehicle type and the position to centre the map on.
    try {
      this.applyState(await pilotApi.state(this.id));
    } catch (e) {
      if (this.sessionLost(e)) return;
    }
    const center: [number, number] | undefined = this.fix ? [this.fix.lon, this.fix.lat] : undefined;
    const base = await createMap(mapEl, { center, zoom: this.fix ? 10.5 : 8.2 });
    this.map = base.map;
    this.hasBasemap = base.hasBasemap;
    const geo = await api.airspace().catch(() => ({ type: "FeatureCollection", features: [] }) as GeoJSON.FeatureCollection);
    this.airspace = splitAirspace(geo);
    addAirspaceLayers(this.map, this.airspace, base.hasBasemap);
    this.addTrafficLayer();
    this.addMine();
    addRouteLayers(this.map);
    this.addLegLayer();
    this.addRerouteLayer();
    this.addAreaLayer();
    this.aircraft = new AircraftLayer(this.map);
    this.addUnitLayer();
    this.addAdsbLayer();
    this.paintTheme();
    void this.refreshCorridors();
    void this.loadAi();
    if (!this.fix) this.map.fitBounds(FOCUS_BOUNDS, { padding: 30, duration: 0 });
    this.map.on("dragstart", (ev) => {
      if ((ev as { originalEvent?: unknown }).originalEvent) this.setFollow(false);
    });
    this.markSites();
    this.drawUnit();
    this.drawRoutes();
    // Hold rebuilds while a finger or mouse is down on the sheet; release after the press (and its click) is over.
    this.sheet.addEventListener("pointerdown", () => { this.sheetHeld = true; });
    const release = () => {
      if (!this.sheetHeld) return;
      this.sheetHeld = false;
      if (this.sheetDirty) requestAnimationFrame(() => { if (!this.sheetHeld && this.sheetDirty) { this.sheetDirty = false; this.renderSheet(); } });
    };
    window.addEventListener("pointerup", release);
    window.addEventListener("pointercancel", release);
    this.renderAll();
    this.socket.start();
    this.timers.push(window.setInterval(() => this.tick(), 1000));
    document.addEventListener("visibilitychange", this.onVisible);
    (window as unknown as { __pilot: unknown }).__pilot = { app: this };
  }

  destroy(): void {
    this.destroyed = true;
    this.socket.stop();
    this.timers.forEach((t) => clearInterval(t));
    document.removeEventListener("visibilitychange", this.onVisible);
    void this.releaseWake();
    this.aircraft?.destroy();
    this.map?.remove();
  }

  // ---- data in ----------------------------------------------------------------------------------
  private sessionLost(e: unknown): boolean {
    if (e instanceof PilotError && (e.status === 401 || e.status === 404)) {
      this.destroy();
      this.onSessionEnd();
      return true;
    }
    return false;
  }

  private applyState(s: DeviceState, askedAt = Date.now()): void {
    const fresh = askedAt >= this.pushedAt; // a poll sent before the latest push is out of date for these fields
    const typeChanged = fresh && s.vehicle_type !== this.vt;
    if (fresh) {
      this.vt = s.vehicle_type;
      this.demo = s.demo;
      this.online = s.online;
      if (s.pending_reroute !== undefined) this.reroute = s.pending_reroute;
      if (s.ai_ack_version !== undefined) this.aiAck = s.ai_ack_version;
      if (s.adsb_state !== undefined && askedAt >= this.adsbAt) this.adsbState = s.adsb_state; // same rule as the list below
      if (s.adsb && askedAt >= this.adsbAt) this.adsb = s.adsb; // an answer asked before the latest traffic message is older
    }
    this.lastSeen = s.last_seen;
    this.callsign = s.callsign ?? this.callsign;
    if (s.latest_fix) this.fix = s.latest_fix;
    if (s.latest_route_response) this.remember(s.latest_route_response);
    const f = s.current_flight;
    const sameFlight = !!f && this.flight?.flight_id === f.flight_id;
    // An ended or replaced flight is shown only if we were already on it (never resurrected after "Done").
    const finished = sameFlight && this.flight?.status === "ended"; // a late poll must not revive a flight that ended
    if (!finished && !(f && this.endedFlights.has(f.flight_id)) && ((f && ACTIVE.has(f.status)) || (f && sameFlight && f.status === "ended"))) {
      // A poll asked before the latest pushed flight-status is older for the status fields: keep what the push said.
      this.flight = sameFlight && this.flight && askedAt < this.flightPushedAt ? { ...f, status: this.flight.status, detected_by: this.flight.detected_by } : f;
      this.flightSince = this.flightSince || Date.now();
      this.navRoute = this.findRoute(f.request_id, f.route_id) ?? this.navRoute;
      if (this.ui !== "home") this.ui = "home";
    }
    if (typeChanged) this.typeChanged();
  }

  private async refresh(): Promise<void> {
    try {
      const askedAt = Date.now();
      const s = await pilotApi.state(this.id);
      if (this.destroyed) return;
      this.applyState(s, askedAt);
      this.afterData();
    } catch (e) {
      this.sessionLost(e);
    }
  }

  private onLink(s: LinkState): void {
    this.link = s;
    if (s === "live") void this.refresh(); // repair any gap
    this.renderDot();
  }

  private onWs(m: WsMessage): void {
    if (m.type === "online" || m.type === "offline" || m.type === "vehicle-type" || m.type === "reroute") this.pushedAt = Date.now();
    if (m.type === "flight-status") this.flightPushedAt = Date.now();
    switch (m.type) {
      case "state":
        this.applyState(m.payload);
        break;
      case "route-response":
        this.onRouteResponse(m.payload);
        return;
      case "flight-status":
        this.onFlightStatus(m.payload);
        break;
      case "clearance":
        if (this.flight && this.flight.flight_id === m.payload.flight_id) this.flight.atc = m.payload;
        break;
      case "traffic":
        this.traffic = m.payload;
        if (m.payload.adsb || m.payload.adsb_state !== undefined) this.adsbAt = Date.now();
        if (m.payload.adsb) this.adsb = m.payload.adsb;
        if (m.payload.adsb_state !== undefined) this.adsbState = m.payload.adsb_state;
        this.drawTraffic();
        break;
      case "online":
        this.online = true;
        break;
      case "offline":
        this.online = false;
        break;
      case "vehicle-type":
        if (m.payload.vehicle_type !== this.vt) {
          this.vt = m.payload.vehicle_type;
          this.typeChanged();
        }
        this.demo = m.payload.demo;
        break;
      case "heartbeat":
        this.online = m.payload.online;
        break;
      case "reroute": {
        // Approved: show it. Any other status (acknowledged, unable, expired, withdrawn) closes it.
        const r = m.payload;
        if (r.status === "approved") this.reroute = r;
        else if (this.reroute?.id === r.id) this.reroute = null;
        break;
      }
    }
    this.afterData();
  }

  private remember(r: Resp): void {
    const list = this.responses.get(r.request_id) ?? [];
    if (!list.some((x) => x.revision === r.revision && x.routes[0]?.route_id === r.routes[0]?.route_id)) list.push(r);
    this.responses.set(r.request_id, list);
    this.latestResponse = r;
  }

  private findRoute(requestId: string, routeId: string): RouteOption | null {
    for (const r of this.responses.get(requestId) ?? []) {
      const hit = r.routes.find((x) => x.route_id === routeId);
      if (hit) return hit;
    }
    return null;
  }

  private onRouteResponse(r: Resp): void {
    const seenIds = new Set((this.responses.get(r.request_id) ?? []).flatMap((x) => x.routes.map((o) => o.route_id)));
    this.remember(r);
    if (this.flight && ACTIVE.has(this.flight.status) && r.request_id === this.flight.request_id) {
      // A new revision of the running flight's request: options we haven't seen are a suggestion.
      this.afterData();
      void seenIds;
      return;
    }
    if (this.ui === "options" && this.options && r.request_id === this.options.request_id && r.revision >= this.options.revision) {
      this.options = r; // same request: a revised summary or options
      this.stale = false;
      this.drawRoutes();
      this.renderSheet();
    }
  }

  /** A revision for the running flight whose first route is new, shown as the banner. */
  private suggestion(): Resp | null {
    const f = this.flight;
    if (!f || !ACTIVE.has(f.status)) return null;
    const list = this.responses.get(f.request_id) ?? [];
    const first = list.find((x) => x.revision === 0);
    const known = new Set(first ? first.routes.map((o) => o.route_id) : []);
    const best = [...list].filter((x) => x.revision > 0 && x.status === "ok" && x.routes.length).sort((a, b) => b.revision - a.revision)[0];
    if (!best) return null;
    const top = best.routes[0].route_id;
    if (top === f.route_id || known.has(top) || this.dismissed.has(`${best.request_id}:${best.revision}`)) return null;
    return best;
  }

  private onFlightStatus(s: FlightStatus): void {
    const f = this.flight;
    if (f && f.flight_id === s.flight_id) {
      f.status = s.status;
      f.detected_by = s.detected_by ?? null;
      if (s.status === "ended" || s.status === "replaced") this.map && this.drawRoutes();
    } else if (ACTIVE.has(s.status)) {
      void this.refresh(); // a flight we don't know yet (planned from another screen)
    }
  }

  private typeChanged(): void {
    if (this.map) this.markSites();
    if (!this.flight && this.ui === "options") {
      this.ui = "home";
      this.options = null;
      this.drawRoutes();
    }
  }

  private afterData(): void {
    if (this.destroyed || !this.map) return;
    this.syncReroute();
    this.drawAdsb();
    this.drawUnit();
    this.drawRoutes();
    this.renderAll();
    this.syncWake();
    if (this.follow && this.flight && ACTIVE.has(this.flight.status) && this.fix) {
      const pad = this.mapPadding();
      this.map.easeTo({ center: [this.fix.lon, this.fix.lat], padding: pad, bearing: this.headingUp ? this.fix.course_deg : 0, duration: 600 });
    }
  }

  private tick(): void {
    this.tickN++;
    if (this.themeChoice === "auto" && this.tickN % 30 === 0) this.applyTheme(autoTheme());
    if (this.link === "down" || this.tickN % 4 === 0) {
      // Socket down: poll every 2 s. Socket up: refresh position while a flight runs.
      if (this.link === "down" ? this.tickN % 2 === 0 : !!this.flight && ACTIVE.has(this.flight.status)) void this.refresh();
    }
    const stale = !!this.options && Date.now() > Date.parse(this.options.expires_at);
    if (stale !== this.stale) {
      this.stale = stale;
      if (this.ui === "options") this.renderSheet();
    }
    if (this.tickN % 10 === 0) void this.refreshCorridors(); // predicted congestion is always on, flying or not
    if (this.reroute && Date.parse(this.reroute.expires_at) < Date.now()) this.reroute = null; // an expired proposal goes quietly
    this.syncReroute();
    if (this.showStatus) this.refreshStatusValues();
    if (this.endArmedAt && Date.now() - this.endArmedAt > 4000) this.endArmedAt = 0; // the confirm tap lapsed
    if (this.flight && ACTIVE.has(this.flight.status)) this.renderSheet(); // ETAs and age move with the clock
  }

  private onVisible = (): void => {
    if (document.visibilityState === "visible") {
      this.syncWake(true);
      void this.refresh();
    }
  };

  // ---- derived ----------------------------------------------------------------------------------
  private pos(): LL | null {
    return this.fix ? { lat: this.fix.lat, lon: this.fix.lon } : null;
  }

  private nodeName(id: string): string {
    return this.airspace?.nodes.get(id)?.props.name ?? id;
  }

  /** Glance name: the full name without its parenthetical ("Downtown Tampa (I-4 / I-275)" -> "Downtown Tampa"). */
  private shortName(id: string): string {
    return shortenName(this.nodeName(id));
  }

  private destinations(): { id: string; name: string; dist: number | null }[] {
    const p = this.pos();
    const out: { id: string; name: string; dist: number | null }[] = [];
    for (const [id, n] of this.airspace.nodes) {
      if (!siteServes(n.props, this.vt)) continue;
      out.push({ id, name: n.props.name ?? id, dist: p ? distanceM(p, n) : null });
    }
    return out.sort((a, b) => (a.dist ?? 0) - (b.dist ?? 0) || a.name.localeCompare(b.name));
  }

  private navNodes(): { nodes: string[]; eta: Map<string, { p10: number; p50: number; p90: number }> } {
    const eta = new Map<string, { p10: number; p50: number; p90: number }>();
    let nodes: string[] = this.flight?.path ?? [];
    if (this.navRoute) {
      nodes = this.navRoute.eta.map((e) => e.node);
      for (const e of this.navRoute.eta) eta.set(e.node, { p10: Date.parse(e.eta_p10) / 1000, p50: Date.parse(e.eta_p50) / 1000, p90: Date.parse(e.eta_p90) / 1000 });
    } else if (this.flight) {
      for (const [n, t] of Object.entries(this.flight.eta_by_node)) eta.set(n, { p10: t, p50: t, p90: t });
    }
    return { nodes, eta };
  }

  /** Seconds to the destination and the next waypoint index, from the unit's position. */
  private progress(): { next: string | null; nextIdx: number; nodes: string[]; eta: Map<string, { p10: number; p50: number; p90: number }> } {
    const { nodes, eta } = this.navNodes();
    const p = this.pos();
    const pts = nodes.map((n) => this.airspace.nodes.get(n)).filter((x): x is NonNullable<typeof x> => !!x);
    if (!p || pts.length < 2 || pts.length !== nodes.length) return { next: nodes[1] ?? null, nextIdx: 1, nodes, eta };
    const i = nextNodeIndex(p, pts);
    return { next: nodes[i] ?? null, nextIdx: i, nodes, eta };
  }

  private levelAhead(nodes: string[], fromIdx: number): TrafficLevel | null {
    if (!this.traffic) return null;
    let worst: TrafficLevel | null = null;
    for (let i = Math.max(1, fromIdx); i < Math.min(nodes.length, fromIdx + 3); i++) {
      const e = this.traffic.edges.find((x) => x.edge === `${nodes[i - 1]}->${nodes[i]}` || x.edge === `${nodes[i]}->${nodes[i - 1]}`);
      if (e && (!worst || LEVEL_RANK[e.now] > LEVEL_RANK[worst])) worst = e.now;
    }
    return worst;
  }

  // ---- map --------------------------------------------------------------------------------------
  private addMine(): void {
    // Landing sites: only the ones that serve this unit's type, small and neutral. The shared all-sites layer is hidden
    // (red means trouble, never "helipad"); the destination alone is the blue pin; the site under our own puck is hidden.
    for (const id of ["sites", "sites-mine"]) if (this.map.getLayer(id)) this.map.setLayoutProperty(id, "visibility", "none");
    const c = THEME_COLORS[this.theme];
    this.map.addLayer({ id: "sites-mine", type: "circle", source: "sites", filter: ["all", ["==", ["get", "mine"], true], ["!=", ["get", "here"], true], ["!=", ["get", "dest"], true]], paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 7, 3, 12, 6], "circle-color": c.corridor, "circle-opacity": 0.85, "circle-stroke-color": c.casing, "circle-stroke-width": 1.5 } });
    this.map.addLayer({ id: "sites-dest", type: "circle", source: "sites", filter: ["==", ["get", "dest"], true], paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 7, 8, 12, 12], "circle-color": c.route, "circle-stroke-color": "#ffffff", "circle-stroke-width": 3 } });
    if (this.map.getLayer("site-labels")) {
      this.map.setFilter("site-labels", ["==", ["get", "dest"], true]);
      this.map.setLayoutProperty("site-labels", "text-field", ["get", "short"]); // one label: the chosen destination
      this.map.setLayoutProperty("site-labels", "visibility", "visible");
    }
  }

  /** The destination the pilot is looking at: the chosen one in options, the flight's while navigating. */
  private activeDestination(): string | null {
    if (this.flight && ACTIVE.has(this.flight.status)) return this.flight.destination;
    return this.ui === "options" ? this.destination : null;
  }

  /** Flag each site as serving our type, under the puck, or the selected destination; redraw only on change. */
  private markSites(): void {
    const p = this.pos();
    const dest = this.activeDestination();
    let key = `${this.vt}|${dest}`;
    const feats = this.airspace.sites.features;
    for (const f of feats) {
      const pr = f.properties as Record<string, unknown>;
      const [lon, lat] = f.geometry.coordinates;
      const here = !!p && distanceM(p, { lat, lon }) < 400;
      const mine = siteServes(pr as never, this.vt);
      const isDest = pr.id === dest;
      pr.mine = mine; pr.here = here; pr.dest = isDest; pr.short = shortenName(String(pr.name ?? pr.id));
      key += here ? `|${pr.id}` : "";
    }
    if (key === this.sitesKey) return;
    this.sitesKey = key;
    (this.map.getSource("sites") as GeoJSONSource).setData(this.airspace.sites);
  }

  /** The airspace the selected route crosses (no-go and Class B/C/D volumes), drawn very faintly; nothing otherwise. */
  private addAreaLayer(): void {
    this.map.addSource("route-area", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    this.map.addLayer({ id: "route-area", type: "line", source: "route-area", paint: { "line-color": THEME_COLORS.day.label, "line-opacity": 0.28, "line-width": 1.5, "line-dasharray": [3, 2] } }, "routes-casing");
  }

  private drawArea(line: RouteLine | undefined): void {
    const key = line ? `${line.id}:${line.waypoints.length}` : "";
    if (key === this.areaKey) return;
    this.areaKey = key;
    const features = line ? this.airspace.nogo.features.filter((f) => routeCrosses(line.waypoints, f.geometry)) : [];
    (this.map.getSource("route-area") as GeoJSONSource).setData({ type: "FeatureCollection", features });
  }

  /** Our own position: a blue puck with a white ring and a heading wedge, drawn once as an image and rotated by course. */
  private addUnitLayer(): void {
    const empty: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };
    this.map.addSource("unit", { type: "geojson", data: empty });
    this.map.addImage("puck", puckImage(THEME_COLORS.day.route), { pixelRatio: 2 });
    this.map.addLayer({ id: "unit-halo", type: "circle", source: "unit", paint: { "circle-radius": 20, "circle-color": THEME_COLORS.day.route, "circle-opacity": 0.14 } });
    this.map.addLayer({ id: "unit", type: "symbol", source: "unit", layout: { "icon-image": "puck", "icon-size": 1, "icon-rotate": ["get", "course"], "icon-rotation-alignment": "map", "icon-allow-overlap": true, "icon-ignore-placement": true } });
  }

  private drawUnit(): void {
    if (!this.map?.getSource("unit")) return;
    const features: GeoJSON.Feature[] = this.fix
      ? [{ type: "Feature", properties: { course: this.fix.course_deg ?? 0 }, geometry: { type: "Point", coordinates: [this.fix.lon, this.fix.lat] } }]
      : [];
    (this.map.getSource("unit") as GeoJSONSource).setData({ type: "FeatureCollection", features });
    this.markSites(); // the site we are standing on drops out
  }

  /** Corridor colours: the AI's forecast for the next 15 min from `/api/traffic`, refreshed every 10 s (no flight needed). */
  private async refreshCorridors(): Promise<void> {
    const t = await pilotApi.traffic(this.id).catch(() => null);
    if (this.destroyed || !t || !this.map?.getSource("corridors")) return;
    colorCorridors(this.map, this.airspace, (a, b) => {
      const e = t.edges[`${a}->${b}`] ?? t.edges[`${b}->${a}`];
      return e ? (e.by_minute?.["15"] ?? e.in_15_min ?? e.now) : undefined;
    });
  }

  /** Moderate and heavy corridors drawn a little wider on top of the quiet base line; low stays plain. */
  private addTrafficLayer(): void {
    this.map.addLayer({
      id: "corridor-traffic", type: "line", source: "corridors",
      filter: ["any", ["==", ["get", "level"], "moderate"], ["==", ["get", "level"], "heavy"]],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": ["match", ["get", "level"], "heavy", TRAFFIC_COLOR.heavy, TRAFFIC_COLOR.moderate], "line-width": ["interpolate", ["linear"], ["zoom"], 7, 5, 12, 10], "line-opacity": 0.9 },
    }, "corridor-coordination");
  }

  /** Congested legs of the selected route, drawn over its blue line in the forecast colour. Layer added once; fed by `drawLegs`. */
  private addLegLayer(): void {
    this.map.addSource("route-legs", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    this.map.addLayer({ id: "route-legs", type: "line", source: "route-legs", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": ["get", "color"], "line-width": 6 } });
  }

  /** ADS-B traffic is a small grey chevron rotated to its track (never a dot, so it cannot pass for a node or pad). */
  private addAdsbLayer(): void {
    this.map.addImage("adsb-chevron", chevronImage(), { pixelRatio: 2 });
    this.map.addSource("adsb", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    this.map.addLayer({ id: "adsb-chevron", type: "symbol", source: "adsb", layout: { "icon-image": "adsb-chevron", "icon-size": 0.7, "icon-rotate": ["get", "track"], "icon-rotation-alignment": "map", "icon-allow-overlap": true, "icon-ignore-placement": true } });
    // The neutral highlight: only for traffic within 3 NM, within 1,000 ft and converging (see drawAdsb). A soft grey
    // halo behind the chevron, never amber or red (it is not an alert); its tag also says "close".
    this.map.addLayer({ id: "adsb-halo", type: "circle", source: "adsb", filter: ["==", ["get", "close"], true], paint: { "circle-radius": 17, "circle-color": this.theme === "night" ? "#e8eaed" : "#5f6368", "circle-opacity": 0.22, "circle-stroke-color": this.theme === "night" ? "#e8eaed" : "#5f6368", "circle-stroke-width": 1, "circle-stroke-opacity": 0.5 } }, "adsb-chevron");
    if (this.map.getStyle().glyphs) {
      this.map.addLayer({ id: "adsb-label", type: "symbol", source: "adsb", minzoom: 9, filter: ["!=", ["get", "label"], ""], layout: { "text-field": ["get", "label"], "text-font": ["Noto Sans Regular"], "text-size": 13, "text-offset": [0, -1.4], "text-optional": true }, paint: { "text-color": "#5f6368", "text-halo-color": "#ffffff", "text-halo-width": 1.2 } });
    }
  }

  /** Each ADS-B aircraft: a grey chevron on its track. Within 3 NM and 1,000 ft: the tag ("+05" = 500 ft above).
   *  A neutral halo and the word "close" only when, in addition, the aircraft is converging (closing from both ground
   *  vectors). Never amber or red, never an alarm. */
  private drawAdsb(): void {
    if (!this.map?.getSource("adsb")) return;
    const own = this.fix;
    const features: GeoJSON.Feature[] = [];
    const M_PER_NM = 1852;
    if (!own || this.adsbState !== "ok") { // no position yet, or the feed is not ok: no aircraft are shown as current
      setData(this.map, "adsb", { type: "FeatureCollection", features });
      return;
    }
    for (const t of this.adsb) {
      const dy = (t.lat - own.lat) * 111_320;
      const dx = (t.lon - own.lon) * 111_320 * Math.cos((own.lat * Math.PI) / 180);
      const horiz = Math.hypot(dx, dy);
      const near = horiz <= 3 * M_PER_NM && Math.abs(t.rel_alt_ft) <= 1000;
      // Converging: the relative velocity points back toward the unit (closing), using both aircraft's ground vectors.
      const ov = [(own.ground_speed_mps ?? 0) * Math.sin((own.course_deg * Math.PI) / 180), (own.ground_speed_mps ?? 0) * Math.cos((own.course_deg * Math.PI) / 180)];
      const tm = t.gs_kt * 0.514444;
      const tv = [tm * Math.sin((t.track_deg * Math.PI) / 180), tm * Math.cos((t.track_deg * Math.PI) / 180)];
      const converging = dx * (tv[0] - ov[0]) + dy * (tv[1] - ov[1]) < 0;
      const hundreds = Math.round(t.rel_alt_ft / 100);
      const close = near && converging;
      const alt = hundreds === 0 ? "same altitude" : `${Math.abs(hundreds) * 100} ft ${hundreds > 0 ? "above" : "below"}`;
      const label = near ? (close ? `close, ${alt}` : alt) : "";
      features.push({ type: "Feature", properties: { icao: t.icao, label, close, track: t.track_deg }, geometry: { type: "Point", coordinates: [t.lon, t.lat] } });
    }
    setData(this.map, "adsb", { type: "FeatureCollection", features });
  }

  /** The ATC proposal: a purple line above the route (AI proposals only use this colour), with a white casing. */
  private addRerouteLayer(): void {
    this.map.addSource("reroute", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    const layout = { "line-cap": "round", "line-join": "round" } as const;
    this.map.addLayer({ id: "reroute-casing", type: "line", source: "reroute", layout, paint: { "line-color": "#ffffff", "line-width": 9 } });
    this.map.addLayer({ id: "reroute", type: "line", source: "reroute", layout, paint: { "line-color": this.theme === "night" ? "#BF5AF2" : "#AF52DE", "line-width": 5 } });
  }

  /** While an ATC proposal is open: show it, dim the current route to ~40 %, and fit both routes once when it appears. */
  private syncReroute(): void {
    if (!this.map?.getSource("reroute")) return;
    const rr = this.pendingReroute();
    const id = rr?.id ?? null;
    if (id === this.rrShown) return;
    this.rrShown = id;
    setData(this.map, "reroute", { type: "FeatureCollection", features: rr ? [{ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: rr.proposed.waypoints.map(([lat, lon]) => [lon, lat]) } }] : [] });
    this.map.setPaintProperty("routes", "line-opacity", rr ? 0.4 : (["case", ["get", "selected"], 1, 0.75] as never));
    this.map.setPaintProperty("route-legs", "line-opacity", rr ? 0.4 : 1);
    if (rr && this.navRoute) {
      const pts = [...this.navRoute.waypoints, ...rr.proposed.waypoints].map(([lat, lon]) => [lon, lat] as [number, number]);
      fitTo(this.map, pts, this.mapPadding());
      this.setFollow(false); // the camera stays where it fitted both routes until the pilot recenters
    }
  }

  /** Each leg the AI says is moderate or heavy when the aircraft gets there (`segments`, U-API contract §1). Low legs stay blue. */
  private drawLegs(r: RouteOption | null, stale: boolean): void {
    const features: GeoJSON.Feature[] = [];
    if (!r || stale) {
      setData(this.map, "route-legs", { type: "FeatureCollection", features });
      return;
    }
    for (const s of r.segments ?? []) {
      if (s.level === "low") continue;
      const a = this.airspace.nodes.get(s.from), b = this.airspace.nodes.get(s.to);
      if (!a || !b) continue;
      const i = nearestWaypoint(r.waypoints, { lat: a.lat, lon: a.lon }), j = nearestWaypoint(r.waypoints, { lat: b.lat, lon: b.lon });
      const [lo, hi] = i < j ? [i, j] : [j, i];
      if (hi - lo < 1) continue;
      features.push({ type: "Feature", properties: { color: TRAFFIC_COLOR[s.level] }, geometry: { type: "LineString", coordinates: r.waypoints.slice(lo, hi + 1).map(([lat, lon]) => [lon, lat]) } });
    }
    setData(this.map, "route-legs", { type: "FeatureCollection", features });
  }

  private drawTraffic(): void {
    if (!this.traffic || !this.map) return;
    const p = this.pos();
    const list: AircraftState[] = this.traffic.aircraft
      .filter((a) => a.id !== this.id)
      .filter((a) => !(a as { on_ground?: boolean }).on_ground) // parked aircraft are not traffic: hidden
      .filter((a) => !p || distanceM(p, { lat: a.lat, lon: a.lon }) <= NEARBY_M)
      .map((a) => ({ ...a, ground_speed_mps: 0, timestamp: Date.now() / 1000, online: true, flight_status: null }));
    this.aircraft.update(list);
  }

  private drawRoutes(): void {
    if (!this.map?.getSource("routes")) return;
    const c = THEME_COLORS[this.theme];
    const lines: RouteLine[] = [];
    let legRoute: RouteOption | null = null;
    if (this.flight && ACTIVE.has(this.flight.status)) {
      if (this.navRoute) lines.push({ id: "nav", waypoints: this.navRoute.waypoints, color: c.route, selected: true });
      legRoute = this.navRoute;
      const s = this.suggestion();
      if (s) lines.push({ id: "alt", waypoints: s.routes[0].waypoints, color: c.routeAlt, dashed: true });
    } else if (this.ui === "options" && this.options) {
      // The selected route is the one blue line; the others are grey, like a maps app.
      for (const r of this.options.routes) lines.push({ id: r.route_id, waypoints: r.waypoints, color: r.rank === this.selectedRank && !this.stale ? c.route : c.routeAlt, selected: r.rank === this.selectedRank });
      legRoute = this.options.routes.find((r) => r.rank === this.selectedRank) ?? null;
    }
    setRoutes(this.map, lines);
    this.drawLegs(legRoute, this.ui === "options" && this.stale);
    this.drawArea(lines.find((l) => l.selected));
    this.markSites();
  }

  // ---- theme ------------------------------------------------------------------------------------
  private loadTheme(): void {
    try {
      const v = localStorage.getItem(THEME_KEY);
      if (v === "day" || v === "night") this.themeChoice = v;
    } catch { /* storage blocked */ }
    this.theme = this.themeChoice === "auto" ? autoTheme() : this.themeChoice;
  }

  private paintTheme(): void {
    applyCarplayTheme(this.map, this.theme, this.hasBasemap);
    styleOverlays(this.map, this.theme);
    this.map.setPaintProperty("unit-halo", "circle-color", THEME_COLORS[this.theme].route);
    const c = THEME_COLORS[this.theme];
    this.map.setPaintProperty("sites-mine", "circle-color", c.corridor);
    this.map.setPaintProperty("sites-mine", "circle-stroke-color", c.casing);
    this.map.setPaintProperty("sites-dest", "circle-color", c.route);
    this.map.setPaintProperty("route-area", "line-color", c.label);
    if (this.map.getLayer("adsb-halo")) { const g = this.theme === "night" ? "#e8eaed" : "#5f6368"; this.map.setPaintProperty("adsb-halo", "circle-color", g); this.map.setPaintProperty("adsb-halo", "circle-stroke-color", g); }
    if (this.map.getLayer("reroute")) this.map.setPaintProperty("reroute", "line-color", this.theme === "night" ? "#BF5AF2" : "#AF52DE");
    // Quiet by default: a corridor with no moderate/heavy forecast keeps the theme's corridor colour (no traffic colour).
    this.map.setPaintProperty("corridors", "line-color", ["match", ["get", "level"], "moderate", TRAFFIC_COLOR.moderate, "heavy", TRAFFIC_COLOR.heavy, c.corridor]);
  }

  setTheme(choice: Theme | "auto"): void {
    this.themeChoice = choice;
    try { if (choice === "auto") localStorage.removeItem(THEME_KEY); else localStorage.setItem(THEME_KEY, choice); } catch { /* storage blocked */ }
    this.applyTheme(choice === "auto" ? autoTheme() : choice);
  }

  private applyTheme(t: Theme): void {
    if (t === this.theme) return;
    this.theme = t;
    this.root.classList.toggle("night", t === "night");
    this.root.classList.toggle("day", t === "day");
    this.paintTheme();
    this.drawRoutes();
  }

  private setFollow(v: boolean): void {
    this.follow = v;
    this.recenter.hidden = v; // only shown once the pilot has moved the map away
  }

  private recenterNow(): void {
    this.setFollow(true);
    if (this.fix) this.map.easeTo({ center: [this.fix.lon, this.fix.lat], zoom: Math.max(this.map.getZoom(), 10), bearing: this.headingUp ? this.fix.course_deg : 0, duration: 600 });
  }

  setHeadingUp(v: boolean): void {
    this.headingUp = v;
    if (!v) this.map.easeTo({ bearing: 0, duration: 500 });
    else if (this.fix) this.map.easeTo({ bearing: this.fix.course_deg, duration: 500 });
    if (this.showStatus) this.refreshStatusValues();
  }

  /** Space the left panel (landscape) or bottom sheet (portrait) takes from the map, so routes fit in what is left. */
  private mapPadding(): { top: number; bottom: number; left: number; right: number } {
    const w = this.root.clientWidth, hh = this.root.clientHeight;
    const r = this.panelCol.getBoundingClientRect();
    if (w > hh) return { top: 40, bottom: 40, left: Math.round(r.right) + 30, right: 40 };
    // Portrait: the bottom sheet may cover part of the routes, never all of them. Top and bottom stay within 60 % of the height,
    // so the fit always has room (an uncapped bottom of 848 px on an 844 px screen made the camera fit impossible).
    const top = 70;
    const bottom = Math.max(0, Math.min(Math.round(hh - r.top) + 20, Math.round(hh * 0.6) - top));
    return { top, bottom, left: 30, right: 30 };
  }

  // ---- actions ----------------------------------------------------------------------------------
  private async requestRoute(destination: string): Promise<void> {
    if (!this.online) return;
    this.destination = destination;
    this.expanded = false;
    this.ui = "options";
    this.loading = true;
    this.message = "";
    this.options = null;
    this.stale = false;
    this.renderSheet();
    try {
      const r = (await pilotApi.routeRequest(this.id, destination, uuid())) as Resp;
      this.remember(r);
      this.loading = false;
      if (r.status === "ok" && r.routes.length) {
        this.options = r;
        this.selectedRank = r.routes[0].rank;
        this.drawRoutes();
        this.fitOptions();
      } else this.message = r.reason ?? "No route is available to that destination.";
    } catch (e) {
      this.loading = false;
      if (this.sessionLost(e)) return;
      if (e instanceof PilotError && e.code === "unit_offline") this.online = false;
      this.message = e instanceof PilotError && e.code === "unit_offline" ? "Unit not connected." : "Couldn't get routes. Try again.";
    }
    this.renderAll();
  }

  private fitOptions(): void {
    const pts = (this.options?.routes ?? []).flatMap((r) => r.waypoints.map(([lat, lon]) => [lon, lat] as [number, number]));
    fitTo(this.map, pts, this.mapPadding());
  }

  private async go(r: RouteOption, req: Resp): Promise<void> {
    if (!this.online) return;
    try {
      const fs = await pilotApi.routeSelection(this.id, req.request_id, r.route_id);
      this.navRoute = r;
      this.flight = {
        flight_id: fs.flight_id, status: fs.status, route_id: r.route_id, request_id: req.request_id,
        destination: r.eta[r.eta.length - 1]?.node ?? this.destination ?? "", path: r.eta.map((e) => e.node), departure_s: 0, eta_by_node: {},
      };
      this.flightSince = Date.now();
      this.ui = "home";
      this.options = null;
      this.setFollow(true);
      this.message = "";
      if (this.fix) this.map.easeTo({ center: [this.fix.lon, this.fix.lat], zoom: 11, duration: 700 });
      this.afterData();
      void this.refresh();
    } catch (e) {
      if (this.sessionLost(e)) return;
      if (e instanceof PilotError && e.code === "unit_offline") this.online = false;
      else this.message = "Couldn't start that route. Try again.";
      this.renderAll();
    }
  }

  /** End: the first tap arms it (the button says so), a second tap within 4 s ends the flight on the server. */
  private async endTap(): Promise<void> {
    const f = this.flight;
    if (!f || !this.online) return;
    if (!this.endArmedAt || Date.now() - this.endArmedAt > 4000) {
      this.endArmedAt = Date.now();
      this.renderSheet();
      return;
    }
    this.endArmedAt = 0;
    try {
      await pilotApi.flightEnd(this.id);
      this.endedFlights.add(f.flight_id);
      this.message = "";
      this.done();
    } catch (e) {
      if (this.sessionLost(e)) return;
      if (e instanceof PilotError && e.code === "unit_offline") this.online = false;
      else this.message = "Couldn't end the flight. Try again.";
      this.renderAll();
    }
  }

  private done(): void {
    this.endArmedAt = 0;
    this.flight = null;
    this.navRoute = null;
    this.ui = "home";
    this.options = null;
    this.drawRoutes();
    this.renderAll();
    this.syncWake();
  }

  // ---- wake lock --------------------------------------------------------------------------------
  private syncWake(force = false): void {
    const want = !!this.flight && ACTIVE.has(this.flight.status);
    if (!want) {
      void this.releaseWake();
      return;
    }
    const nav = navigator as Navigator & { wakeLock?: { request(t: "screen"): Promise<{ release(): Promise<void>; addEventListener?: (n: string, f: () => void) => void }> } };
    if (!nav.wakeLock || (this.wakeLock && !force)) return;
    nav.wakeLock.request("screen").then((l) => {
      this.wakeLock = l;
      l.addEventListener?.("release", () => { if (this.wakeLock === l) this.wakeLock = null; });
    }).catch(() => { this.wakeLock = null; }); // denied (low battery, unsupported): the screen may dim, nothing else
  }

  private async releaseWake(): Promise<void> {
    const l = this.wakeLock;
    this.wakeLock = null;
    try {
      await l?.release();
    } catch {
      /* already released */
    }
  }

  // ---- rendering --------------------------------------------------------------------------------
  private renderAll(): void {
    this.renderDot();
    this.offline.hidden = this.online;
    this.root.classList.toggle("is-offline", !this.online);
    this.renderSheet();
    if (this.showStatus) this.refreshStatusValues();
  }

  /** Everything fine: a small green dot. Trouble: amber (reconnecting) or red (unit offline). The detail is one tap away. */
  private renderDot(): void {
    const linkLabel = this.link === "live" ? "Live" : "Reconnecting";
    const state = !this.online ? "bad" : this.link === "live" ? "ok" : "warn";
    this.dot.className = `status-dot ${state}`;
    clear(this.dot);
    this.dot.append(h("i", {}), h("span", { class: "sr", id: "link" }, linkLabel));
  }

  private sheetHtml = "";

  /** Rebuild the panel only when what it shows changed: a re-render between touch-down and touch-up would eat a tap. */
  private renderSheet(): void {
    this.renderSheetNow();
    this.markCutOff();
  }

  private renderSheetNow(): void {
    if (!this.airspace) return;
    if (this.sheetHeld) { this.sheetDirty = true; return; } // never swap rows under a pointer: the tap is lost
    const cls = `sheet${this.expanded || this.query.trim() ? " expanded" : ""}${this.flight ? " nav" : ""}${this.flight?.status === "ended" ? " ended" : ""}`;
    if (this.patchHomeDistances(cls)) return;
    const next = h("div");
    if (this.flight) next.append(...keep(this.navigateView()));
    else if (!this.online) next.append(this.offlineView());
    else if (this.ui === "options") next.append(...keep(this.optionsView()));
    else next.append(...this.homeView());
    // The key describes what `next` shows, so it is refreshed even when the rendered HTML is unchanged.
    this.homeKey = this.isHome() ? this.homeKeyNow() : "";
    if (next.innerHTML === this.sheetHtml && this.sheet.className === cls) return;
    this.sheetHtml = next.innerHTML;
    const hadFocus = document.activeElement?.id === "dest-search";
    clear(this.sheet);
    this.sheet.className = cls;
    this.sheet.append(...next.childNodes);
    if (hadFocus) this.sheet.querySelector<HTMLInputElement>("#dest-search")?.focus();
  }

  private searchBar(disabled: boolean): HTMLElement {
    const input = h("input", {
      id: "dest-search", class: "search", type: "search", placeholder: "Where to?", "aria-label": "Where to?", value: this.query, autocomplete: "off", disabled,
      onfocus: () => { this.expanded = true; this.sheet.classList.add("expanded"); },
      oninput: (ev: Event) => { this.query = (ev.target as HTMLInputElement).value; this.renderSheet(); },
    });
    input.value = this.query;
    return h("label", { class: "searchbar" }, icon("search", 22), input);
  }

  private typeLine(): HTMLElement {
    return h("span", { class: "type-line", id: "type-badge", title: this.demo ? "Demo unit" : "Set by the unit", "data-type": this.vt }, VEHICLE_LABEL[this.vt]);
  }

  private offlineView(): HTMLElement {
    return h("div", { class: "home locked" },
      h("div", { class: "offline-state" }, h("strong", {}, "Unit not connected"), h("span", {}, "Check that your AeroLink unit is powered and online.")),
      this.searchBar(true), this.typeLine(), this.footer());
  }

  /** Idle: the map and one question. The nearest destinations are listed; the rest appear when the field is used. */
  private isHome(): boolean {
    return !this.flight && this.online && this.ui === "home";
  }

  /** The home list's identity: its ids in order, the query and the message. Distances are not part of it; the sheet's class is checked on its own. */
  private homeKeyNow(): string {
    return JSON.stringify({ q: this.query.trim().toLowerCase(), m: this.message, ids: this.homeList().map((d) => d.id) });
  }

  /** The distances move with the unit (about once a second): when only they changed, patch the text and keep the rows. */
  private patchHomeDistances(cls: string): boolean {
    if (!this.homeKey || !this.isHome() || this.sheet.className !== cls || this.homeKeyNow() !== this.homeKey) return false;
    const list = this.homeList();
    const rows = this.sheet.querySelectorAll<HTMLElement>(".dest-row");
    if (rows.length !== list.length) return false;
    rows.forEach((row, i) => {
      const dist = row.querySelector(".dest-dist");
      if (dist) dist.textContent = list[i].dist === null ? "" : fmtKm(list[i].dist);
    });
    return true;
  }

  /** The destinations the home list shows: all of them, or the ones matching the query. */
  private homeList(): { id: string; name: string; dist: number | null }[] {
    const q = this.query.trim().toLowerCase();
    return this.destinations().filter((d) => !q || d.name.toLowerCase().includes(q) || d.id.includes(q));
  }

  private homeView(): HTMLElement[] {
    const list = this.homeList();
    return [h("div", { class: "home" },
      this.searchBar(false),
      this.message ? h("p", { class: "muted" }, this.message) : null,
      h("ul", { class: "dest-list", id: "dest-list" }, list.length ? list.map((d) =>
        h("li", {}, h("button", { class: "dest-row", type: "button", "data-id": d.id, onclick: () => void this.requestRoute(d.id) },
          h("span", { class: "dest-pin" }, icon("pin", 22)),
          h("span", { class: "dest-name", title: d.name, "data-full": d.name, "data-detail-title": "Destination" }, shortenName(d.name)), h("span", { class: "muted dest-dist" }, d.dist === null ? "" : fmtKm(d.dist))))) : h("li", { class: "muted pad" }, "No matching destinations")),
      this.typeLine(),
      this.footer())];
  }

  private optionsView(): (Node | null)[] {
    const back = h("button", { class: "round-btn", id: "back", type: "button", "aria-label": "Back", onclick: () => { this.ui = "home"; this.expanded = false; this.options = null; this.drawRoutes(); this.renderSheet(); } }, icon("back", 24));
    const head = h("div", { class: "sheet-head" }, back, h("h2", { title: this.destination ? this.nodeName(this.destination) : "", "data-full": this.destination ? this.nodeName(this.destination) : "", "data-detail-title": "Destination" }, this.destination ? this.shortName(this.destination) : "Routes"));
    if (this.loading) return [head, h("p", { class: "muted pad", id: "loading" }, "Finding routes")];
    const o = this.options;
    if (!o) return [head, h("p", { class: "pad", id: "no-route" }, this.message || "No route available.")];
    const badges = routeBadges(o.routes);
    return [
      head,
      this.stale ? h("div", { class: "stale-bar", id: "stale-bar" }, h("span", {}, "Out of date"), h("button", { class: "btn small", id: "refresh", type: "button", onclick: () => this.destination && void this.requestRoute(this.destination) }, "Refresh")) : null,
      this.message ? h("p", { class: "pad" }, this.message) : null,
      h("div", { class: `cards${this.stale ? " stale" : ""}`, id: "cards" }, o.routes.map((r) => {
        const b = badges.get(r.route_id);
        const last = r.eta[r.eta.length - 1];
        const sel = r.rank === this.selectedRank;
        return h("article", { class: `route-card${sel ? " selected" : ""}`, "data-rank": r.rank, onclick: () => { this.selectedRank = r.rank; this.drawRoutes(); this.renderSheet(); } },
          h("div", { class: "rc-top" },
            h("strong", { class: "rc-time" }, fmtDuration(predictedTime(r))),
            h("span", { class: "rc-badges" },
              b?.recommended ? h("span", { class: "chip recommended" }, "Recommended") : null,
              b?.fastest ? h("span", { class: "chip fastest" }, "Fastest") : null)),
          h("div", { class: "rc-line" },
            last ? h("span", { class: "rc-eta" }, `Arrive ${fmtClock(last.eta_p10)} to ${fmtClock(last.eta_p90)}`, " ", aiMark()) : null,
            this.trafficWord(r)),
          this.atcPill(this.atcOfRoute(r)),
          h("div", { class: "rc-more" },
            h("div", { class: "rc-reason muted" }, aiMark(), " ", r.reason),
            h("button", { class: "btn primary go", type: "button", "data-go": r.rank, disabled: this.stale || !this.online, onclick: (ev: Event) => { ev.stopPropagation(); void this.go(r, o); } }, "Go")));
      })),
      this.aiAdvisory(),
      this.footer(),
    ];
  }

  /** The requirement of an offered route; asks the server once per route (informational, never blocks Go). */
  private atcOfRoute(r: RouteOption): Atc | undefined {
    const known = this.routeAtc.get(r.route_id);
    if (!known && !this.routeAtcAsked.has(r.route_id)) {
      this.routeAtcAsked.add(r.route_id);
      pilotApi.atcRequirement(this.id, r.eta.map((x) => x.node)).then((a) => {
        this.routeAtc.set(r.route_id, { requirement: a.requirement, airspaces: a.airspaces, status: a.requirement === "none" ? "not_needed" : "pending" });
        this.renderSheet();
      }).catch(() => { this.routeAtcAsked.delete(r.route_id); });
    }
    return known;
  }

  /** The traffic word on a route card, from the legs (the AI's level at the ETA over each leg): the worst leg, ties to the
   *  earliest, named by where it is. All light (or no legs) reads "Light traffic"; without `segments`, the route's own level. */
  private trafficWord(r: RouteOption): HTMLElement {
    const segs = r.segments ?? [];
    if (!segs.length) return h("span", { class: `rc-traffic ${r.traffic}` }, `${TRAFFIC_LABEL[r.traffic]} traffic`);
    let worst = segs[0];
    for (const s of segs) if (LEVEL_RANK[s.level] > LEVEL_RANK[worst.level]) worst = s; // strictly worse: ties keep the earlier leg
    if (worst.level === "low") return h("span", { class: "rc-traffic low" }, "Light traffic");
    return h("span", { class: `rc-traffic ${worst.level}` }, `${TRAFFIC_LABEL[worst.level]} traffic near ${this.shortName(worst.to)}`);
  }

  /** One small line when the route crosses airspace that needs ATC; nothing otherwise. Never a gate: Go always works. */
  private atcPill(a: Atc | undefined): HTMLElement | null {
    if (!a || a.requirement === "none") return null;
    const done = a.status === "done";
    // "Ops noted" is what the operator recorded, never an ATC instruction; the radio line stays with it (L-FIX).
    const text = {
      clearance: done ? "Ops noted: ATC clearance obtained" : "Clearance needed (Class B)",
      contact_tower: done ? "Ops noted: tower contacted" : "Contact tower (Class D)",
      contact_approach: done ? "Ops noted: approach contacted" : "Contact approach (Class C)",
    }[a.requirement];
    return h("div", { class: `atc-pill ${done ? "done" : "pending"}`, "data-atc": a.requirement, "data-status": a.status, title: a.airspaces.join(", ") },
      text, done ? h("span", { class: "atc-sub" }, "Confirm with ATC by radio") : null);
  }

  private navigateView(): (Node | null)[] {
    const f = this.flight!;
    if (f.status === "ended") {
      return [h("div", { class: "arrived", id: "arrived" }, h("h2", {}, "Flight ended"), h("p", { class: "muted" }, `You've arrived at ${this.nodeName(f.destination)}.`), h("button", { class: "btn primary big", id: "done", type: "button", onclick: () => this.done() }, "Done"))];
    }
    const { next, nextIdx, nodes, eta } = this.progress();
    const destEta = eta.get(f.destination);
    const left = destEta ? Math.max(0, destEta.p50 - Date.now() / 1000) : null;
    const e = next ? eta.get(next) : undefined;
    const ahead = this.levelAhead(nodes, nextIdx);
    const s = this.suggestion();
    const p = this.pos();
    const pts = nodes.map((n) => this.airspace.nodes.get(n));
    const known = pts.every((x) => !!x) && pts.length > 1;
    const nextPt = next ? this.airspace.nodes.get(next) : undefined;
    const distNext = p && nextPt ? fmtKm(distanceM(p, nextPt)) : "";
    const distLeft = p && known ? fmtKm(remainingM(p, pts as LL[], nextIdx)) : "Unknown";
    // The cue arrow points to the next waypoint relative to where the nose is pointing.
    const rel = p && nextPt && this.fix ? bearingDeg(p, nextPt) - (this.fix.course_deg ?? 0) : 0;
    const banners: Node[] = [];
    const rr = this.pendingReroute();
    if (rr) banners.push(this.rerouteCard(rr));
    // "✦ AI" only when the AI detector flagged the deviation; the fixed rule shows the plain banner.
    if (f.status === "off_route") banners.push(h("div", { class: "banner off", id: "off-route" }, "Off route. Recalculating", f.detected_by === "ai" ? [" ", aiMark()] : null));
    if (s) {
      const rem = left ?? (this.navRoute ? this.navRoute.est_time_s - (Date.now() - this.flightSince) / 1000 : null);
      const save = rem === null ? null : Math.max(1, Math.round((rem - s.routes[0].est_time_s) / 60));
      const key = `${s.request_id}:${s.revision}`;
      banners.push(h("div", { class: "banner faster", id: "faster" },
        h("strong", {}, f.status === "off_route" || save === null ? "New route available" : `Faster route available, saves ${save} min`, " ", aiMark()),
        h("div", { class: "banner-actions" },
          h("button", { class: "btn primary", id: "accept", type: "button", onclick: () => void this.accept(s) }, "Accept"),
          h("button", { class: "btn", id: "ignore", type: "button", onclick: () => { this.dismissed.add(key); this.drawRoutes(); this.renderSheet(); } }, "Ignore"))));
    }
    const arrive = destEta ? fmtClock(destEta.p50) : "time unknown";
    // Full-screen navigation (CarPlay-style): the map fills the screen; one guidance card floats at the top and one
    // slim ETA bar with a red End floats at the bottom (`.nav-top` / `.nav-bottom`, positioned in pilot.css).
    const top = h("div", { class: "nav-top" },
      ...banners,
      h("div", { class: "nav-main", id: "nav" },
        h("span", { class: "cue" }, icon("navigation", 40, rel - 45)),
        h("div", { class: "nav-next" },
          h("strong", { id: "next-name", title: next ? this.nodeName(next) : "", "data-full": next ? this.nodeName(next) : "", "data-detail-title": "Next stop" }, next ? this.shortName(next) : "None"),
          h("span", { class: "muted", id: "next-eta" }, `${distNext ? `${distNext}, ` : ""}ETA ${e ? fmtClock(e.p50) : "unknown"}`))),
      this.atcPill(f.atc ?? (this.navRoute ? this.routeAtc.get(this.navRoute.route_id) : undefined)),
      // Silent unless it matters: light traffic or no data shows nothing; the id stays for tests and screen readers.
      h("div", { class: `traffic-ahead ${ahead && ahead !== "low" ? ahead : "quiet"}`, id: "traffic-ahead" }, ahead && ahead !== "low" ? `${TRAFFIC_LABEL[ahead]} traffic ahead` : ahead ? "Traffic ahead: Light" : "Traffic ahead: no data"));
    const bottom = h("div", { class: "nav-bottom" },
      this.footer(),
      this.details ? h("div", { class: "nav-details", id: "nav-details" },
        h("div", {}, h("span", { class: "muted" }, "To"), h("strong", {}, this.nodeName(f.destination))),
        h("div", {}, h("span", { class: "muted" }, "Distance left"), h("strong", {}, distLeft)),
        e ? h("div", {}, h("span", { class: "muted" }, "Next ETA range"), h("strong", {}, `${fmtClock(e.p10)} to ${fmtClock(e.p90)}`)) : null,
        h("div", {}, h("span", { class: "muted" }, "Flight"), h("strong", { id: "flight-chip", class: f.status }, flightLabel(f.status)))) : h("span", { class: "sr", id: "flight-chip" }, flightLabel(f.status)),
      this.message ? h("p", { class: "muted nav-msg" }, this.message) : null,
      h("div", { class: "nav-bar" },
        h("button", { class: "nav-strip", type: "button", "aria-expanded": String(this.details), "aria-label": "Trip details", onclick: () => { this.details = !this.details; this.renderSheet(); } },
          h("strong", { id: "dest-left" }, left === null ? "Unknown" : fmtDuration(left)),
          h("span", { class: "muted" }, `Arrive ${arrive}`)),
        // Red like CarPlay's End (user decision). A second tap confirms: no browser dialogs.
        h("button", { class: `end-btn${this.endArmedAt ? " armed" : ""}`, id: "end-btn", type: "button", "aria-label": this.endArmedAt ? "Tap again to end the flight" : "End flight", disabled: !this.online, onclick: () => void this.endTap() }, this.endArmedAt ? "Tap to end" : "End")));
    return [top, bottom];
  }

  // ---- AI identity and the one-time disclaimer (U-API contract §3) ------------------------------------------------
  private async loadAi(): Promise<void> {
    this.ai = await api.ai().catch(() => null);
    if (this.destroyed || !this.ai) return;
    this.maybeAiNotice();
  }

  /** Shown once after pairing, until this unit's pilot acknowledges the current disclaimer version. */
  private maybeAiNotice(): void {
    const d = this.ai?.disclaimer;
    if (!d || this.aiAck === d.version || this.root.querySelector("#ai-notice")) return;
    const notice = pilotText(d);
    const ok = h("button", { class: "btn primary big", id: "ai-ok", type: "button" }, "I understand");
    const err = h("p", { class: "pair-error", role: "alert" }); // filled only if the answer fails
    ok.addEventListener("click", () => {
      ok.disabled = true;
      pilotApi.aiAck(this.id, d.version).then(() => {
        this.aiAck = d.version;
        this.root.querySelector("#ai-notice")?.remove();
      }).catch((e) => {
        if (this.sessionLost(e)) return;
        ok.disabled = false;
        err.textContent = "Couldn't record that. Tap again.";
      });
    });
    this.root.append(h("section", { class: "ai-notice", id: "ai-notice", "aria-labelledby": "ai-notice-title" },
      h("div", { class: "ai-notice-card" },
        h("h2", { id: "ai-notice-title" }, h("span", { class: "ai-mark" }, lucideIcon(Sparkles)), " About AeroLink AI"),
        h("p", { id: "ai-notice-text" }, notice),
        ok, err)));
  }

  /** "How the AI works": the four models in one line each, the measured results from `/api/ai`, and what it can't do. */
  private showAiSheet(): void {
    if (this.root.querySelector("#ai-sheet")) return;
    const a = this.ai;
    if (!a) {
      // The AI details come from /api/ai: when it is down, say so and give the disclaimer as it is written.
      this.root.append(h("section", { class: "ai-notice", id: "ai-sheet", "aria-labelledby": "ai-sheet-title" },
        h("div", { class: "ai-notice-card" },
          h("div", { class: "sheet-head" }, h("h2", { id: "ai-sheet-title" }, aiMark(), " How the AI works"),
            h("button", { class: "round-btn", id: "ai-close", type: "button", "aria-label": "Close", onclick: () => this.root.querySelector("#ai-sheet")?.remove() }, icon("close", 22))),
          h("p", { id: "ai-unavailable", class: "muted" }, "AI details are unavailable right now."),
          h("p", { id: "ai-static-disclaimer" }, AI_FALLBACK_DISCLAIMER))));
      return;
    }
    const r = a.results;
    const row = (k: string, v: string) => h("div", { class: "st-row" }, h("span", { class: "muted" }, k), h("strong", {}, v));
    this.root.append(h("section", { class: "ai-notice", id: "ai-sheet", "aria-labelledby": "ai-sheet-title" },
      h("div", { class: "ai-notice-card" },
        h("div", { class: "sheet-head" }, h("h2", { id: "ai-sheet-title" }, aiMark(), " How the AI works"),
          h("button", { class: "round-btn", id: "ai-close", type: "button", "aria-label": "Close", onclick: () => this.root.querySelector("#ai-sheet")?.remove() }, icon("close", 22))),
        ...a.models.map((m) => h("div", { class: "ai-model" }, h("strong", {}, m.title ?? m.name), m.title ? h("span", { class: "muted ai-model-name" }, ` ${m.name}`) : null, h("span", { class: "muted" }, `: ${m.drives}`))),
        row("Measured: ETA error, AI vs classical", `${r.eta_error_ai_s} s vs ${r.eta_error_classical_s} s`),
        row("Arrival windows cover", `${Math.round(r.window_coverage * 100)}%`),
        row("Off-route precision / recall", `${r.offroute_precision.toFixed(2)} / ${r.offroute_recall.toFixed(2)}`),
        h("p", { class: "muted", id: "ai-honest" }, r.route_choice_note),
        h("p", { class: "muted" }, `${a.forecaster.name}, updated ${fmtClock(a.forecaster.updated_at)}`),
        this.aiAdvisory())));
  }

  /** The one persistent line under the route options and in the status sheet. */
  private aiAdvisory(): HTMLElement {
    return h("p", { class: "ai-line muted", id: "ai-advisory" }, aiMark(), " AI advisory. Not a clearance. You are pilot in command.");
  }

  // ---- ATC reroute (U-API contract §2): the controller approves, the pilot decides ----------------------------------
  /** The approved reroute still open for an answer (not past its expiry). */
  private pendingReroute(): Reroute | null {
    const r = this.reroute;
    if (!r || r.status !== "approved" || Date.parse(r.expires_at) < Date.now()) return null;
    return r;
  }

  /** The reroute's new route as a navigable option: its legs carry the AI's level at each ETA (segments). */
  private routeFromReroute(r: Reroute): RouteOption {
    const now = Date.now() / 1000;
    const segs = r.proposed.segments ?? [];
    const iso = (sec: number) => new Date(sec * 1000).toISOString();
    const last = segs.length ? segs[segs.length - 1].eta_s : now;
    return {
      route_id: r.proposed.route_id, rank: 1, label: "fastest", distance_m: 0, est_time_s: Math.max(0, last - now), expected_wait_s: 0,
      bunching_risk: 0, traffic: "low", cost_terms: { est_time_s: 0, expected_wait_s: 0, mu: 0, bunching_risk: 0 }, reason: r.reason,
      eta: segs.map((x) => ({ node: x.to, eta_p10: iso(x.eta_s), eta_p50: iso(x.eta_s), eta_p90: iso(x.eta_s) })),
      waypoints: r.proposed.waypoints as RouteOption["waypoints"], segments: segs,
    };
  }

  /** Acknowledge: the unit switches to the proposed route. Unable: the route stays and ATC is told. */
  private async answerReroute(answer: "acknowledge" | "unable"): Promise<void> {
    const r = this.pendingReroute();
    if (!r || !this.online) return;
    try {
      const res = answer === "acknowledge" ? await pilotApi.rerouteAck(this.id, r.id) : await pilotApi.rerouteUnable(this.id, r.id);
      this.reroute = null;
      if (answer === "acknowledge" && this.flight) {
        this.navRoute = this.routeFromReroute(res.reroute);
        this.flight = { ...this.flight, route_id: r.proposed.route_id, path: r.proposed.path, atc: undefined };
        this.flightSince = Date.now();
      }
      this.message = answer === "acknowledge" ? "" : "Route unchanged. Confirm with ATC by radio.";
      this.afterData();
      void this.refresh();
    } catch (e) {
      if (this.sessionLost(e)) return;
      if (e instanceof PilotError && e.code === "unit_offline") this.online = false;
      else if (e instanceof PilotError && e.status === 409) {
        this.reroute = null; // no longer open (answered elsewhere or expired)
        this.message = "That reroute is no longer open.";
        void this.refresh();
      } else this.message = "Couldn't send your answer. Try again.";
      this.renderAll();
    }
  }

  /** A details page (same overlay style as "How the AI works"): the full text of anything the screen had to cut off. */
  private showDetails(title: string, body: Node[]): void {
    this.root.querySelector("#detail-sheet")?.remove();
    this.root.append(h("section", { class: "ai-notice", id: "detail-sheet", "aria-labelledby": "detail-title" },
      h("div", { class: "ai-notice-card" },
        h("div", { class: "sheet-head" }, h("h2", { id: "detail-title" }, title),
          h("button", { class: "round-btn", id: "detail-close", type: "button", "aria-label": "Close", onclick: () => this.root.querySelector("#detail-sheet")?.remove() }, icon("close", 22))),
        ...body)));
  }

  /** Reroute details: the whole reason, the time saved, the requirement change and the radio line; the two answers work from here too. */
  private showRerouteDetails(r: Reroute): void {
    const min = Math.max(1, Math.round(r.gain_s / 60));
    const answer = (a: "acknowledge" | "unable") => { this.root.querySelector("#detail-sheet")?.remove(); void this.answerReroute(a); };
    const parts: (Node | null)[] = [
      h("p", { id: "detail-reason" }, aiMark(), " ", r.reason),
      h("p", { id: "detail-gain" }, r.gain_s > 0 ? `${min} min faster` : "New route"),
      r.requirement_change ? h("p", { id: "detail-req" }, `Changes your airspace requirement from ${REQ_LABEL[r.requirement_change.from]} to ${REQ_LABEL[r.requirement_change.to]}.`) : null,
      h("p", { class: "muted", id: "detail-sub" }, "Confirm with ATC by radio if your clearance changes"),
      h("div", { class: "rr-actions" },
        h("button", { class: "btn primary", id: "detail-accept", type: "button", onclick: () => answer("acknowledge") }, "Accept"),
        h("button", { class: "btn", id: "detail-decline", type: "button", onclick: () => answer("unable") }, "Decline")),
    ];
    this.showDetails("Reroute details", parts.filter((x): x is Node => !!x));
  }

  private cutHandlerOn = false;
  /** Names that are cut off (a clamp) get a chevron and open the details page with the full text. Measured without the cue, so it never flaps. */
  private markCutOff(): void {
    if (!this.cutHandlerOn) {
      this.cutHandlerOn = true;
      const open = (ev: Event) => {
        const el = (ev.target as HTMLElement).closest?.<HTMLElement>(".cut[data-full]");
        if (!el) return;
        ev.stopPropagation(); ev.preventDefault(); // a cut-off name inside a row must not also pick the row
        this.showDetails(el.dataset.detailTitle ?? "Details", [h("p", { id: "detail-full" }, el.dataset.full ?? "")]);
      };
      this.root.addEventListener("click", open, true);
      this.root.addEventListener("keydown", (ev) => { if ((ev as KeyboardEvent).key === "Enter") open(ev); }, true);
    }
    this.root.querySelectorAll<HTMLElement>("[data-full]").forEach((el) => {
      el.classList.remove("cut");
      const cut = el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1;
      const chev = el.querySelector(":scope > .cut-chev");
      if (cut) {
        el.classList.add("cut");
        el.setAttribute("role", "button"); el.tabIndex = 0;
        if (!chev) el.append(lucideIcon(ChevronRight, "cut-chev"));
      } else {
        chev?.remove(); el.removeAttribute("role"); el.removeAttribute("tabindex");
      }
    });
  }

  /** The card: calm, one line of what and why, and the two answers. Confirmation is by radio, said on the card. */
  private rerouteCard(r: Reroute): HTMLElement {
    const min = Math.max(1, Math.round(r.gain_s / 60));
    // The reason is one line with an ellipsis and a chevron; a tap opens the details sheet with the full text.
    const reason = h("div", { class: "rr-reason muted", id: "rr-reason", role: "button", tabindex: "0", "aria-label": "Reroute details", title: r.reason, onclick: () => this.showRerouteDetails(r) },
      h("span", { class: "rr-reason-text" }, aiMark(), " ", r.reason), lucideIcon(ChevronRight, "cut-chev"));
    return h("div", { class: "reroute", id: "reroute" },
      h("div", { class: "rr-head" }, h("strong", {}, "Ops reroute (advisory)")),
      h("div", { class: "rr-line" }, r.gain_s > 0 ? `${min} min faster` : "New route"),
      reason,
      r.requirement_change ? h("div", { class: "rr-req", id: "rr-req" }, `Changes your airspace requirement from ${REQ_LABEL[r.requirement_change.from]} to ${REQ_LABEL[r.requirement_change.to]}.`) : null,
      h("div", { class: "rr-sub muted" }, "Confirm with ATC by radio if your clearance changes"),
      h("div", { class: "rr-actions" },
        h("button", { class: "btn primary", id: "rr-ack", type: "button", onclick: () => void this.answerReroute("acknowledge") }, "Accept"),
        h("button", { class: "btn", id: "rr-unable", type: "button", onclick: () => void this.answerReroute("unable") }, "Decline")));
  }

  private async accept(s: Resp): Promise<void> {
    if (!this.online) return;
    const r = s.routes[0];
    try {
      const fs = await pilotApi.routeSelection(this.id, s.request_id, r.route_id);
      this.dismissed.add(`${s.request_id}:${s.revision}`);
      this.navRoute = r;
      if (this.flight) {
        this.flight = { ...this.flight, flight_id: fs.flight_id, status: fs.status === "planned" ? "planned" : fs.status, route_id: r.route_id, path: r.eta.map((x) => x.node), atc: undefined };
      }
      this.flightSince = Date.now();
      this.afterData();
      void this.refresh();
    } catch (e) {
      if (this.sessionLost(e)) return;
      if (e instanceof PilotError && e.code === "unit_offline") this.online = false;
      else this.message = "Couldn't switch routes.";
      this.renderAll();
    }
  }

  /** The status rows as [id, label, value]. The sheet is built from these once; while open only the values change. */
  private statusRows(): [string, string, string][] {
    const f = this.fix;
    const inRegion = f ? f.lon >= FOCUS_BOUNDS[0][0] && f.lon <= FOCUS_BOUNDS[1][0] && f.lat >= FOCUS_BOUNDS[0][1] && f.lat <= FOCUS_BOUNDS[1][1] : null;
    const age = this.latestResponse ? Math.max(0, Math.round((Date.now() - Date.parse(this.latestResponse.forecast_time)) / 1000)) : null;
    // A feed that is not "ok" never shows aircraft as current (U-API contract §10).
    const adsb = this.adsbState !== "ok" ? "Traffic data unavailable" : this.adsbAt ? `Updated ${Math.max(0, Math.round((Date.now() - this.adsbAt) / 1000))} s ago.` : "Waiting for feed.";
    return [
      ["st-online", "Unit", `${this.id}, ${this.online ? "Online" : "Offline"}`],
      ["st-adsb", "Internet ADS-B, delayed and advisory", adsb],
      ["st-gnss", "GNSS fix", f ? `${f.fix.type}${f.fix.hdop !== undefined ? `, HDOP ${f.fix.hdop}` : ""}${f.fix.satellites !== undefined ? `, ${f.fix.satellites} sats` : ""}` : "No fix yet"],
      ["st-region", "Region check", inRegion === null ? "Unknown" : inRegion ? "Inside the network area" : "Outside the network area"],
      ["st-age", "Last routes", age === null ? "None yet" : `${fmtDuration(age)} ago`],
      ["st-graph", "Network version", this.latestResponse?.graph_version ?? "Unknown"],
      ["st-seen", "Last heard", this.lastSeen ? fmtClock(this.lastSeen) : "Unknown"],
    ];
  }

  /** Build the whole status sheet (opening it, and the buttons inside it). */
  private renderStatus(): void {
    const row = (k: string, v: string, id: string) => h("div", { class: "st-row" }, h("span", { class: "muted" }, k), h("strong", { id }, v));
    const seg = (id: string, label: string, on: boolean, fn: () => void) => h("button", { class: `seg${on ? " on" : ""}`, id, type: "button", "aria-pressed": String(on), onclick: fn }, label);
    clear(this.statusPanel);
    this.statusPanel.append(
      h("div", { class: "sheet-head" }, h("h2", {}, "Unit status"), h("button", { class: "round-btn", id: "status-close", type: "button", "aria-label": "Close", onclick: () => { this.showStatus = false; this.statusPanel.hidden = true; } }, icon("close", 22))),
      ...(this.callsign ? [row("Callsign", this.callsign, "st-callsign")] : []),
      ...this.statusRows().map(([id, k, v]) => row(k, v, id)),
      this.aiAdvisory(),
      h("div", { class: "st-credit" }, this.credit()),
      this.protoNote("proto-note-status"),
      h("button", { class: "btn", id: "ai-how", type: "button", onclick: () => this.showAiSheet() }, aiMark(), " How the AI works"),
      h("div", { class: "seg-row" }, h("span", { class: "muted" }, "Map"),
        h("div", { class: "segs" }, seg("heading-north", "North up", !this.headingUp, () => this.setHeadingUp(false)), seg("heading-up", "Heading up", this.headingUp, () => this.setHeadingUp(true)))),
      h("div", { class: "seg-row" }, h("span", { class: "muted" }, "Theme"),
        h("div", { class: "segs" },
          seg("theme-auto", "Auto", this.themeChoice === "auto", () => { this.setTheme("auto"); this.renderStatus(); }),
          seg("theme-day", "Day", this.themeChoice === "day", () => { this.setTheme("day"); this.renderStatus(); }),
          seg("theme-night", "Night", this.themeChoice === "night", () => { this.setTheme("night"); this.renderStatus(); }))),
    );
  }

  /** While the sheet is open the timer and the feeds only change the values: rows, buttons and taps stay where they are. */
  private refreshStatusValues(): void {
    if (this.statusPanel.hidden) return;
    if (!this.statusPanel.querySelector("#st-online")) { this.renderStatus(); return; }
    for (const [id, , v] of this.statusRows()) {
      const el = this.statusPanel.querySelector<HTMLElement>(`#${id}`);
      if (el && el.textContent !== v) el.textContent = v;
    }
  }
}

const keep = (xs: (Node | null)[]): Node[] => xs.filter((x): x is Node => !!x);

/** The one small AI mark (U-plan §D): on predictions and AI reasons only. Decorative; the words around it say what it is. */

/** Night from 19:00 to 06:00 local time unless the pilot chose. */
function autoTheme(now = new Date()): Theme {
  const hr = now.getHours();
  return hr >= 19 || hr < 6 ? "night" : "day";
}

/** The position marker: a blue disc with a white ring and a small heading wedge on top (north = up, rotated by course). */
function puckImage(color: string): { width: number; height: number; data: Uint8ClampedArray } {
  const n = 96, c = document.createElement("canvas");
  c.width = c.height = n;
  const g = c.getContext("2d")!;
  g.translate(n / 2, n / 2);
  g.fillStyle = color; g.strokeStyle = "#ffffff"; g.lineWidth = 5; g.lineJoin = "round";
  g.beginPath(); g.moveTo(0, -42); g.lineTo(16, -20); g.lineTo(-16, -20); g.closePath(); g.fill(); // wedge
  g.shadowColor = "rgba(0,0,0,0.3)"; g.shadowBlur = 6;
  g.beginPath(); g.arc(0, 0, 20, 0, Math.PI * 2); g.fill(); g.stroke();
  const d = g.getImageData(0, 0, n, n);
  return { width: n, height: n, data: d.data };
}

function flightLabel(s: string): string {
  return s === "planned" ? "Planned" : s === "active" ? "Active" : s === "off_route" ? "Off route" : s === "ended" ? "Ended" : s;
}

export async function startApp(root: HTMLElement, id: string, onSessionEnd: () => void): Promise<Controller> {
  const p = new Pilot(root, id, onSessionEnd);
  await p.start();
  return p;
}

/** The airspace-requirement pill labels (contract A), used in the reroute card's requirement change. */
const REQ_LABEL: Record<AtcRequirement, string> = { none: "No ATC", contact_tower: "Contact tower", contact_approach: "Contact approach", clearance: "Clearance needed" };

/** The disclaimer as the server writes it (U-API contract §3), shown when /api/ai cannot be reached. */
const AI_FALLBACK_DISCLAIMER = "AeroLink is a prototype and is advisory only. It is not certified or approved by the FAA, and it is not an air traffic control service or a collision-avoidance system. Its AI predicts traffic and suggests routes, and it can be wrong or out of date. Traffic from other aircraft comes from a public feed that can be incomplete or delayed, so an empty screen does not mean the sky is empty. You, as pilot in command, remain responsible for the safe operation of your aircraft: keep looking outside and follow the clearances and instructions ATC gives you directly. A message in AeroLink, including an \"ATC reroute\", is not a clearance. AeroLink records your unit's position, routes and answers and shows them to ATC operators.";

/** The pilot's text: `disclaimer.pilot` from /api/ai, else the older `text` field. */
const pilotText = (d: { pilot?: string; text: string }): string => d.pilot ?? d.text;

/** The ADS-B chevron: a small arrow pointing up (north); the layer rotates it to the track. */
function chevronImage(): { width: number; height: number; data: Uint8ClampedArray } {
  const n = 64, c = document.createElement("canvas");
  c.width = c.height = n;
  const g = c.getContext("2d")!;
  g.beginPath(); g.moveTo(32, 6); g.lineTo(54, 54); g.lineTo(32, 42); g.lineTo(10, 54); g.closePath();
  g.fillStyle = "#5f6368"; g.strokeStyle = "#ffffff"; g.lineWidth = 4; g.lineJoin = "round";
  g.fill(); g.stroke();
  const d = g.getImageData(0, 0, n, n);
  return { width: n, height: n, data: d.data };
}
