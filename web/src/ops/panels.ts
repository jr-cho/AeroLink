/** Panels of the ATC dashboard that open from the corner buttons: conflicts, alerts, unit health, system health. */
import { h } from "../shared/dom.ts";
import { fmtClock, fmtDuration, fmtNm, fmtPct } from "../shared/format.ts";
import type { Airspace } from "../shared/map.ts";
import type { AircraftState, Method, ModelsResponse, TrafficResponse } from "../shared/types.ts";
import {
  type AdsbResponse, type AlertRow, type Conflict, type ConflictsResponse, type DeviceRow, type OpsRoute, type OverviewResponse, type Reroute, type Source, type TelemetryRow, type WhatIf, type Watchdog,
} from "./types.ts";
import { ago, aiMark, call, inText, nodeName, uptime } from "./util.ts";

export type Filter = "all" | "needs" | "medical" | "reroutes";

/** Everything the panels read; `app.ts` owns it and calls the panels' `render` when a piece changes. */
export interface Ops {
  airspace: Airspace;
  routes: OpsRoute[];
  conflicts: ConflictsResponse | null;
  state: AircraftState[];
  adsb: AdsbResponse | null;
  devices: DeviceRow[];
  alerts: AlertRow[];
  watchdogs: Watchdog[];
  traffic: TrafficResponse | null;
  models: (ModelsResponse & { models?: { name: string; loaded: boolean; error?: string | null }[] }) | null;
  scenarios: { scenarios: string[]; current: string | null } | null;
  method: Method;
  forecaster: string;
  filter: Filter;
  selected: string | null;
  selectedConflict: string | null;
  track: TelemetryRow[];
  whatif: WhatIf | null;
  whatifError: string | null;
  whatifShown: number | null;
  operator: string;
  ackError: string;
  markError: string;
  markBusy: boolean;
  /** The flight detail (the existing view) is open; the popover shows otherwise (R-contract §6.4). */
  detail: boolean;
  /** The flight popover is open for the selected flight. Esc closes it; the selection stays. */
  popOpen: boolean;
  /** The AI overview of the next 30 min (null until the first answer). */
  overview: OverviewResponse | null;
  /** The header is collapsed to its one line (remembered per browser). */
  overviewCollapsed: boolean;
  /** All bullets shown, not the first three. */
  overviewAllBullets: boolean;
  /** AI reroute proposals known to the page (open ones from the poll, the rest from the socket). */
  reroutes: Reroute[];
  /** Finished cards that have collapsed into the flight's records (and dismissed ones). */
  collapsedReroutes: Set<string>;
  rerouteBusy: Set<string>;
  rerouteError: Record<string, string>;
  wsUp: boolean;
  settingsNote: string;
}

export interface Actions {
  select(flightId: string | null): void;
  selectConflict(c: Conflict): void;
  previewWhatIf(index: number | null): void;
  ack(id: number): void;
  /** Mark the flight's ATC requirement handled (by radio) or undo it. */
  mark(flightId: string, action: "cleared" | "contacted" | "revoke"): void;
  /** Fly to an aircraft id, ADS-B icao or node id. */
  focus(id: string, src?: Source): void;
  setMethod(m: Method): void;
  setForecaster(f: string): void;
  setScenario(s: string): void;
  approveReroute(id: string): void;
  dismissReroute(id: string): void;
  toggleOverview(): void;
  /** Open the flight detail from the popover's Details button. */
  details(flightId: string): void;
  /** Close the popover; the selection stays (Esc). */
  closePop(): void;
  toggleOverviewMore(): void;
}

export const cls = (...c: (string | false | undefined)[]) => c.filter(Boolean).join(" ");

export const badge = (text: string, kind: string) => h("span", { class: `badge b-${kind}` }, text);

export function conflictedFlights(c: ConflictsResponse | null): Set<string> {
  return new Set((c?.conflicts ?? []).flatMap((x) => x.flights.map((f) => f.flight_id)));
}

export function conflictTitle(o: Ops, c: Conflict): string {
  const where = c.node ? nodeName(o.airspace, c.node) : c.edge ? `${nodeName(o.airspace, c.edge[0])} to ${nodeName(o.airspace, c.edge[1])}` : "route";
  const who = c.kind === "adsb" ? `${call(c.flights[0].aircraft_id)} and ADS-B ${c.adsb?.callsign || c.adsb?.icao}` : `${call(c.flights[0].aircraft_id)} and ${call(c.flights[1].aircraft_id)}`;
  return `${who} ${c.kind === "opposing" ? "opposing on" : "at"} ${where} ${inText(c.time_to_conflict_s)}`;
}

// ---------------------------------------------------------------------------------------------
// Right: tabs
// ---------------------------------------------------------------------------------------------
export function conflictsTab(o: Ops, act: Actions): HTMLElement {
  const c = o.conflicts;
  const root = h("div", { id: "conflict-list" });
  if (!c) return h("p", { class: "muted" }, "Loading");
  root.append(h("p", { class: "muted small" }, `Predicted conflicts in the next ${c.horizon_min} min, most urgent first. ADS-B: ${c.adsb_state}.`));
  if (!c.conflicts.length) root.append(h("p", { class: "empty" }, "No predicted conflicts."));
  for (const x of c.conflicts) {
    const detail = x.kind === "adsb" ? `closest approach ${fmtNm(x.min_distance_m ?? 0)}${x.adsb?.alt_ft != null ? `, ${Math.round(x.adsb.alt_ft)} ft` : ""}` : `windows overlap ${fmtDuration(x.overlap_s ?? 0)}, p50 gap ${fmtDuration(x.gap_s ?? 0)}`;
    root.append(h("button", { type: "button", class: cls("cf-row", `sev-${x.severity}`, x.id === o.selectedConflict && "sel"), "data-conflict": x.id, onclick: () => act.selectConflict(x) },
      h("div", { class: "fl-top" }, aiMark(), badge(x.severity === "high" ? "High" : "Medium", x.severity), x.medical && badge("Medical", "medical"), badge(x.kind === "adsb" ? "ADS-B" : x.kind, "plain"), h("span", { class: "grow" }), h("span", { class: "small" }, `${fmtClock(x.t)}`)),
      h("div", {}, conflictTitle(o, x)),
      h("div", { class: "muted small" }, detail + (x.bunching_risk !== null ? `, bunching ${fmtPct(x.bunching_risk)}` : ""))));
  }
  if (c.groups.length) {
    root.append(h("h3", {}, "Queues forming"), ...c.groups.map((g) => h("button", { type: "button", class: "mini-row group", onclick: () => act.focus(g.node) }, `${g.count} flights at ${nodeName(o.airspace, g.node)} ${inText(g.time_to_conflict_s)}${g.medical ? " (medical)" : ""}`)));
  }
  root.append(h("h3", {}, "Bunching risk (route engine)"));
  if (!c.bunching.length) root.append(h("p", { class: "muted small" }, "No flight above 5 %."));
  for (const b of c.bunching.slice(0, 8)) {
    const f = o.routes.find((r) => r.flight_id === b.flight_id);
    root.append(h("button", { type: "button", class: "mini-row", onclick: () => f && act.select(f.flight_id) }, `${call(b.aircraft_id)}: ${fmtPct(b.risk)}`));
  }
  return root;
}

export function alertsTab(o: Ops, act: Actions, onlyOpen: boolean, toggle: () => void): HTMLElement {
  const root = h("div", { id: "alert-panel" });
  const list = o.alerts.filter((a) => !onlyOpen || (!a.acked_at && a.kind !== "alert_ack"));
  root.append(h("label", { class: "check small" }, h("input", { type: "checkbox", id: "alerts-open", checked: onlyOpen, onchange: toggle }), "Unacknowledged only"));
  if (o.ackError) root.append(h("p", { class: "err small", id: "ack-error" }, o.ackError));
  if (!list.length) root.append(h("p", { class: "empty" }, "No alerts."));
  for (const a of list.slice(0, 100)) {
    const acked = !!a.acked_at;
    root.append(h("div", { class: cls("al-row", `sev-${a.severity}`, acked && "acked"), "data-alert": a.id },
      h("div", { class: "fl-top" }, badge(a.severity, a.severity === "critical" ? "danger" : a.severity === "warn" ? "warn" : "plain"), h("span", { class: "muted small" }, a.kind.replace(/_/g, " ")), h("span", { class: "grow" }), h("span", { class: "small muted" }, fmtClock(a.at))),
      h("div", {}, a.message),
      h("div", { class: "al-foot" },
        a.aircraft_id && h("button", { type: "button", class: "link", onclick: () => act.focus(a.aircraft_id!) }, call(a.aircraft_id)),
        h("span", { class: "grow" }),
        a.kind === "alert_ack" ? h("span", { class: "muted small" }, "log entry") : acked ? h("span", { class: "muted small" }, `acknowledged by ${a.acked_by}`) : h("button", { type: "button", class: "btn ack", "data-ack": a.id, onclick: () => act.ack(a.id) }, "Acknowledge"))));
  }
  return root;
}

const STATUS_BADGE: Record<string, string> = { healthy: "ok", degraded: "warn", offline: "danger" };

export function fleetTab(o: Ops): HTMLElement {
  const root = h("div", { id: "fleet-panel" });
  if (!o.devices.length) return h("p", { class: "empty" }, "No units registered.");
  root.append(h("div", { class: "scroll-x" }, h("table", { class: "tbl small", id: "fleet-table" },
    h("thead", {}, h("tr", {}, ...["Unit", "Status", "Last seen", "GNSS", "Wi-Fi", "Uptime", "Last reset", "Firmware"].map((t) => h("th", {}, t)))),
    h("tbody", {}, ...o.devices.map((d) => {
      const hh = d.health;
      return h("tr", { "data-unit": d.id },
        h("td", {}, h("b", {}, d.callsign || d.id), h("div", { class: "muted" }, d.callsign ? `${d.id}, ${d.vehicle_type}` : d.vehicle_type)),
        h("td", {}, badge(d.status, STATUS_BADGE[d.status] ?? "plain"), d.status_reasons.length ? h("div", { class: "muted" }, d.status_reasons.map((x) => x.replace(/_/g, " ")).join(", ")) : null),
        h("td", {}, ago(d.age_s)),
        h("td", {}, hh ? `${hh.gnss.fix}, ${hh.gnss.satellites} sats, HDOP ${hh.gnss.hdop}` : "none"),
        h("td", {}, hh ? `${hh.wifi_rssi_dbm} dBm` : "none"),
        h("td", {}, hh ? uptime(hh.uptime_s) : "none"),
        h("td", {}, hh ? hh.reset_reason.replace(/_/g, " ") : "none"),
        h("td", {}, hh ? hh.firmware_version : "none"));
    })))));
  root.append(h("p", { class: "muted small" }, "A unit with no health report yet shows healthy while its telemetry flows."));
  return root;
}

const WD_BADGE: Record<string, string> = { ok: "ok", warn: "warn", fail: "danger" };

export function systemTab(o: Ops, act: Actions): HTMLElement {
  const root = h("div", { id: "system-panel" });
  const t = o.traffic as (TrafficResponse & { forecast_age_s?: number | null; stale?: boolean }) | null;
  root.append(h("h3", {}, "Watchdogs"));
  if (!o.watchdogs.length) root.append(h("p", { class: "muted small" }, "No results yet."));
  else root.append(h("table", { class: "tbl small", id: "watchdog-table" }, h("tbody", {}, ...o.watchdogs.map((w) =>
    h("tr", { "data-watchdog": w.name }, h("td", {}, h("b", {}, w.name)), h("td", {}, badge(w.state, WD_BADGE[w.state] ?? "plain")), h("td", {}, w.detail, h("div", { class: "muted" }, `limit: ${w.threshold}`)))))));
  const feed = o.adsb?.feed;
  root.append(h("h3", {}, "Live ADS-B feed"), h("p", { id: "adsb-state", class: "small" },
    badge(feed?.state ?? "unknown", feed?.state === "ok" ? "ok" : feed?.state === "disabled" ? "plain" : "danger"),
    ` ${feed ? `${feed.kept} aircraft kept, ${feed.dropped} filtered out` : ""}`,
    feed?.error ? h("div", { class: "err" }, feed.error) : null,
    feed?.last_ok_at ? h("div", { class: "muted" }, `last update ${fmtClock(feed.last_ok_at)}`) : null));
  root.append(h("h3", {}, "Forecast"), h("p", { class: "small", id: "forecast-state" },
    t?.forecast_age_s != null ? `Refreshed ${Math.round(t.forecast_age_s)} s ago` : "Not refreshed yet", t?.stale ? badge("stale", "danger") : null));
  const models = o.models?.models ?? [];
  if (models.length) root.append(h("h3", {}, "Models"), h("ul", { class: "small plain", id: "model-list" }, ...models.map((m) => h("li", {}, `${m.name}: `, badge(m.loaded ? "loaded" : m.error ? "error" : "not loaded", m.loaded ? "ok" : m.error ? "danger" : "plain")))));
  root.append(h("h3", {}, "System settings"), h("p", { class: "muted small" }, "These change the forecast for everyone using AeroLink. They never touch a flight."));
  const methods = (["classical", "aerolink"] as Method[]).map((m) => h("button", { type: "button", class: "seg", "data-method": m, "aria-pressed": String(o.method === m), onclick: () => act.setMethod(m) }, m === "aerolink" ? "AeroLink" : "Classical"));
  const forecasters = [...new Set(["cv", "chronos", "aerolink-gbm", o.forecaster])];
  const fsel = h("select", { id: "sys-forecaster", "aria-label": "Forecaster", onchange: () => act.setForecaster(fsel.value) }, ...forecasters.map((f) => h("option", { value: f, selected: f === o.forecaster }, f)));
  const scen = o.scenarios?.scenarios ?? [];
  const ssel = h("select", { id: "sys-scenario", "aria-label": "Scenario", onchange: () => act.setScenario(ssel.value) }, ...scen.map((s) => h("option", { value: s, selected: s === o.scenarios?.current }, s)));
  root.append(h("div", { class: "ctl" }, h("span", { class: "ctl-label" }, "Routing method"), h("div", { class: "seg-row", id: "sys-method" }, methods)),
    h("div", { class: "ctl" }, h("label", { class: "ctl-label", for: "sys-forecaster" }, "Forecaster"), fsel),
    h("div", { class: "ctl" }, h("label", { class: "ctl-label", for: "sys-scenario" }, "Simulator scenario"), ssel),
    h("p", { class: "small", role: "status", id: "settings-note" }, o.settingsNote));
  return root;
}
