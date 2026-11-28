/** The web map page (`/`): full-bleed map with floating cards, like a maps app. */
import { api, isUnauthorized, operatorSignedIn } from "../shared/api.ts";
import { clear, h } from "../shared/dom.ts";
import {
  AircraftLayer, FOCUS_BOUNDS, addAirspaceLayers, addConeLayer, addPlannedLayer, addRouteLayers, colorCorridors, createMap, splitAirspace,
} from "../shared/map.ts";
import type { Method } from "../shared/types.ts";
import { ADSB_POLL_MS, AdsbLayer, type AdsbResponse } from "./adsb.ts";
import { levelAt, notify, type Ctx } from "./context.ts";
import { mountDirections } from "./directions.ts";
import { flightsPanel, impactPanel, layersPanel, medicalPanel, scenarioPanel, type Panel } from "./panels.ts";
import "./webmap.css";

const FORECASTERS = ["cv", "chronos", "aerolink-gbm"];

export async function mount(root: HTMLElement): Promise<void> {
  clear(root);
  root.className = "webmap";
  const mapEl = h("div", { class: "map", id: "map" });
  root.append(mapEl);
  const { map, hasBasemap } = await createMap(mapEl);
  const geo = await api.airspace().catch(() => ({ type: "FeatureCollection", features: [] }) as GeoJSON.FeatureCollection);
  const airspace = splitAirspace(geo);
  addAirspaceLayers(map, airspace, hasBasemap);
  addRouteLayers(map);
  const aircraft = new AircraftLayer(map);
  addConeLayer(map);
  addPlannedLayer(map);
  const adsb = new AdsbLayer(map);
  map.fitBounds(FOCUS_BOUNDS, { padding: { top: 60, bottom: 60, left: 380, right: 60 }, duration: 0 });

  const ctx: Ctx = { map, hasBasemap, airspace, aircraft, state: null, traffic: null, prevTraffic: null, flights: [], health: null, method: "aerolink", forecaster: "cv", online: true, operator: true, listeners: new Set(), adsb: { layer: adsb, ok: false } };
  let trafficOn = true;
  let minutes = 0;

  // ---- traffic colouring and time slider -------------------------------------------------------
  const slider = h("input", { type: "range", id: "traffic-slider", min: 0, max: 45, step: 5, value: 0, "aria-label": "Forecast time offset in minutes", oninput: () => { minutes = Number(slider.value); applyTraffic(); } });
  const sliderLabel = h("output", { id: "traffic-slider-label", for: "traffic-slider" }, "Now");
  const sliderNote = h("span", { class: "muted small", id: "traffic-note" });
  const timebar = h("section", { class: "card timebar", "aria-label": "Traffic forecast time" },
    h("span", { class: "tb-title" }, "Traffic"), slider, sliderLabel, sliderNote);

  function applyTraffic(): void {
    sliderLabel.textContent = minutes === 0 ? "Now" : `in ${minutes} min`;
    timebar.hidden = !trafficOn;
    if (!trafficOn || !ctx.traffic) {
      colorCorridors(map, airspace, null);
      return;
    }
    const edges = ctx.traffic.edges;
    let inexact = false;
    colorCorridors(map, airspace, (a, b) => {
      const r = levelAt(edges[`${a}->${b}`] ?? edges[`${b}->${a}`], minutes);
      if (!r.exact) inexact = true;
      return r.level;
    });
    sliderNote.textContent = inexact ? "Forecast horizon ends sooner; showing the furthest available." : "";
  }

  // ---- method and forecaster toggles -----------------------------------------------------------
  const directions = mountDirections(ctx);
  const methodBtns = (["classical", "aerolink"] as Method[]).map((m) =>
    h("button", { type: "button", class: "seg", "data-method": m, "aria-pressed": "false", onclick: () => setMethod(m) }, m === "aerolink" ? "AeroLink" : "Classical"));
  const methodNote = h("span", { class: "tb-note", role: "status" });
  const forecasterSel = h("select", { id: "forecaster", "aria-label": "Forecaster", onchange: () => setForecaster(forecasterSel.value) },
    ...FORECASTERS.map((f) => h("option", { value: f }, f)));
  const forecasterNote = h("span", { class: "tb-note", id: "forecaster-note", role: "status" });

  /** One quiet line, shared by the toolbar and the scenario panel, shown only while signed out. */
  const signInLine = () => h("a", { class: "signin-note", href: "/login?next=/" }, "Operator sign-in required");
  const authNote = signInLine();
  authNote.hidden = true;
  function applyAuth(): void {
    methodBtns.forEach((b) => { (b as HTMLButtonElement).disabled = !ctx.operator; });
    forecasterSel.disabled = !ctx.operator;
    authNote.hidden = ctx.operator;
    notify(ctx);
    if (openPanel === "scenario") panels.find((p) => p.id === "scenario")?.refresh();
  }
  function signedOut(): void {
    ctx.operator = false;
    applyAuth();
  }
  function syncToggles(): void {
    methodBtns.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.method === ctx.method)));
    if (![...forecasterSel.options].some((o) => o.value === ctx.forecaster)) forecasterSel.append(h("option", { value: ctx.forecaster }, ctx.forecaster));
    forecasterSel.value = ctx.forecaster;
  }
  async function setMethod(m: Method): Promise<void> {
    if (m === ctx.method) return;
    methodNote.textContent = "Switching";
    try {
      await api.setMethod(m);
      ctx.method = m;
      methodNote.textContent = "";
      syncToggles();
      await directions.replan();
    } catch (e) {
      if (isUnauthorized(e)) { methodNote.textContent = ""; return signedOut(); }
      methodNote.textContent = `Failed: ${(e as Error).message}`;
    }
  }
  async function setForecaster(f: string): Promise<void> {
    forecasterSel.disabled = true;
    forecasterNote.textContent = "Loading model";
    forecasterNote.classList.add("loading");
    try {
      await api.setForecaster(f);
      ctx.forecaster = f;
      forecasterNote.textContent = "";
      await directions.replan();
    } catch (e) {
      if (isUnauthorized(e)) { forecasterNote.textContent = ""; signedOut(); }
      else forecasterNote.textContent = `Failed: ${(e as Error).message}`;
    } finally {
      forecasterNote.classList.remove("loading");
      forecasterSel.disabled = !ctx.operator;
      syncToggles();
    }
  }
  const controls = h("section", { class: "card controls", "aria-label": "Method and forecaster" },
    h("div", { class: "ctl" }, h("span", { class: "ctl-label" }, "Routing method"), h("div", { class: "seg-row", id: "method-toggle", role: "group", "aria-label": "Routing method" }, methodBtns), methodNote),
    h("div", { class: "ctl" }, h("label", { class: "ctl-label", for: "forecaster" }, "Forecaster"), forecasterSel, forecasterNote),
    authNote);

  // ---- panels -----------------------------------------------------------------------------------
  const panels: Panel[] = [
    layersPanel(ctx, { onTraffic: (on) => { trafficOn = on; applyTraffic(); }, onAdsb: (on) => { adsb.setVisible(on); } }),
    flightsPanel(ctx), medicalPanel(ctx), impactPanel(ctx), scenarioPanel(ctx, signInLine, signedOut),
  ];
  let openPanel: string | null = null;
  const body = h("div", { class: "panel-host", hidden: true });
  const tabs = panels.map((p) => h("button", { type: "button", class: "tab", id: `tab-${p.id}`, "aria-pressed": "false", onclick: () => toggle(p.id) }, p.title));
  function toggle(id: string): void {
    openPanel = openPanel === id ? null : id;
    tabs.forEach((t, i) => t.setAttribute("aria-pressed", String(panels[i].id === openPanel)));
    show();
  }
  function show(): void {
    body.hidden = openPanel === null;
    clear(body);
    const p = panels.find((x) => x.id === openPanel);
    if (p) {
      body.append(p.el);
      p.refresh();
    }
  }
  const dock = h("aside", { class: "card dock", "aria-label": "Panels" }, h("div", { class: "tabs", role: "toolbar" }, tabs), body);

  // ---- alerts -----------------------------------------------------------------------------------
  const alertList = h("div", { class: "alert-list", id: "alert-list", hidden: true });
  const alertBtn = h("button", { type: "button", class: "alert-chip", id: "alert-chip", "aria-expanded": "false", onclick: () => { alertList.hidden = !alertList.hidden; alertBtn.setAttribute("aria-expanded", String(!alertList.hidden)); } }, "No alerts");
  const alerts = h("div", { class: "alerts" }, alertBtn, alertList);
  function updateAlerts(): void {
    const items: { text: string; kind: string; lon: number; lat: number }[] = [];
    for (const { state: a, kind } of aircraft.summary()) {
      if (kind === "ground") continue; // parked aircraft raise no alerts
      const [[w, s], [e, n]] = FOCUS_BOUNDS;
      if (a.off_route) items.push({ kind: "off-route", text: `${a.callsign || a.id} is off route`, lon: a.lon, lat: a.lat });
      if (kind === "lost") items.push({ kind: "lost", text: `${a.callsign || a.id} lost (no signal)`, lon: a.lon, lat: a.lat });
      if (a.lon < w || a.lon > e || a.lat < s || a.lat > n) items.push({ kind: "out-of-region", text: `${a.callsign || a.id} is outside the Central Florida region`, lon: a.lon, lat: a.lat });
    }
    alertBtn.textContent = items.length ? `${items.length} alert${items.length === 1 ? "" : "s"}` : "No alerts";
    alertBtn.classList.toggle("has", items.length > 0);
    clear(alertList);
    for (const it of items) alertList.append(h("button", { type: "button", class: `alert-item ${it.kind}`, onclick: () => map.flyTo({ center: [it.lon, it.lat], zoom: Math.max(map.getZoom(), 10) }) }, it.text));
  }

  // ---- footer -----------------------------------------------------------------------------------
  const adsbCredit = h("span", { id: "adsb-credit" }, "Aircraft data: ", h("a", { href: "https://adsb.lol", target: "_blank", rel: "noopener" }, "adsb.lol"), " (ODbL)");
  const footer = h("footer", { class: "attribution" },
    h("span", { class: "proto-note", id: "proto-note" }, "Prototype. Not certified. Not for flight. Not ATC."), " ",
    adsbCredit, ". Airspace: FAA ADDS. Map data from ",
    h("a", { href: "https://www.openstreetmap.org/copyright", target: "_blank", rel: "noopener" }, "OpenStreetMap"), " and ",
    h("a", { href: "https://protomaps.com", target: "_blank", rel: "noopener" }, "Protomaps"));
  const status = h("div", { class: "status-pill", id: "status-pill", hidden: true }, "Backend not reachable. Retrying.");
  const tilt = h("button", { type: "button", class: "card fab", id: "tilt", "aria-label": "Toggle 3D view", onclick: () => map.easeTo({ pitch: map.getPitch() > 5 ? 0 : 55, duration: 600 }) }, "3D");

  root.append(directions.el, controls, dock, alerts, timebar, tilt, footer, status);
  syncToggles();
  applyTraffic();
  void operatorSignedIn().then((ok) => { if (ok === false) signedOut(); });

  // ---- polling ----------------------------------------------------------------------------------
  const poll = (fn: () => Promise<void>, ms: number) => {
    let running = false;
    const tick = async () => {
      if (running) return;
      running = true;
      try {
        await fn();
        ctx.online = true;
      } catch {
        ctx.online = false;
      }
      status.hidden = ctx.online;
      running = false;
    };
    void tick();
    return setInterval(tick, ms);
  };
  poll(async () => {
    const s = await api.state();
    ctx.state = s;
    ctx.method = s.method;
    aircraft.update(s.aircraft);
    syncToggles();
    updateAlerts();
  }, 1000);
  poll(async () => {
    ctx.prevTraffic = ctx.traffic;
    ctx.traffic = await api.traffic();
    applyTraffic();
    notify(ctx);
    if (openPanel) panels.find((p) => p.id === openPanel)?.refresh();
  }, 5000);
  poll(async () => {
    ctx.flights = await api.flights(true);
    if (openPanel) panels.find((p) => p.id === openPanel)?.refresh();
    else panels[1].refresh();
  }, 3000);
  poll(async () => {
    try {
      const r = await fetch("/api/adsb");
      if (!r.ok) throw new Error(String(r.status));
      const d = (await r.json()) as AdsbResponse;
      ctx.adsb.ok = d.feed?.state === "ok";
      adsb.update(ctx.adsb.ok ? d.aircraft : null);
    } catch {
      ctx.adsb.ok = false;
      adsb.update(null);
    }
    notify(ctx);
  }, ADSB_POLL_MS);
  poll(async () => { ctx.health = await api.health(); }, 5000);
  void api.models().then((m) => { ctx.forecaster = m.forecaster; syncToggles(); }).catch(() => {});
}
