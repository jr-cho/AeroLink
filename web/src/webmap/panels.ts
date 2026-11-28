/** Side panels of the web map: layers, planned flights, medical view, AI impact, scenario control. */
import { api, isUnauthorized } from "../shared/api.ts";
import { NOGO_COLOR, NOGO_LABEL, TRAFFIC_COLOR, TRAFFIC_LABEL, VEHICLE_COLOR, VEHICLE_LABEL } from "../shared/colors.ts";
import { clear, h } from "../shared/dom.ts";
import { fmtClock } from "../shared/format.ts";
import { setData, setLayerVisible, setNogoVisible, cvCone } from "../shared/map.ts";
import type { AblationRun, AblationSummary, EvalResponse, EvalRow, FlightRow, TrafficLevel } from "../shared/types.ts";
import type { Ctx } from "./context.ts";

export interface Panel {
  id: string;
  title: string;
  el: HTMLElement;
  /** Called when the panel is shown or the data changed. */
  refresh(): void;
}

/** Used only when `/api/scenarios` cannot be read (backend/scenarios/*.yaml is the real list). */
export const SCENARIOS_FALLBACK = ["nominal_day", "i4_peak", "medical_dispatch"];

// ---- Layers ---------------------------------------------------------------------------------

export function layersPanel(ctx: Ctx, opts: { onTraffic: (on: boolean) => void; onAdsb: (on: boolean) => void }): Panel {
  const toggles: [string, string, boolean, (on: boolean) => void][] = [
    ["Traffic on corridors", "layer-traffic", true, (on) => opts.onTraffic(on)],
    ["No-go airspace (3D)", "layer-nogo", true, (on) => setNogoVisible(ctx.map, on)],
    ["Live aircraft", "layer-aircraft", true, (on) => ctx.aircraft.setVisible(on)],
    ["Live ADS-B traffic", "layer-adsb", true, (on) => opts.onAdsb(on)],
    ["Planned flights", "layer-planned", false, (on) => setLayerVisible(ctx.map, ["planned"], on)],
    ["Prediction cones", "layer-cones", false, (on) => setLayerVisible(ctx.map, ["cones", "cones-outline"], on)],
  ];
  const el = h("div", { class: "panel-body" });
  el.append(h("h3", {}, "Map layers"));
  for (const [label, id, on, fn] of toggles) {
    el.append(h("label", { class: "check" }, h("input", { type: "checkbox", id, checked: on, onchange: (e: Event) => fn((e.target as HTMLInputElement).checked) }), label));
  }
  const adsbNote = h("p", { class: "muted small", id: "adsb-note", hidden: true }, "ADS-B unavailable");
  el.querySelector("#layer-adsb")?.closest("label")?.after(adsbNote);
  const conesNote = h("p", { class: "muted small", id: "cones-note" });
  el.append(
    conesNote,
    h("h3", {}, "Legend"),
    h("div", { class: "legend" },
      ...(Object.keys(TRAFFIC_COLOR) as TrafficLevel[]).map((l) => h("span", {}, h("i", { class: "line", style: { background: TRAFFIC_COLOR[l] } }), `${TRAFFIC_LABEL[l]} traffic`)),
      h("span", {}, h("i", { class: "line dashed", style: { color: "#e37400" } }), "Needs Class D coordination"),
      h("span", {}, h("i", { class: "line dashed", style: { color: "#a142f4" } }), "Needs Class B clearance"),
      h("span", {}, h("i", { class: "pt evtol" }), "eVTOL site"),
      h("span", {}, h("i", { class: "pt helipad" }), "Hospital helipad"),
      ...(Object.keys(VEHICLE_COLOR) as (keyof typeof VEHICLE_COLOR)[]).map((v) => h("span", {}, h("i", { class: "dot", style: { background: VEHICLE_COLOR[v] } }), VEHICLE_LABEL[v])),
      h("span", {}, h("i", { class: "dot lost" }), "Lost / offline"),
      h("span", {}, h("i", { class: "dot ring" }), "Off route")),
    h("div", { class: "legend" }, ...Object.entries(NOGO_LABEL).map(([k, v]) => h("span", {}, h("i", { class: "box", style: { background: NOGO_COLOR[k] } }), v))));
  function refresh(): void {
    adsbNote.hidden = ctx.adsb.ok;
    conesNote.textContent = ctx.traffic?.cones
      ? "Prediction cones come from the server's forecast: a ribbon along each planned route, as wide as the spread of its predicted arrival times (likely range), out to 45 minutes."
      : "Prediction cones: the server sent none, so this is a client-side constant-velocity estimate (two minutes ahead), not a forecast.";
  }
  refresh();
  return { id: "layers", title: "Layers", el, refresh };
}

// ---- Planned flights ------------------------------------------------------------------------

function nodeName(ctx: Ctx, id: string): string {
  return ctx.airspace.nodes.get(id)?.props.name ?? id;
}

/** Line geometry of a flight's node path, following corridor shapes where they exist. */
function flightLine(ctx: Ctx, path: string[]): GeoJSON.Position[] {
  const coords: GeoJSON.Position[] = [];
  for (let i = 0; i + 1 < path.length; i++) {
    const [u, v] = [path[i], path[i + 1]];
    const f = ctx.airspace.corridors.features.find((c) => (c.properties?.from === u && c.properties?.to === v) || (c.properties?.from === v && c.properties?.to === u));
    let seg: GeoJSON.Position[];
    if (f) seg = f.properties?.from === u ? f.geometry.coordinates : [...f.geometry.coordinates].reverse();
    else seg = [u, v].map((n) => { const p = ctx.airspace.nodes.get(n); return p ? [p.lon, p.lat] : [0, 0]; });
    coords.push(...(coords.length ? seg.slice(1) : seg));
  }
  return coords;
}

export function flightsPanel(ctx: Ctx): Panel {
  const el = h("div", { class: "panel-body", id: "flights-panel" });
  function refresh(): void {
    clear(el);
    el.append(h("h3", {}, "Planned flights"));
    const rows = ctx.flights;
    if (!rows.length) el.append(h("p", { class: "muted" }, "No planned flights right now."));
    for (const f of rows) {
      const eta = f.eta_p50 ?? f.eta_by_node?.[f.destination];
      el.append(h("div", { class: "row-card flight", "data-aircraft": f.aircraft_id },
        h("div", { class: "row-title" }, h("i", { class: "dot", style: { background: VEHICLE_COLOR[f.vehicle_type] } }), f.aircraft_id, h("span", { class: "chip" }, f.status)),
        h("div", { class: "muted" }, `${nodeName(ctx, f.path[0])} to ${nodeName(ctx, f.destination)}`),
        eta !== undefined ? h("div", {}, "ETA ", h("b", {}, fmtClock(eta)), etaWindow(f)) : null));
    }
    // map overlay
    setData(ctx.map, "planned", { type: "FeatureCollection", features: rows.filter((f) => f.path?.length > 1).map((f) => ({ type: "Feature", properties: { vehicle_type: f.vehicle_type, aircraft_id: f.aircraft_id }, geometry: { type: "LineString", coordinates: flightLine(ctx, f.path) } })) });
    const cones = ctx.traffic?.cones ?? { type: "FeatureCollection", features: (ctx.state?.aircraft ?? []).filter((a) => a.online && !a.on_ground).map((a) => cvCone(a)) };
    setData(ctx.map, "cones", cones);
  }
  return { id: "flights", title: "Flights", el, refresh };
}

function etaWindow(f: FlightRow): string {
  return f.eta_p10 !== undefined && f.eta_p90 !== undefined ? ` (window ${fmtClock(f.eta_p10)} to ${fmtClock(f.eta_p90)})` : "";
}

// ---- Medical view ---------------------------------------------------------------------------

/** A flight whose prediction moved because of the medical flight: from the server's `prediction_history`. */
interface Affected {
  flight: FlightRow;
  before: number | null;
  after: number;
  reason: string;
}

function affectedBy(med: FlightRow, flights: FlightRow[]): Affected[] {
  const out: Affected[] = [];
  for (const o of flights) {
    if (o.flight_id === med.flight_id) continue;
    const hist = o.prediction_history ?? [];
    const i = hist.findIndex((e) => e.reason.includes(med.aircraft_id) && e.reason.includes("planned"));
    if (i >= 0) out.push({ flight: o, before: i > 0 ? hist[i - 1].eta_p50 : null, after: hist[i].eta_p50, reason: hist[i].reason });
  }
  return out;
}

/** Medical flights and which other flights' predictions changed after each one was planned (and why). */
export function medicalPanel(ctx: Ctx): Panel {
  const el = h("div", { class: "panel-body", id: "medical-panel" });
  function refresh(): void {
    clear(el);
    el.append(h("h3", {}, "Medical flights"));
    const med = ctx.flights.filter((f) => f.vehicle_type === "medical");
    if (!med.length) el.append(h("p", { class: "muted" }, "No medical flight is active."));
    for (const f of med) {
      const hit = affectedBy(f, ctx.flights);
      el.append(h("div", { class: "row-card medical-flight", "data-aircraft": f.aircraft_id },
        h("div", { class: "row-title" }, h("i", { class: "dot", style: { background: VEHICLE_COLOR.medical } }), f.aircraft_id, h("span", { class: "chip" }, f.status)),
        h("div", { class: "muted" }, `${nodeName(ctx, f.path[0])} to ${nodeName(ctx, f.destination)}`),
        h("div", {}, "Flights whose predicted arrival changed after it was planned: ", h("b", {}, String(hit.length))),
        ...hit.map((x) => h("div", { class: "muted small affected", "data-aircraft": x.flight.aircraft_id },
          h("b", {}, x.flight.aircraft_id), ` arrival ${x.before !== null ? `from ${fmtClock(x.before)} to ` : "to "}${fmtClock(x.after)}`, ` (${x.reason})`))));
    }
    el.append(h("p", { class: "muted small" }, "Medical flights are served first. Each change is a prediction the server recorded when the flight was planned on shared corridors; the first line of a flight's history is its initial estimate."));
  }
  return { id: "medical", title: "Medical", el, refresh };
}

// ---- AI impact ------------------------------------------------------------------------------

function mean(r: EvalRow): number {
  return r.completed_trips ? r.total_trip_time_s / r.completed_trips : NaN;
}

/** A metric of the ablation table. `better` says which direction is an improvement (none: just a count). */
interface Metric {
  key: keyof AblationSummary & string;
  label: string;
  fmt: (v: number) => string;
  better?: "lower" | "higher";
}
const sec = (v: number) => `${v.toFixed(0)} s`;
const sec1 = (v: number) => `${v.toFixed(1)} s`;
export const ABLATION_METRICS: Metric[] = [
  { key: "door_to_door_mean_s", label: "Door-to-door, mean", fmt: sec, better: "lower" },
  { key: "door_to_door_p95_s", label: "Door-to-door, p95", fmt: sec, better: "lower" },
  { key: "ground_delay_mean_s", label: "Ground delay, mean", fmt: sec1, better: "lower" },
  { key: "ground_delay_p95_s", label: "Ground delay, p95", fmt: sec1, better: "lower" },
  { key: "trip_time_mean_s", label: "Flight time, mean", fmt: sec, better: "lower" },
  { key: "holding_s_per_trip", label: "Holding per trip", fmt: sec1, better: "lower" },
  { key: "eta_error_mean_s", label: "ETA error (primary), mean", fmt: sec, better: "lower" },
  { key: "eta_error_p90_s", label: "ETA error (primary), p90", fmt: sec, better: "lower" },
  { key: "eta_error_cruise_mean_s", label: "ETA error, cruise-only, mean", fmt: sec, better: "lower" },
  { key: "eta_coverage", label: "ETA window coverage", fmt: (v) => `${(v * 100).toFixed(0)}%`, better: "higher" },
  { key: "suggestions_offered", label: "Suggestions offered", fmt: (v) => String(Math.round(v)) },
  { key: "suggestions_accepted", label: "Suggestions accepted", fmt: (v) => String(Math.round(v)) },
  { key: "bunching_events", label: "Bunching events", fmt: (v) => String(Math.round(v)), better: "lower" },
  { key: "loss_of_separation", label: "Loss of separation", fmt: (v) => String(Math.round(v)), better: "lower" },
  { key: "medical_response_mean_s", label: "Medical response, mean", fmt: sec, better: "lower" },
  { key: "no_route", label: "No-route answers", fmt: (v) => String(Math.round(v)), better: "lower" },
];

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** B1 -> A5 verdict for one metric, as measured. Equal at the displayed precision is shown as equal. */
export function compare(m: Metric, b1: number | null, a5: number | null): { text: string; cls: string } {
  if (b1 === null || a5 === null) return { text: "none", cls: "" };
  if (m.fmt(b1) === m.fmt(a5)) return { text: "equal", cls: "equal" };
  const d = a5 - b1;
  const text = `${d > 0 ? "+" : "-"}${m.fmt(Math.abs(d))}`;
  if (!m.better) return { text, cls: "" };
  return { text: `${text} ${(m.better === "lower") === d < 0 ? "better" : "worse"}`, cls: (m.better === "lower") === d < 0 ? "better" : "worse" };
}

function ablationView(ab: NonNullable<EvalResponse["ablations"]>): HTMLElement[] {
  const b1 = ab.summary.find((r) => r.config === "B1");
  const a5 = ab.summary.find((r) => r.config === "A5");
  if (!b1 || !a5) return [h("p", { class: "muted" }, "The ablation table has no B1 or A5 row.")];
  const rows = ABLATION_METRICS.filter((m) => num(b1[m.key]) !== null || num(a5[m.key]) !== null);
  const t = h("table", { class: "metrics ablation" },
    h("thead", {}, h("tr", {}, h("th", {}, "Metric"), h("th", {}, "B1"), h("th", {}, "A5"), h("th", {}, "A5 vs B1"))),
    h("tbody", {}, ...rows.map((m) => {
      const x = num(b1[m.key]), y = num(a5[m.key]);
      const c = compare(m, x, y);
      return h("tr", { "data-metric": m.key }, h("td", {}, m.label), h("td", {}, x === null ? "none" : m.fmt(x)), h("td", {}, y === null ? "none" : m.fmt(y)), h("td", { class: `delta ${c.cls}` }, c.text));
    })));
  const out: HTMLElement[] = [h("div", { class: "scroll-x" }, t)];
  const runs = ab.runs ?? [];
  const scenarios = [...new Set(runs.map((r: AblationRun) => r.scenario))];
  if (scenarios.length) {
    const key = ABLATION_METRICS[0];
    const per = h("table", { class: "metrics per-scenario" },
      h("thead", {}, h("tr", {}, ...["Scenario", `B1 ${key.label.toLowerCase()}`, "A5", "A5 vs B1"].map((c) => h("th", {}, c)))),
      h("tbody", {}, ...scenarios.map((sc) => {
        const x = num(runs.find((r) => r.scenario === sc && r.config === "B1")?.[key.key]);
        const y = num(runs.find((r) => r.scenario === sc && r.config === "A5")?.[key.key]);
        const c = compare(key, x, y);
        return h("tr", {}, h("td", {}, sc), h("td", {}, x === null ? "none" : key.fmt(x)), h("td", {}, y === null ? "none" : key.fmt(y)), h("td", { class: `delta ${c.cls}` }, c.text));
      })));
    out.push(h("h3", {}, "Per scenario"), h("div", { class: "scroll-x" }, per));
  }
  out.push(h("p", { class: "muted small" }, `Offline runs, ${b1.trips ?? "?"} trips (B1) and ${a5.trips ?? "?"} (A5). "equal" means the same at the precision shown. Numbers are as measured, with no tuning; a metric the run did not record is left out.`));
  return out;
}

/** The M4 fallback table (one row per scenario and configuration), for a server without ablation results. */
function m4View(rows: EvalRow[]): HTMLElement[] {
  const cell = (v: number, d = 0) => (Number.isFinite(v) ? v.toFixed(d) : "none");
  const scenarios = [...new Set(rows.map((r) => r.scenario))];
  const by = (s: string, c: string) => rows.find((r) => r.scenario === s && r.config === c);
  const t = h("table", { class: "metrics" }, h("thead", {}, h("tr", {}, ...["Scenario", "Config", "Trips", "Avg trip (s)", "Pad wait (s)", "Bunching", "Med. wait (s)"].map((c) => h("th", {}, c)))));
  const tb = h("tbody", {});
  for (const s of scenarios) {
    for (const c of ["B1", "A5"]) {
      const r = by(s, c);
      if (!r) continue;
      tb.append(h("tr", { "data-config": c }, h("td", {}, s), h("td", {}, h("b", {}, c)), h("td", {}, `${r.completed_trips}/${r.trips}`), h("td", {}, cell(mean(r))), h("td", {}, cell(r.total_pad_wait_s ?? NaN)), h("td", {}, cell(r.bunching_events ?? NaN)), h("td", {}, cell(r.medical_wait_s ?? NaN))));
    }
    if (by(s, "B1") && !by(s, "A5")) tb.append(h("tr", { class: "missing" }, h("td", {}, s), h("td", { colspan: 6 }, "A5 not in the committed run")));
  }
  t.append(tb);
  return [h("div", { class: "scroll-x" }, t), h("p", { class: "muted small" }, `Committed M4 runs (${rows.length} rows, configurations ${[...new Set(rows.map((r) => r.config))].join(", ")}). The M5 ablation table is not available from this server. Numbers are as measured, with no tuning.`)];
}

export function impactPanel(ctx: Ctx): Panel {
  const el = h("div", { class: "panel-body", id: "impact-panel" });
  let data: EvalResponse | null = null;
  let err = "";
  let loading = false;
  async function load(): Promise<void> {
    if (data || loading) return;
    loading = true;
    try {
      data = await api.evalAll();
    } catch (e) {
      err = (e as Error).message;
    }
    loading = false;
    refresh();
  }
  function refresh(): void {
    clear(el);
    el.append(h("h3", {}, "AI impact: B1 (classical) vs A5 (full AeroLink)"));
    void load();
    if (err) el.append(h("p", { class: "error" }, `Evaluation results unavailable: ${err}`));
    if (!data && !err) el.append(h("p", { class: "muted" }, "Loading committed runs"));
    if (data?.ablations?.summary?.length) el.append(...ablationView(data.ablations));
    else if (data?.scenarios?.length) el.append(...m4View(data.scenarios));
    else if (data) el.append(h("p", { class: "muted" }, "The server has no evaluation results."));
    el.append(h("h3", {}, "Live counters"));
    const stats = Object.entries(ctx.health?.stats ?? {});
    if (!stats.length) el.append(h("p", { class: "muted" }, "No live counters (backend offline)."));
    else el.append(h("dl", { class: "stats" }, ...stats.sort().flatMap(([k, v]) => [h("dt", {}, k.replace(/_/g, " ")), h("dd", {}, String(v))])));
    if (ctx.health) el.append(h("p", { class: "muted small" }, `MQTT ${ctx.health.mqtt ? "connected" : "not connected"}`));
  }
  return { id: "impact", title: "AI impact", el, refresh };
}

// ---- Scenario -------------------------------------------------------------------------------

/** `signIn` builds the shared "Operator sign-in required" link; `onUnauthorized` tells the page a PUT got 401. */
export function scenarioPanel(ctx: Ctx, signIn: () => HTMLElement, onUnauthorized: () => void): Panel {
  const el = h("div", { class: "panel-body", id: "scenario-panel" });
  let msg = "";
  let isErr = false;
  let current = "";
  let list: string[] = SCENARIOS_FALLBACK;
  let fromServer = false;
  let busy = false;
  let loaded = false;
  async function load(): Promise<void> {
    if (loaded) return;
    loaded = true;
    try {
      const r = await api.scenarios();
      list = r.scenarios;
      current = r.current ?? current;
      fromServer = true;
    } catch {
      loaded = false; // try again next time the panel opens
    }
    refresh();
  }
  async function pick(s: string): Promise<void> {
    busy = true;
    msg = `Switching to ${s}`;
    isErr = false;
    refresh();
    try {
      await api.setScenario(s);
      current = s;
      msg = `Scenario ${s} requested.`;
    } catch (e) {
      if (isUnauthorized(e)) {
        msg = "";
        onUnauthorized();
      } else {
        msg = `Could not switch: ${(e as Error).message}`;
        isErr = true;
      }
    }
    busy = false;
    refresh();
  }
  function refresh(): void {
    clear(el);
    el.append(h("h3", {}, "Live scenario"), h("p", { class: "muted small" }, "Restarts the simulated traffic. Aircraft and traffic update in a few seconds."));
    void load();
    for (const s of list) el.append(h("button", { type: "button", class: `list-btn${s === current ? " active" : ""}`, "data-scenario": s, disabled: busy || !ctx.operator, onclick: () => pick(s) }, s.replace(/_/g, " "), s === current ? h("span", { class: "chip" }, msg.includes(s) ? "requested" : "current") : null));
    if (!ctx.operator) el.append(signIn());
    if (!fromServer) el.append(h("p", { class: "muted small" }, "The scenario list could not be read from the server; showing the common ones."));
    if (msg) el.append(h("p", { class: isErr ? "error" : "muted", role: "status" }, msg));
  }
  return { id: "scenario", title: "Scenario", el, refresh };
}
