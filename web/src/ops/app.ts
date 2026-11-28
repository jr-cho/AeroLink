/**
 * The ATC dashboard (`/ops`, ARCHITECTURE §13.4) in the CarPlay Maps style (E6.14): a light map, a left sidebar of
 * every active flight with its ATC requirement, and three quiet corner buttons (alerts, system status, settings).
 * It never sends anything to an aircraft; ATC speaks to the pilot by radio. Writes: acknowledging alerts, marking a
 * flight's ATC requirement handled (the pilot sees it, informational), and the existing system settings.
 * Needs an operator login (G2): `index.ts` checks it before mounting; a 401 at any time returns to `/login`.
 */
import { ScaleControl, type Map as MlMap } from "maplibre-gl";
import { clear, h } from "../shared/dom.ts";
import { FOCUS_BOUNDS, addAirspaceLayers, createMap, fitTo, splitAirspace } from "../shared/map.ts";
import { TRAFFIC_COLOR } from "../shared/colors.ts";
import { applyCarplayTheme, styleOverlays, type Theme } from "../shared/mapStyles/carplay.ts";
import type { Method } from "../shared/types.ts";
import { ops, openOpsSocket, type OpsMessage } from "./api.ts";
import { addOpsLayers, type ConflictMark, type OpsLayers, type Sev } from "./maplayers.ts";
import { alertsTab, conflictsTab, fleetTab, systemTab, type Actions, type Ops } from "./panels.ts";
import { isUnauthorized, signOut, toLogin, watchExpiry, whoami, type Operator } from "./session.ts";
import { flightsPanel, overviewAge, visibleReroutes } from "./sidebar.ts";
import { createArrivals } from "./timeline.ts";
import { getAppearance, resolveTheme, saveAppearance, type Appearance } from "./theme.ts";
import type { AiInfo, AlertRow, Congestion, Conflict, Reroute, Source } from "./types.ts";
import { ago, aiMark, call, conflictPoint, learnCallsigns, pathCoords, predictedLevel, routeCoords, sig, wpCoords } from "./util.ts";
import { fmtFt, fmtKt } from "../shared/format.ts";
import { icon, Sparkles, X } from "../shared/icon.ts";
import { closeFullText } from "./fulltext.ts";
import "./ops.css";

const OVERVIEW_KEY = "aerolink.ops.overviewCollapsed";
/** The overview header's collapsed state, remembered per browser. Storage blocked: expanded. */
function readOverviewCollapsed(): boolean {
  try { return localStorage.getItem(OVERVIEW_KEY) === "1"; } catch { return false; }
}

type Panel = "alerts" | "status" | "gear" | "ai" | null;
type AlertTab = "alerts" | "conflicts";
type StatusTab = "fleet" | "system";

function failure(e: unknown): string {
  if (isUnauthorized(e)) { toLogin(true); return "Your session ended."; }
  return (e as Error).message;
}

async function whoamiOrLeave(): Promise<void> {
  if ((await whoami()) === null) toLogin(true);
}

export async function mount(root: HTMLElement, who: Operator | null = null): Promise<void> {
  if (who) watchExpiry(who.expires_at);
  clear(root);
  let appearance: Appearance = getAppearance();
  let theme: Theme = resolveTheme(appearance);
  root.className = `ops ${theme}`;
  const mapEl = h("div", { class: "map", id: "map" });
  root.append(mapEl);
  const { map, hasBasemap } = await createMap(mapEl);
  const geo = await ops.airspace().catch(() => ({ type: "FeatureCollection", features: [] }) as GeoJSON.FeatureCollection);
  const airspace = splitAirspace(geo);
  addAirspaceLayers(map, airspace, hasBasemap);
  const layers: OpsLayers = addOpsLayers(map, theme);
  map.addControl(new ScaleControl({ unit: "nautical", maxWidth: 90 }), "bottom-right");
  map.fitBounds(FOCUS_BOUNDS, { padding: { top: 40, bottom: 40, left: 380, right: 40 }, duration: 0 });

  const o: Ops = {
    airspace, routes: [], detail: false, popOpen: false, overview: null, overviewCollapsed: readOverviewCollapsed(), overviewAllBullets: false, conflicts: null, state: [], adsb: null, devices: [], alerts: [], watchdogs: [], traffic: null, models: null, scenarios: null,
    method: "aerolink", forecaster: "cv", filter: "all", selected: null, selectedConflict: null,
    track: [], whatif: null, whatifError: null, whatifShown: null, operator: who?.name || who?.username || "", ackError: "", markError: "", markBusy: false, reroutes: [], collapsedReroutes: new Set<string>(), rerouteBusy: new Set<string>(), rerouteError: {}, wsUp: false, settingsNote: "",
  };
  const show: Record<Source, boolean> = { aircraft: true, sim: true, adsb: true };
  // AI traffic prediction is always on (UI-AI-PLAN A): the corridors show the forecast for +15 min until ATC moves the slider.
  const ui = { panel: null as Panel, alertTab: "alerts" as AlertTab, statusTab: "fleet" as StatusTab, openAlertsOnly: true, minutes: 15, congestion: true, congestionData: null as Congestion | null };

  function paintTheme(): void {
    applyCarplayTheme(map, theme, hasBasemap);
    styleOverlays(map, theme);
    // the ATC map keeps its sites as small neutral dots; the network legs are drawn quietly
    if (map.getLayer("sites")) {
      map.setLayoutProperty("sites", "visibility", "visible");
      map.setFilter("sites", ["!=", ["get", "site"], "waypoint"]);
      map.setPaintProperty("sites", "circle-radius", ["interpolate", ["linear"], ["zoom"], 7, 3, 12, 5]);
      map.setPaintProperty("sites", "circle-color", theme === "day" ? "#ffffff" : "#262b32");
      map.setPaintProperty("sites", "circle-stroke-color", theme === "day" ? "#8e9aa8" : "#95a0ad");
      map.setPaintProperty("sites", "circle-stroke-width", 1.5);
    }
    layers.paint(theme);
    root.className = `ops ${theme}`;
  }
  paintTheme();

  // ---- actions -----------------------------------------------------------------------------------
  const act: Actions = {
    select(id) {
      o.selected = id;
      o.detail = false; // a selection opens the popover; "Details" opens the detail (R-contract §6.4)
      o.popOpen = id !== null;
      arrivals.paint(o); // the selected tag is highlighted in the timeline too
      paintPop();
      o.selectedConflict = null; // a conflict's line is shown while it is selected, not after the user moves on
      o.track = [];
      o.whatif = o.whatifError = o.whatifShown = null;
      o.markError = "";
      layers.setTrack([]);
      layers.setWhatIf([]);
      side.render();
      drawRoute();
      drawTraffic();
      drawReroutes(); // the preview follows the selected flight's card
      if (id) {
        const r = o.routes.find((x) => x.flight_id === id);
        if (r) fitTo(map, routeCoords(r), { top: 80, bottom: 80, left: 380, right: padRight() });
        void loadDetail(id);
        void fetchFlightOv(id);
      }
    },
    selectConflict(c: Conflict) {
      o.selectedConflict = c.id;
      const at = conflictPoint(c, airspace);
      if (at) map.flyTo({ center: at, zoom: Math.max(map.getZoom(), 10), duration: 600 });
      renderPanel();
    },
    previewWhatIf(i) {
      o.whatifShown = i;
      const w = o.whatif;
      layers.setWhatIf(i !== null && w ? [pathCoords(airspace, w.alternatives[i].path)] : []);
      side.render();
    },
    async ack(id) {
      o.ackError = "";
      try { mergeAlert(await ops.ack(id)); } catch (e) { o.ackError = `Could not acknowledge: ${failure(e)}`; }
      renderPanel();
      renderCorner();
    },
    async mark(flightId, action) {
      o.markError = "";
      o.markBusy = true;
      side.render();
      try {
        const atc = await ops.mark(flightId, action);
        const r = o.routes.find((x) => x.flight_id === flightId);
        if (r) r.atc = atc;
      } catch (e) {
        o.markError = `Could not save: ${failure(e)}`;
      }
      o.markBusy = false;
      side.render();
      drawRoute();
      renderCorner();
    },
    focus(id) {
      const ac = o.state.find((a) => a.id === id);
      const ad = o.adsb?.aircraft.find((a) => a.icao === id);
      const n = airspace.nodes.get(id);
      const at: [number, number] | null = ac ? [ac.lon, ac.lat] : ad ? [ad.lon, ad.lat] : n ? [n.lon, n.lat] : null;
      if (at) map.flyTo({ center: at, zoom: Math.max(map.getZoom(), 10.5), duration: 600 });
    },
    async setMethod(m: Method) {
      o.settingsNote = "Switching"; renderPanel();
      try { await ops.setMethod(m); o.method = m; o.settingsNote = ""; } catch (e) { o.settingsNote = `Failed: ${failure(e)}`; }
      renderPanel();
    },
    async setForecaster(f: string) {
      o.settingsNote = "Loading model"; renderPanel();
      try { await ops.setForecaster(f); o.forecaster = f; o.settingsNote = ""; } catch (e) { o.settingsNote = `Failed: ${failure(e)}`; }
      renderPanel();
    },
    async approveReroute(id) {
      if (o.rerouteBusy.has(id)) return;
      o.rerouteBusy.add(id);
      delete o.rerouteError[id];
      side.render();
      try { mergeReroute(await ops.approveReroute(id)); } catch (e) { o.rerouteError[id] = `Could not send: ${failure(e)}`; }
      o.rerouteBusy.delete(id);
      side.render();
    },
    async dismissReroute(id) {
      delete o.rerouteError[id];
      try { mergeReroute(await ops.dismissReroute(id)); } catch (e) { o.rerouteError[id] = `Could not dismiss: ${failure(e)}`; side.render(); }
    },
    toggleOverview() {
      o.overviewCollapsed = !o.overviewCollapsed;
      try { localStorage.setItem(OVERVIEW_KEY, o.overviewCollapsed ? "1" : "0"); } catch { /* not remembered; still applied */ }
      side.render();
    },
    toggleOverviewMore() { o.overviewAllBullets = !o.overviewAllBullets; side.render(); },
    details(flightId) {
      o.detail = true;
      o.popOpen = false;
      side.render();
      paintPop();
      if (flightId !== o.selected) act.select(flightId);
    },
    closePop() { o.popOpen = false; paintPop(); },
    async setScenario(s: string) {
      o.settingsNote = "Switching scenario"; renderPanel();
      try { await ops.setScenario(s); o.scenarios = { scenarios: o.scenarios?.scenarios ?? [s], current: s }; o.settingsNote = ""; } catch (e) { o.settingsNote = `Failed: ${failure(e)}`; }
      renderPanel();
    },
  };

  // ---- AI reroute proposals (contract §2): cards in the sidebar, the proposed route dashed on the map -------------
  const RANK: Record<string, number> = { proposed: 0, approved: 1, acknowledged: 2, unable: 2, expired: 2, dismissed: 2 };
  const collapseTimers = new Map<string, ReturnType<typeof setTimeout>>();
  const fitted = new Set<string>();
  /** Store a reroute. A stale "proposed" from a poll never overwrites a status the socket already moved past. */
  function upsertReroute(rr: Reroute): void {
    const i = o.reroutes.findIndex((x) => x.id === rr.id);
    if (i >= 0) {
      if (RANK[o.reroutes[i].status] > RANK[rr.status]) return;
      o.reroutes[i] = rr;
    } else o.reroutes.push(rr);
    if (rr.status === "dismissed") o.collapsedReroutes.add(rr.id);
    else if (rr.status !== "proposed" && rr.status !== "approved" && !collapseTimers.has(rr.id)) {
      // a finished card stays 10 s so the controller reads the answer, then it lives on only in the flight's records
      collapseTimers.set(rr.id, setTimeout(() => { o.collapsedReroutes.add(rr.id); collapseTimers.delete(rr.id); refreshReroutes(); }, 10_000));
    }
  }
  /** After the socket comes back: open reroutes again, and the history of every flight we track a reroute for. */
  async function resyncReroutes(): Promise<void> {
    try {
      for (const rr of (await ops.reroutes()).reroutes) upsertReroute(rr);
      for (const f of new Set(o.reroutes.map((rr) => rr.flight_id))) for (const rr of await ops.rerouteHistory(f)) upsertReroute(rr);
      refreshReroutes();
    } catch { /* the poll tries again */ }
  }
  function refreshReroutes(): void {
    side.render();
    drawReroutes();
    arrivals.paint(o);
  }
  function mergeReroute(rr: Reroute): void {
    upsertReroute(rr);
    refreshReroutes();
  }
  /** One preview at a time: the hovered or focused card, else the selected flight's, else the top card. */
  function previewReroute(): Reroute | undefined {
    const open = visibleReroutes(o).filter((rr) => rr.status === "proposed" || rr.status === "approved");
    const focused = (document.activeElement as HTMLElement | null)?.closest?.("[data-reroute]")?.getAttribute("data-reroute") ?? null;
    const pick = (id: string | null) => (id ? open.find((rr) => rr.id === id) : undefined);
    return pick(hoverReroute) ?? pick(focused) ?? open.find((rr) => rr.flight_id === o.selected) ?? open[0];
  }
  let hoverReroute: string | null = null;
  /** The preview (current solid, proposed dashed) for one card only. The camera moves once per card, when it first appears. */
  function drawReroutes(): void {
    const shown = visibleReroutes(o).filter((rr) => rr.status === "proposed" || rr.status === "approved");
    const pv = previewReroute();
    layers.setReroute(pv ? [wpCoords(pv.current.waypoints)] : [], pv ? [wpCoords(pv.proposed.waypoints)] : []);
    const fresh = shown.filter((rr) => !fitted.has(rr.id));
    if (fresh.length) {
      for (const rr of fresh) fitted.add(rr.id);
      fitTo(map, fresh.flatMap((rr) => [...wpCoords(rr.current.waypoints), ...wpCoords(rr.proposed.waypoints)]), { top: 80, bottom: 80, left: 380, right: padRight() });
    }
  }

  let alertsPrimed = false;
  const unacked = () => o.alerts.filter((a) => !a.acked_at && a.kind !== "alert_ack" && a.kind !== "clearance");
  function mergeAlert(row: AlertRow, notify = false): void {
    // reroute events come pre-acknowledged and belong to the reroute cards, never to the alert list or the bell (contract §6)
    if (row.kind === "reroute") return;
    const i = o.alerts.findIndex((a) => a.id === row.id);
    if (i >= 0) o.alerts[i] = row;
    else {
      o.alerts.unshift(row);
      if (notify && alertsPrimed && !row.acked_at && (row.severity === "critical" || /offline|emergency/.test(row.kind))) interrupt(row.severity === "critical" ? "danger" : "warn", row.message);
    }
    o.alerts.sort((a, b) => b.id - a.id);
  }

  const toast = h("div", { class: "toast", id: "interrupt", role: "alert", "aria-live": "assertive", hidden: true });
  let toastTimer: ReturnType<typeof setTimeout> | undefined;
  function interrupt(kind: "danger" | "warn", text: string): void {
    clear(toast);
    toast.className = `toast ${kind}`;
    toast.append(h("span", {}, text), h("button", { type: "button", class: "link", onclick: () => { ui.panel = "alerts"; syncPanels(); toast.hidden = true; } }, "Show"));
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 12_000);
  }

  // ---- sidebar and corner buttons ----------------------------------------------------------------
  const side = flightsPanel(o, act);
  // the arrivals timeline (right column, R2 checkpoint 2); below 1280 px it opens over the map from the "Arrivals" button
  const arrivals = createArrivals(act);
  let arrivalsOpen = false;
  const arrBtn = h("button", { type: "button", class: "round arr-btn", id: "arr-btn", "aria-pressed": "false", onclick: () => {
    arrivalsOpen = !arrivalsOpen;
    arrivals.el.classList.toggle("open", arrivalsOpen);
    arrBtn.setAttribute("aria-pressed", String(arrivalsOpen));
  } }, "Arrivals");
  /** The right edge the map's fit must clear: the timeline column on wide screens, the corner buttons otherwise. */
  const padRight = () => (innerWidth >= 1600 ? 440 : innerWidth >= 1280 ? 400 : 80);
  const bellCount = h("span", { class: "badge-n", id: "bell-count", hidden: true });
  const bell = h("button", { type: "button", class: "round", id: "bell-btn", "aria-label": "Alerts", "aria-pressed": "false", onclick: () => toggle("alerts") }, bellIcon(), bellCount);
  const dot = h("button", { type: "button", class: "round", id: "status-btn", "aria-label": "System status", "aria-pressed": "false", onclick: () => toggle("status") }, h("i", { class: "dot" }), h("span", { class: "sr", id: "ind-live" }, "Live"));
  const aiBtn = h("button", { type: "button", class: "round ai-round", id: "ai-btn", "aria-label": "How the AI works", "aria-pressed": "false", onclick: () => toggle("ai") }, icon(Sparkles));
  const gear = h("button", { type: "button", class: "round", id: "gear-btn", "aria-label": "Settings", "aria-pressed": "false", onclick: () => toggle("gear") }, gearIcon());
  const corner = h("div", { class: "corner" }, arrBtn, bell, dot, aiBtn, gear);
  // the one quiet line that says the AI is live (UI-AI-PLAN C/D): the forecaster and how old its forecast is
  const aiChip = h("div", { class: "ai-chip", id: "ai-chip", role: "status", hidden: true });
  // The disclaimer (UI-AI-PLAN D3): once per session and version, one tap to acknowledge; then a quiet footer line.
  let ackedVersion: string | null = who?.ai_ack_version ?? null;
  const ackText = h("p", { id: "ai-ack-text" });
  const ackError = h("p", { class: "err small", id: "ai-ack-error", role: "alert" });
  const ackBtn = h("button", { type: "button", class: "btn primary", id: "ai-ack-btn", onclick: () => void acknowledgeAi() }, "I understand");
  const disclaimer = h("section", { class: "ai-ack", id: "ai-ack", role: "dialog", "aria-modal": "true", "aria-labelledby": "ai-ack-title", hidden: true },
    h("h2", { id: "ai-ack-title" }, "AI in AeroLink"), ackText, ackError, h("div", { class: "row" }, ackBtn));
  const aiFooter = h("footer", { class: "ai-footer", id: "ai-footer" }, icon(Sparkles), " AI-assisted predictions. Verify before acting. Ops is not ATC.");
  root.append(disclaimer, aiFooter);
  const panel = h("section", { class: "popover", id: "popover", hidden: true });
  const status = h("div", { class: "status-pill", id: "status-pill", hidden: true }, "Backend not reachable. Retrying.");
  const attribution = h("footer", { class: "attribution" },
    h("span", { class: "proto-note", id: "proto-note" }, "Prototype. Not certified. Not for flight. Not ATC."), " ",
    "Aircraft data: ", h("a", { href: "https://adsb.lol", target: "_blank", rel: "noopener" }, "adsb.lol"), " ", h("span", { id: "adsb-attribution" }, "(ODbL)"), ". Airspace: FAA ADDS. Map data from ",
    h("a", { href: "https://www.openstreetmap.org/copyright", target: "_blank", rel: "noopener" }, "OpenStreetMap"), " and ", h("a", { href: "https://protomaps.com", target: "_blank", rel: "noopener" }, "Protomaps"));
  const pop = h("section", { class: "fl-pop", id: "flight-pop", "aria-label": "Flight", hidden: true });
  root.append(side.el, arrivals.el, corner, panel, status, toast, aiChip, attribution, pop);
  // The flight popover (R2 CP3): anchored to the aircraft, two-sentence AI overview from the flight endpoint, no AI text on 503
  const flightOv = new Map<string, string | null>();
  let popSig = "";
  function paintPop(): void {
    const r = o.selected ? o.routes.find((x) => x.flight_id === o.selected) : undefined;
    const show = !!r && o.popOpen && !o.detail;
    pop.hidden = !show;
    if (!r || !show) { popSig = ""; return; }
    const ov = flightOv.get(r.flight_id) ?? null;
    const p = r.position;
    const key = JSON.stringify([r.flight_id, call(r.aircraft_id), r.path_names, p ? [fmtFt(p.alt_msl_m), fmtKt(p.ground_speed_mps)] : null, ov]);
    if (key !== popSig) { // rebuilt only when something on it changed, so a poll does not move the buttons under the pointer
      popSig = key;
      clear(pop);
      pop.append(
        h("div", { class: "fp-head" }, h("b", { class: "fp-cs" }, call(r.aircraft_id)), h("span", { class: "fp-kv" }, p ? `${fmtFt(p.alt_msl_m)}, ${fmtKt(p.ground_speed_mps)}` : "no live position")),
        h("p", { class: "fp-route", id: "pop-route" }, `${r.path_names[0] ?? ""} to ${r.path_names[r.path_names.length - 1] ?? ""}`),
        h("div", { class: "fp-btns" },
          h("button", { type: "button", class: "btn primary", id: "pop-details", onclick: () => act.details(r.flight_id) }, "Details"),
          h("button", { type: "button", class: "btn", id: "pop-close", "aria-label": "Close", onclick: () => act.closePop() }, icon(X))),
        ...(ov ? [h("div", { class: "fp-ai", id: "pop-ai" }, h("span", { class: "fp-ai-h" }, aiMark(), " overview"), h("p", { class: "fp-text" }, ov))] : []));
    }
    placePop();
  }
  /** Beside the aircraft (or the route's start when it has no live position), kept clear of the sidebar and the timeline. */
  function placePop(): void {
    const r = o.selected ? o.routes.find((x) => x.flight_id === o.selected) : undefined;
    if (!r || pop.hidden) return;
    const at: [number, number] = r.position ? [r.position.lon, r.position.lat] : [r.waypoints[0][1], r.waypoints[0][0]];
    const q = map.project(at);
    const W = mapEl.clientWidth, H = mapEl.clientHeight;
    const maxLeft = W - (innerWidth >= 1600 ? 432 : innerWidth >= 1280 ? 392 : 16) - 260;
    pop.style.left = `${Math.min(Math.max(372, q.x + 18), Math.max(372, maxLeft))}px`;
    pop.style.top = `${Math.min(Math.max(16, q.y - 40), Math.max(16, H - 200))}px`;
  }
  map.on("move", placePop);
  addEventListener("resize", () => { arrivals.paint(o); placePop(); });
  async function fetchFlightOv(id: string): Promise<void> {
    try { flightOv.set(id, (await ops.flightOverview(id)).text || null); } catch (e) {
      if (isUnauthorized(e)) toLogin(true);
      flightOv.set(id, null); // 503 overview_unavailable, 404, or unreachable: no AI text (R-contract §11.3)
    }
    paintPop();
  }

  function toggle(p: Exclude<Panel, null>): void { ui.panel = ui.panel === p ? null : p; syncPanels(); }
  function syncPanels(): void {
    panel.hidden = ui.panel === null;
    panel.dataset.panel = ui.panel ?? "";
    for (const [b, p] of [[bell, "alerts"], [dot, "status"], [aiBtn, "ai"], [gear, "gear"]] as const) b.setAttribute("aria-pressed", String(ui.panel === p));
    renderPanel();
  }

  /** Overall health for the status dot: red when something is broken, amber when degraded or the live feed is down. */
  function healthTone(): "ok" | "warn" | "bad" {
    if (o.watchdogs.some((w) => w.state === "fail") || o.devices.some((d) => d.status === "offline" && d.online === false && d.age_s !== null && d.age_s < 600)) return "bad";
    if (!o.wsUp || o.watchdogs.some((w) => w.state === "warn") || o.devices.some((d) => d.status === "degraded") || (o.traffic as { stale?: boolean } | null)?.stale) return "warn";
    return "ok";
  }
  function renderCorner(): void {
    const n = unacked().length;
    bellCount.hidden = n === 0;
    bellCount.textContent = String(n);
    dot.className = `round tone-${healthTone()}`;
    const live = dot.querySelector("#ind-live");
    if (live) live.textContent = o.wsUp ? "Live" : "Live feed lost";
    if (ui.panel === "alerts" || ui.panel === "status") refreshPanel();
  }

  const badgeFor = (n: number | string) => (n ? h("span", { class: "tab-n" }, String(n)) : null);
  /** Background refreshes (polls, live feeds) must not rebuild the panel while the controller is using a dropdown or a
   *  field in it: rebuilding destroys an open <select> (it snapped shut every poll). Defer until focus leaves the panel. */
  let panelStale = false;
  function refreshPanel(): void {
    const el = document.activeElement;
    if (el && panel.contains(el) && (el.tagName === "SELECT" || el.tagName === "INPUT")) { panelStale = true; return; }
    renderPanel();
  }
  panel.addEventListener("focusout", () => {
    // focusout fires before the next element gets focus: check on the next tick.
    setTimeout(() => { if (panelStale && !panel.contains(document.activeElement)) { panelStale = false; renderPanel(); } }, 0);
  });
  function renderPanel(): void {
    if (ui.panel === null) return;
    panelStale = false;
    const top = panel.scrollTop;
    clear(panel);
    const tab = (id: string, label: string, on: boolean, n: number | string, fn: () => void) =>
      h("button", { type: "button", role: "tab", class: `tab${on ? " on" : ""}`, id: `tab-${id}`, "aria-selected": String(on), onclick: fn }, label, badgeFor(n));
    if (ui.panel === "alerts") {
      panel.append(h("div", { class: "tabs", role: "tablist" },
        tab("alerts", "Alerts", ui.alertTab === "alerts", unacked().length, () => { ui.alertTab = "alerts"; renderPanel(); }),
        tab("conflicts", "Conflicts", ui.alertTab === "conflicts", o.conflicts?.conflicts.length ?? 0, () => { ui.alertTab = "conflicts"; renderPanel(); })));
      panel.append(ui.alertTab === "alerts" ? alertsTab(o, act, ui.openAlertsOnly, () => { ui.openAlertsOnly = !ui.openAlertsOnly; renderPanel(); }) : conflictsTab(o, act));
    } else if (ui.panel === "status") {
      const bad = o.devices.filter((d) => d.status !== "healthy").length;
      const wd = o.watchdogs.some((w) => w.state === "fail") ? "!" : o.watchdogs.some((w) => w.state === "warn") ? "~" : "";
      panel.append(h("div", { class: "tabs", role: "tablist" },
        tab("fleet", "Units", ui.statusTab === "fleet", bad, () => { ui.statusTab = "fleet"; renderPanel(); }),
        tab("system", "System", ui.statusTab === "system", wd, () => { ui.statusTab = "system"; renderPanel(); })),
        h("p", { class: `small live ${o.wsUp ? "muted" : "err"}` }, o.wsUp ? "Live alert channel connected." : "Live feed lost: reconnecting."));
      panel.append(ui.statusTab === "fleet" ? fleetTab(o) : systemTab(o, act));
    } else if (ui.panel === "ai") {
      panel.append(aiSheet());
    } else {
      panel.append(settingsView());
    }
    panel.scrollTop = top;
  }

  const horizonText = (min: number) => (min === 0 ? "Now" : `in ${min} min`);
  const slider = h("input", { type: "range", id: "traffic-slider", min: 0, max: 45, step: 5, value: ui.minutes, "aria-label": "Forecast time offset in minutes", oninput: () => {
    ui.minutes = Number(slider.value);
    label.textContent = horizonText(ui.minutes);
    drawCongestion();
  } });
  const label = h("output", { id: "traffic-slider-label", for: "traffic-slider" }, horizonText(ui.minutes));
  // The ✦ legend: the one line that says the colours are a model's forecast, for which horizon, and what the colours mean.
  const legendText = h("span", { id: "ai-legend-text" }, "");
  const legend = h("div", { class: "ai-legend", id: "ai-legend", role: "note", hidden: true },
    h("span", { class: "ai-mark", "aria-hidden": "true" }, icon(Sparkles)), legendText,
    h("i", { class: "sw", style: { background: TRAFFIC_COLOR.moderate }, "aria-hidden": "true" }), "Moderate",
    h("i", { class: "sw", style: { background: TRAFFIC_COLOR.heavy }, "aria-hidden": "true" }), "Heavy");
  root.append(legend);
  function settingsView(): HTMLElement {
    const seg = (id: Appearance, text: string) => h("button", { type: "button", class: `seg${appearance === id ? " on" : ""}`, id: `ap-${id}`, "aria-pressed": String(appearance === id), onclick: () => setAppearance(id) }, text);
    const tg = (id: string, text: string, on: boolean, fn: (v: boolean) => void) => {
      const c = h("input", { type: "checkbox", id, checked: on, onchange: () => fn(c.checked) });
      return h("label", { class: "tg" }, c, text);
    };
    return h("div", { id: "gear-panel" },
      h("h3", {}, "Appearance"),
      h("div", { class: "segs", role: "group", "aria-label": "Appearance" }, seg("light", "Light"), seg("dark", "Dark"), seg("auto", "Auto")),
      h("h3", {}, "On the map"),
      tg("tg-sim", "Simulated aircraft", show.sim, (v) => { show.sim = v; drawTraffic(); }),
      tg("tg-adsb", "ADS-B traffic", show.adsb, (v) => { show.adsb = v; drawTraffic(); }),
      tg("tg-congestion", "Congestion forecast", ui.congestion, (v) => { ui.congestion = v; drawCongestion(); }),
      h("div", { class: "row" }, h("span", { class: "ctl-label" }, "Forecast"), slider, label),
      h("h3", {}, "Signed in"),
      h("div", { class: "row" }, h("span", { id: "operator-name", title: who ? `Signed in as ${who.username}` : "" }, o.operator || "Operator"),
        h("button", { type: "button", class: "btn", id: "logout", onclick: () => void signOut() }, "Sign out")));
  }
  function setAppearance(a: Appearance): void {
    appearance = a;
    saveAppearance(a);
    applyTheme();
    renderPanel();
  }
  function applyTheme(): void {
    const t = resolveTheme(appearance);
    if (t === theme) return;
    theme = t;
    paintTheme();
    drawTraffic();
    drawRoute();
  }
  setInterval(() => { if (appearance === "auto") applyTheme(); }, 60_000);

  // ---- How the AI works (UI-AI-PLAN D2): the four models in a line each, the measured results, the honest note -----
  function aiSheet(): HTMLElement {
    const a = aiInfo;
    if (!a) return h("div", { id: "ai-sheet" }, h("h3", {}, "How the AI works"), h("p", { class: "muted small" }, aiFailed ? "The AI details are not reachable right now." : "Loading"));
    const r = a.results;
    return h("div", { id: "ai-sheet" },
      h("h2", { class: "ai-title" }, "How the AI works"),
      h("ul", { class: "plain small", id: "ai-models" }, ...a.models.map((m) => h("li", { class: "ai-model", "data-model": m.key },
        h("b", {}, m.title), " ", h("span", { class: "muted" }, m.name), h("br", {}), `${m.what} `, h("span", { class: "muted" }, `Feeds: ${m.drives}`)))),
      h("h3", {}, "Measured on the committed evaluations"),
      h("dl", { class: "kv small", id: "ai-results" },
        h("dt", {}, "ETA error"), h("dd", {}, `${r.eta_error_ai_s} s with AI, ${r.eta_error_classical_s} s classical`),
        h("dt", {}, "ETA windows"), h("dd", {}, `contain the real arrival ${Math.round(r.window_coverage * 100)}% of the time`),
        h("dt", {}, "Off route"), h("dd", {}, `precision ${r.offroute_precision.toFixed(2)}, recall ${r.offroute_recall.toFixed(2)}`)),
      h("p", { class: "small muted", id: "ai-route-note" }, r.route_choice_note),
      h("p", { class: "small muted" }, `Forecaster ${a.forecaster.name} ${a.forecaster.version}`));
  }

  // ---- drawing -----------------------------------------------------------------------------------
  const selectedRoute = () => o.routes.find((r) => r.flight_id === o.selected);
  /** The conflict under the pointer (sidebar or panel row), if any: its connecting line is shown while hovered. */
  let hoverConflict: string | null = null;
  root.addEventListener("mouseover", (e) => {
    const t = e.target as HTMLElement;
    const id = t.closest?.("[data-conflict]")?.getAttribute("data-conflict") ?? null;
    if (id !== hoverConflict) { hoverConflict = id; drawTraffic(); }
    const card = t.closest?.("[data-reroute]")?.getAttribute("data-reroute") ?? null;
    if (card !== hoverReroute) { hoverReroute = card; drawReroutes(); }
  });
  // keyboard focus on a card moves the preview to it too
  root.addEventListener("focusin", () => drawReroutes());
  root.addEventListener("focusout", () => setTimeout(drawReroutes, 0));
  function drawTraffic(): void {
    layers.setTraffic(o.state, o.adsb?.aircraft ?? [], { show, theme, selectedAircraft: selectedRoute()?.aircraft_id ?? null, names: call });
    // Conflicts at rest: a red ring at the predicted point and an outline on each aircraft. The line between the two
    // aircraft is drawn only for the selected or hovered conflict, or one that involves the selected flight.
    const pos = new Map<string, [number, number]>();
    for (const a of o.state) pos.set(a.id, [a.lon, a.lat]);
    for (const a of o.adsb?.aircraft ?? []) pos.set(a.icao, [a.lon, a.lat]);
    const focus = selectedRoute()?.flight_id ?? null;
    const rings: ConflictMark[] = [];
    const aircraft: ConflictMark[] = [];
    const lines: { coords: [number, number][]; sev: Sev }[] = [];
    for (const c of o.conflicts?.conflicts ?? []) {
      const ids = c.kind === "adsb" ? [c.flights[0]?.aircraft_id, c.adsb?.icao] : c.flights.slice(0, 2).map((f) => f.aircraft_id);
      const [p, q] = ids.map((i) => (i ? pos.get(i) : undefined));
      if (!p || !q) continue;
      const sev = c.severity;
      aircraft.push({ at: p, sev }, { at: q, sev });
      rings.push({ at: conflictPoint(c, airspace) ?? [(p[0] + q[0]) / 2, (p[1] + q[1]) / 2], sev });
      const shown = c.id === o.selectedConflict || c.id === hoverConflict || (!!focus && c.flights.some((f) => f.flight_id === focus));
      if (shown) lines.push({ coords: [p, q], sev });
    }
    layers.setConflicts({ rings, aircraft, lines });
  }
  function drawRoute(): void {
    const r = selectedRoute() ?? null;
    layers.setRoute(r, r ? routeCoords(r) : [], r ? r.atc.legs.map((l) => pathCoords(airspace, l)) : []);
    const cone = r ? (o.traffic?.cones?.features ?? []).find((f) => f.properties?.flight_id === r.flight_id) : undefined;
    layers.setCone(cone ?? null);
  }
  function drawCongestion(): void {
    const step = ui.minutes / 5;
    const on = ui.congestion && !!o.traffic;
    // the AI's forecast at the chosen minute: only moderate and heavy corridors are drawn, a low one stays plain
    const feats: GeoJSON.Feature[] = [];
    if (on) {
      const edges = o.traffic!.edges;
      for (const f of airspace.corridors.features) {
        const p = f.properties as Record<string, string>;
        const lvl = predictedLevel(edges[`${p.from}->${p.to}`] ?? edges[`${p.to}->${p.from}`], ui.minutes);
        if (lvl === "moderate" || lvl === "heavy") feats.push({ type: "Feature", properties: { level: lvl }, geometry: f.geometry });
      }
    }
    layers.setCongestion(feats);
    layers.setSiteLoad(ui.congestion ? ui.congestionData?.sites ?? [] : [], step, airspace);
    legend.hidden = !on;
    legendText.textContent = `AI forecast, ${ui.minutes === 0 ? "now" : `in ${ui.minutes} min`}`;
    const steps = (o.traffic as { cone_steps?: { flight_id: string; steps: { min: number; p50: [number, number] }[] }[] } | null)?.cone_steps ?? [];
    layers.setPredicted(!ui.congestion || ui.minutes === 0 ? [] : steps.flatMap((c) => {
      const s = c.steps.find((x) => x.min === ui.minutes);
      return s ? [{ id: c.flight_id, at: [s.p50[1], s.p50[0]] as [number, number] }] : [];
    }));
  }

  async function loadDetail(id: string): Promise<void> {
    const r = o.routes.find((x) => x.flight_id === id);
    if (!r) return;
    // the flight's reroute records, all statuses (history), merged with what the socket has told us
    ops.rerouteHistory(r.flight_id).then((rows) => { for (const rr of rows) upsertReroute(rr); refreshReroutes(); }).catch(() => {});
    const [track, wi] = await Promise.allSettled([ops.telemetry(r.aircraft_id), ops.whatif(r.flight_id)]);
    if (o.selected !== id) return;
    if (track.status === "fulfilled") {
      o.track = track.value;
      const segs: [number, number][][] = [];
      let last: unknown = Symbol();
      for (const p of o.track) {
        if (p.segment_id !== last) { segs.push([]); last = p.segment_id; }
        segs[segs.length - 1].push([p.lon, p.lat]);
      }
      layers.setTrack(segs);
    }
    if (wi.status === "fulfilled") { o.whatif = wi.value; o.whatifError = null; } else o.whatifError = "Alternatives are not available for this flight right now.";
    side.render();
  }

  // ---- map clicks --------------------------------------------------------------------------------
  map.on("click", (e) => {
    const t = map.queryRenderedFeatures([[e.point.x - 10, e.point.y - 10], [e.point.x + 10, e.point.y + 10]], { layers: ["ops-traffic", "ops-ground"].filter((l) => map.getLayer(l)) })[0];
    if (t) {
      const r = o.routes.find((x) => x.aircraft_id === (t.properties as Record<string, string>).id);
      if (r) { act.select(r.flight_id); return; }
    }
    if (ui.panel) { ui.panel = null; syncPanels(); }
  });
  map.on("mousemove", (e) => {
    const f = map.queryRenderedFeatures(e.point, { layers: ["ops-traffic"].filter((l) => map.getLayer(l)) });
    map.getCanvas().style.cursor = f.length ? "pointer" : "";
  });
  addEventListener("keydown", (e) => {
    const tag = (e.target as HTMLElement | null)?.tagName;
    if (e.key !== "Escape" || tag === "INPUT" || tag === "SELECT") return;
    if (closeFullText()) return; // the full-text panel closes first
    // Esc closes the popover only; the selection stays (R-contract §6.4)
    if (ui.panel) { ui.panel = null; syncPanels(); } else if (o.popOpen && !o.detail) act.closePop();
  });

  // ---- data --------------------------------------------------------------------------------------
  let online = true;
  const sigs = new Map<string, string>();
  const changed = (key: string, v: unknown): boolean => {
    const s = sig(v);
    if (sigs.get(key) === s) return false;
    sigs.set(key, s);
    return true;
  };
  const poll = (fn: () => Promise<void>, ms: number) => {
    let running = false;
    const tick = async () => {
      if (running) return;
      running = true;
      try { await fn(); online = true; } catch (e) { if (isUnauthorized(e)) toLogin(true); online = false; }
      status.hidden = online;
      running = false;
    };
    void tick();
    return setInterval(tick, ms);
  };

  poll(async () => {
    const s = await ops.state();
    o.state = s.aircraft;
    learnCallsigns(s.aircraft);
    o.method = s.method;
    drawTraffic();
  }, 1000);
  poll(async () => {
    o.adsb = await ops.adsb();
    // the feed credit is shown once, as "Aircraft data: adsb.lol (ODbL)", not repeated from the feed text
    drawTraffic();
    if (ui.panel === "status" && changed("adsb", o.adsb.feed)) refreshPanel();
  }, 5000);
  poll(async () => {
    o.routes = await ops.routes();
    learnCallsigns(o.routes);
    arrivals.paint(o);
    if (o.selected && !o.routes.some((r) => r.flight_id === o.selected)) act.select(null);
    drawRoute();
    if (changed("routes", o.routes.map((r) => ({ ...r, position: null, cone: null })))) { side.render(); renderCorner(); }
  }, 3000);
  let conflictsPrimed = false;
  const seenConflicts = new Set<string>();
  poll(async () => {
    o.conflicts = await ops.conflicts(45);
    for (const c of o.conflicts.conflicts) {
      if (!seenConflicts.has(c.id) && conflictsPrimed && c.severity === "high") interrupt("danger", `Conflict: ${c.flights.map((f) => call(f.aircraft_id)).join(" / ")}${c.adsb ? ` / ${c.adsb.callsign || c.adsb.icao}` : ""}`);
      seenConflicts.add(c.id);
    }
    conflictsPrimed = true;
    drawTraffic();
    if (changed("conflicts", o.conflicts)) { side.render(); refreshPanel(); }
  }, 3000);
  poll(async () => {
    o.traffic = await ops.trafficFull();
    drawRoute();
    drawCongestion();
    if (changed("traffic-age", [(o.traffic as { stale?: boolean }).stale])) renderCorner();
    if (ui.panel === "status" && ui.statusTab === "system") refreshPanel();
  }, 5000);
  poll(async () => { ui.congestionData = await ops.congestion(); drawCongestion(); }, 10_000);
  poll(async () => {
    o.devices = await ops.devices();
    learnCallsigns(o.devices);
    if (changed("devices", o.devices)) renderCorner();
  }, 3000);
  let aiInfo: AiInfo | null = null;
  let aiFetchedAt = 0;
  let aiFailed = false;
  function paintAiChip(): void {
    if (!aiInfo) { aiChip.hidden = !aiFailed; aiChip.replaceChildren(aiMark(), " forecast unavailable"); return; }
    const age = aiInfo.forecaster.forecast_age_s + (Date.now() - aiFetchedAt) / 1000;
    aiChip.hidden = false;
    aiChip.replaceChildren(aiMark(), ` forecast live, updated ${ago(age)}, ${aiInfo.forecaster.name}`);
  }
  /** Show the disclaimer until this version is acknowledged. The first showing takes the focus (keyboard users), then it stays put. */
  function paintDisclaimer(): void {
    const v = aiInfo?.disclaimer;
    const need = !!v && ackedVersion !== v.version;
    const wasHidden = disclaimer.hidden;
    if (v) ackText.textContent = v.ops ?? v.text;
    disclaimer.hidden = !need;
    if (need && wasHidden) ackBtn.focus({ preventScroll: true });
  }
  async function acknowledgeAi(): Promise<void> {
    const v = aiInfo?.disclaimer.version;
    if (!v) return;
    ackError.textContent = "";
    try { await ops.ackAi(v); ackedVersion = v; paintDisclaimer(); } catch (e) { ackError.textContent = `Could not record that: ${failure(e)}`; }
  }
  const fetchAi = () => ops.ai().then((a) => { aiInfo = a; aiFetchedAt = Date.now(); aiFailed = false; paintAiChip(); paintDisclaimer(); if (ui.panel === "ai") refreshPanel(); }).catch(() => { aiFailed = true; paintAiChip(); });
  void fetchAi();
  setInterval(fetchAi, 10_000);
  setInterval(paintAiChip, 1000);

  poll(async () => {
    const d = await ops.reroutes();
    if (changed("reroutes", d.reroutes)) {
      for (const rr of d.reroutes) upsertReroute(rr);
      refreshReroutes();
    }
  }, 3000);
  // The AI overview (R-contract §1): replaced only when its content changes, so the card never rebuilds under the pointer
  // for nothing; its age line ticks every second on its own. A missing endpoint hides the card, never the status pill.
  const fetchOverview = async (): Promise<void> => {
    try {
      const ov = await ops.overview();
      // R-contract §12.1: a response without bullets is bad data, so the card stays hidden
      if (!ov.bullets.length) { if (o.overview) { o.overview = null; side.render(); } return; }
      if (changed("overview", ov)) { o.overview = ov; side.render(); }
    } catch (e) {
      if (isUnauthorized(e)) toLogin(true);
      // 404 (older backend) or 503 overview_unavailable (R-contract §11.3): hide the card, as if it never came
      else if (o.overview) { o.overview = null; side.render(); }
    }
  };
  void fetchOverview();
  setInterval(fetchOverview, 15_000);
  setInterval(() => arrivals.paint(o), 5_000);
  setInterval(() => { if (o.selected) void fetchFlightOv(o.selected); }, 15_000); // the window moves with the clock, so the lanes are re-placed every few seconds
  setInterval(() => {
    const el = document.getElementById("ov-age");
    if (el && o.overview) el.textContent = overviewAge(o.overview);
  }, 1000);
  poll(async () => {
    const d = await ops.deepHealth();
    if (d.watchdogs && changed("wd", d.watchdogs)) { o.watchdogs = d.watchdogs; renderCorner(); }
  }, 5000);
  poll(async () => {
    const m = await ops.modelsFull();
    o.models = m;
    o.forecaster = m.forecaster;
    o.method = m.method;
    if (changed("models", m) && ui.panel) refreshPanel(); // a background refresh never rebuilds an open dropdown
  }, 10_000);
  void ops.scenarios().then((s) => { o.scenarios = s; if (ui.panel) refreshPanel(); }).catch(() => {});
  void ops.alerts(100).then((rows) => { for (const r of rows) mergeAlert(r); }).catch(() => {}).finally(() => { alertsPrimed = true; renderCorner(); });
  setInterval(() => { if (o.selected) void loadDetail(o.selected); }, 10_000);

  let socketDropped = false;
  openOpsSocket((m: OpsMessage) => {
    if (m.type === "state") {
      for (const a of m.payload.unacked) mergeAlert(a);
      if (m.payload.watchdogs.length) o.watchdogs = m.payload.watchdogs;
    } else if (m.type === "alert") mergeAlert(m.payload, true);
    else if (m.type === "ack") mergeAlert(m.payload);
    else if (m.type === "reroute") mergeReroute(m.payload);
    else if (m.type === "watchdog") o.watchdogs = [...o.watchdogs.filter((w) => w.name !== m.payload.name), m.payload].sort((a, b) => a.name.localeCompare(b.name));
    else if (m.type === "clearance") {
      const r = o.routes.find((x) => x.flight_id === m.payload.flight_id);
      if (r) { r.atc = m.payload.atc; side.render(); drawRoute(); }
    } else return;
    renderCorner();
  }, (up) => {
    o.wsUp = up;
    if (!up) socketDropped = true;
    else if (socketDropped) { socketDropped = false; void resyncReroutes(); }
    renderCorner();
  }, () => { void whoamiOrLeave(); });

  side.render();
  renderCorner();
  (window as unknown as { __ops?: { map: MlMap } }).__ops = { map };
}

const svg = (d: string) => {
  const el = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  el.setAttribute("viewBox", "0 0 24 24"); el.setAttribute("width", "22"); el.setAttribute("height", "22");
  el.setAttribute("fill", "none"); el.setAttribute("stroke", "currentColor"); el.setAttribute("stroke-width", "2"); el.setAttribute("stroke-linecap", "round"); el.setAttribute("stroke-linejoin", "round");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("d", d);
  el.append(p);
  return el;
};
const bellIcon = () => svg("M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.94 1.94 0 0 0 3.4 0");
const gearIcon = () => svg("M4 6h10M18 6h2M4 12h2M10 12h10M4 18h12M20 18h0M14 4v4M8 10v4M16 16v4");
